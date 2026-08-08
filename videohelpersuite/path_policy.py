"""Immutable filesystem and URL authorization policy for VHS runtime inputs."""

from __future__ import annotations

import ipaddress
import os
import socket
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Mapping
from urllib.parse import SplitResult, urlsplit, urlunsplit


class PolicyConfigurationError(RuntimeError):
    """Raised when server-owned security configuration is invalid or ambiguous."""


class PathAccessDenied(PermissionError):
    def __init__(self, capability: "PathCapability", reason: str):
        self.capability = capability
        self.reason = reason
        super().__init__(
            f"Path access denied for {capability.value}: {reason}."
        )


class URLAccessDenied(PermissionError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"URL access denied: {reason}.")


class PathCapability(str, Enum):
    READ_MEDIA = "read_media"
    LIST_DIRECTORY = "list_directory"
    PREVIEW_MEDIA = "preview_media"
    WRITE_OUTPUT = "write_output"
    DELETE_ARTIFACT = "delete_artifact"


class FilesystemMode(str, Enum):
    HOST_ROOTS = "host_roots"
    ALLOWLIST = "allowlist"
    LEGACY_LOCAL = "legacy_local"


class DeploymentProfile(str, Enum):
    TRUSTED_LOCAL = "trusted_local"
    REMOTE_RESTRICTED = "remote_restricted"


class URLMode(str, Enum):
    DISABLED = "disabled"
    HTTPS = "https"


_READ_CAPABILITIES = frozenset({
    PathCapability.READ_MEDIA,
    PathCapability.LIST_DIRECTORY,
    PathCapability.PREVIEW_MEDIA,
})
_OUTPUT_CAPABILITIES = frozenset(PathCapability)


@dataclass(frozen=True)
class AuthorizedPath:
    requested: str
    canonical: str
    capability: PathCapability
    root_id: str


@dataclass(frozen=True)
class AuthorizedURL:
    normalized: str
    scheme: str
    host: str
    port: int


@dataclass(frozen=True)
class _PolicyRoot:
    root_id: str
    canonical: str
    capabilities: frozenset[PathCapability]


@dataclass(frozen=True)
class PathPolicy:
    deployment_profile: DeploymentProfile
    filesystem_mode: FilesystemMode
    url_mode: URLMode
    roots: tuple[_PolicyRoot, ...]
    legacy_alias: bool = False

    def authorize_path(
        self,
        path: os.PathLike[str] | str,
        capability: PathCapability,
    ) -> AuthorizedPath:
        requested = _path_text(path, capability)
        canonical = _canonical_path(requested, capability)

        matched = _find_root(canonical, capability, self.roots)
        if matched is not None:
            return AuthorizedPath(requested, canonical, capability, matched.root_id)

        if (
            self.filesystem_mode is FilesystemMode.LEGACY_LOCAL
            and capability in _READ_CAPABILITIES
        ):
            return AuthorizedPath(requested, canonical, capability, "legacy_local")

        raise PathAccessDenied(capability, "outside configured capability roots")

    def reauthorize_path(self, authorized: AuthorizedPath) -> AuthorizedPath:
        """Re-resolve a previously authorized request immediately before use."""
        return self.authorize_path(authorized.requested, authorized.capability)

    def validate_url(self, url: str) -> AuthorizedURL:
        if self.url_mode is URLMode.DISABLED:
            raise URLAccessDenied("downloader disabled by deployment policy")
        if not isinstance(url, str) or not url:
            raise URLAccessDenied("malformed URL")

        try:
            parsed = urlsplit(url)
            port = parsed.port or 443
        except (TypeError, ValueError):
            raise URLAccessDenied("malformed URL") from None

        if parsed.scheme.lower() != "https":
            raise URLAccessDenied("unsupported scheme")
        if not parsed.hostname:
            raise URLAccessDenied("missing host")
        if parsed.username is not None or parsed.password is not None:
            raise URLAccessDenied("embedded credentials are forbidden")
        if parsed.fragment:
            raise URLAccessDenied("fragments are forbidden")

        host = parsed.hostname.lower().rstrip(".")
        if not host:
            raise URLAccessDenied("missing host")
        normalized_parts = SplitResult(
            "https",
            parsed.netloc,
            parsed.path or "/",
            parsed.query,
            "",
        )
        return AuthorizedURL(
            normalized=urlunsplit(normalized_parts),
            scheme="https",
            host=host,
            port=port,
        )

    def authorize_url_network(
        self,
        url: str,
        *,
        resolver: Callable[[str], Iterable[str]] | None = None,
    ) -> AuthorizedURL:
        authorized = self.validate_url(url)
        if resolver is None:
            resolver = lambda host: _resolve_addresses(host, authorized.port)
        try:
            addresses = tuple(resolver(authorized.host))
        except (OSError, ValueError):
            raise URLAccessDenied("host resolution failed") from None
        if not addresses:
            raise URLAccessDenied("host resolution failed")
        for raw_address in addresses:
            try:
                address = ipaddress.ip_address(raw_address)
            except ValueError:
                raise URLAccessDenied("host resolution returned an invalid address") from None
            if not address.is_global:
                raise URLAccessDenied("target address class is forbidden")
        return authorized

    def capability_summary(self) -> dict[str, str | bool]:
        return {
            "deployment_profile": self.deployment_profile.value,
            "filesystem_policy": self.filesystem_mode.value,
            "url_policy": self.url_mode.value,
            "legacy_alias": self.legacy_alias,
        }


def build_path_policy(
    *,
    environ: Mapping[str, str],
    host_roots: Mapping[str, str],
    listen: str | None,
) -> PathPolicy:
    """Parse one fail-closed, server-owned policy snapshot."""
    environment = dict(environ)
    derived_profile = (
        DeploymentProfile.TRUSTED_LOCAL
        if _listen_is_loopback_only(listen)
        else DeploymentProfile.REMOTE_RESTRICTED
    )
    profile = _parse_profile(environment, derived_profile)
    filesystem_mode, legacy_alias = _parse_filesystem_mode(environment, profile)
    external_roots = _parse_external_roots(environment, filesystem_mode)
    url_mode = _parse_url_mode(environment, profile)

    roots = _build_roots(host_roots, external_roots)
    return PathPolicy(
        deployment_profile=profile,
        filesystem_mode=filesystem_mode,
        url_mode=url_mode,
        roots=roots,
        legacy_alias=legacy_alias,
    )


def build_runtime_path_policy() -> PathPolicy:
    """Build the immutable policy from ComfyUI-owned runtime state."""
    import folder_paths

    try:
        from comfy.cli_args import args
        listen = getattr(args, "listen", None)
    except (ImportError, AttributeError):
        listen = None

    return build_path_policy(
        environ=os.environ,
        host_roots={
            "input": folder_paths.get_input_directory(),
            "output": folder_paths.get_output_directory(),
            "temp": folder_paths.get_temp_directory(),
        },
        listen=listen,
    )


def _path_text(
    path: os.PathLike[str] | str,
    capability: PathCapability,
) -> str:
    try:
        value = os.fspath(path)
    except TypeError:
        raise PathAccessDenied(capability, "malformed path") from None
    if not isinstance(value, str) or not value or "\x00" in value:
        raise PathAccessDenied(capability, "malformed path")
    return value


def _canonical_path(path: str, capability: PathCapability) -> str:
    try:
        return os.path.realpath(os.path.abspath(path))
    except (OSError, TypeError, ValueError):
        raise PathAccessDenied(capability, "malformed path") from None


def _is_within(root: str, target: str) -> bool:
    try:
        comparison_root = os.path.normcase(root)
        comparison_target = os.path.normcase(target)
        return os.path.commonpath((comparison_root, comparison_target)) == comparison_root
    except (OSError, TypeError, ValueError):
        return False


def _find_root(
    canonical: str,
    capability: PathCapability,
    roots: tuple[_PolicyRoot, ...],
) -> _PolicyRoot | None:
    matches = [
        root
        for root in roots
        if capability in root.capabilities and _is_within(root.canonical, canonical)
    ]
    if not matches:
        return None
    return max(matches, key=lambda root: len(root.canonical))


def _build_roots(
    host_roots: Mapping[str, str],
    external_roots: tuple[str, ...],
) -> tuple[_PolicyRoot, ...]:
    required = {"input", "output", "temp"}
    if set(host_roots) != required:
        raise PolicyConfigurationError("host roots must define input, output, and temp")

    roots = []
    seen = set()
    host_canonicals = []
    for root_id in ("input", "output", "temp"):
        canonical = _configuration_root(host_roots[root_id], require_directory=False)
        comparison = os.path.normcase(canonical)
        if comparison in seen:
            raise PolicyConfigurationError("host roots must be distinct")
        if any(
            _is_within(existing, canonical) or _is_within(canonical, existing)
            for existing in host_canonicals
        ):
            raise PolicyConfigurationError("host roots must not overlap")
        seen.add(comparison)
        host_canonicals.append(canonical)
        roots.append(_PolicyRoot(
            root_id=root_id,
            canonical=canonical,
            capabilities=(
                _READ_CAPABILITIES
                if root_id == "input"
                else _OUTPUT_CAPABILITIES
            ),
        ))

    for index, external in enumerate(external_roots):
        comparison = os.path.normcase(external)
        if comparison in seen:
            raise PolicyConfigurationError("external read roots must be distinct")
        seen.add(comparison)
        roots.append(_PolicyRoot(
            root_id=f"external_{index + 1}",
            canonical=external,
            capabilities=_READ_CAPABILITIES,
        ))
    return tuple(roots)


def _parse_profile(
    environment: Mapping[str, str],
    derived: DeploymentProfile,
) -> DeploymentProfile:
    raw = environment.get("VHS_DEPLOYMENT_PROFILE")
    if raw is None:
        return derived
    try:
        selected = DeploymentProfile(raw.strip().lower())
    except ValueError:
        raise PolicyConfigurationError("invalid deployment profile") from None
    if selected is DeploymentProfile.TRUSTED_LOCAL and derived is not selected:
        raise PolicyConfigurationError(
            "trusted_local cannot override non-loopback or unknown exposure"
        )
    return selected


def _parse_filesystem_mode(
    environment: Mapping[str, str],
    profile: DeploymentProfile,
) -> tuple[FilesystemMode, bool]:
    new_setting = environment.get("VHS_PATH_POLICY")
    legacy_present = "VHS_STRICT_PATHS" in environment
    if new_setting is not None and legacy_present:
        raise PolicyConfigurationError("conflicting path policy settings")

    if legacy_present:
        return FilesystemMode.HOST_ROOTS, True
    if new_setting is None:
        selected = FilesystemMode.HOST_ROOTS
    else:
        try:
            selected = FilesystemMode(new_setting.strip().lower())
        except ValueError:
            raise PolicyConfigurationError("invalid filesystem policy") from None
    if (
        selected is FilesystemMode.LEGACY_LOCAL
        and profile is not DeploymentProfile.TRUSTED_LOCAL
    ):
        raise PolicyConfigurationError(
            "legacy_local is invalid outside trusted_local deployment"
        )
    return selected, False


def _parse_external_roots(
    environment: Mapping[str, str],
    filesystem_mode: FilesystemMode,
) -> tuple[str, ...]:
    raw = environment.get("VHS_EXTERNAL_READ_ROOTS")
    if filesystem_mode is not FilesystemMode.ALLOWLIST:
        if raw:
            raise PolicyConfigurationError(
                "external read roots require allowlist filesystem policy"
            )
        return ()
    if not raw:
        raise PolicyConfigurationError("allowlist filesystem policy requires roots")

    roots = []
    for value in raw.split(os.pathsep):
        if not value.strip():
            raise PolicyConfigurationError("external read roots contain an empty entry")
        roots.append(_configuration_root(value.strip(), require_directory=True))
    return tuple(roots)


def _parse_url_mode(
    environment: Mapping[str, str],
    profile: DeploymentProfile,
) -> URLMode:
    raw = environment.get("VHS_URL_POLICY")
    if raw is None:
        return (
            URLMode.HTTPS
            if profile is DeploymentProfile.TRUSTED_LOCAL
            else URLMode.DISABLED
        )
    try:
        return URLMode(raw.strip().lower())
    except ValueError:
        raise PolicyConfigurationError("invalid URL policy") from None


def _configuration_root(value: str, *, require_directory: bool) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise PolicyConfigurationError("invalid path-policy root")
    try:
        canonical = os.path.realpath(os.path.abspath(value))
    except (OSError, TypeError, ValueError):
        raise PolicyConfigurationError("invalid path-policy root") from None
    if require_directory and not Path(canonical).is_dir():
        raise PolicyConfigurationError("external read root is not an existing directory")
    return canonical


def _listen_is_loopback_only(listen: str | None) -> bool:
    if not isinstance(listen, str) or not listen.strip():
        return False
    for raw in listen.split(","):
        value = raw.strip().strip("[]")
        if value.lower() == "localhost":
            continue
        try:
            if not ipaddress.ip_address(value).is_loopback:
                return False
        except ValueError:
            return False
    return True


def _resolve_addresses(host: str, port: int) -> tuple[str, ...]:
    return tuple({
        result[4][0]
        for result in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    })

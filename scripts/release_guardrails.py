#!/usr/bin/env python3
"""Deterministic, fail-closed release metadata and archive guardrails."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised by the Python 3.10 CI lane.
    import tomli as tomllib


APPROVED_PUBLISHER_ID = "rookiestar"
APPROVED_NODE_ID = "comfyui-videohelper-adv"
APPROVED_VERSION = "2.0.0"
APPROVED_DISPLAY_NAME = "ComfyUI-VideoHelper-Adv"
APPROVED_REPOSITORY = "https://github.com/rookiestar28/ComfyUI-VideoHelper-Adv"
RELEASE_ENVIRONMENT = "comfy-registry-release"

_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_REGISTRY_ID = re.compile(r"^[a-z](?:[a-z0-9]|[._-](?=[a-z0-9])){0,98}[a-z0-9]$")
_REQUIRED_ARCHIVE_PATHS = frozenset(
    {
        "__init__.py",
        "pyproject.toml",
        "requirements.txt",
        "videohelpersuite/nodes.py",
        "web/js/VHS.core.js",
        "video_formats/h264-mp4.json",
    }
)
_FORBIDDEN_PREFIXES = (
    ".git/",
    ".github/",
    ".githooks/",
    ".planning/",
    ".sessions/",
    "reference/",
    "scripts/",
    "testframework/",
    "tests/",
)
_FORBIDDEN_EXACT_NAMES = frozenset(
    {
        ".gitignore",
        ".comfyignore",
        "roadmap.md",
        "requirements-test.txt",
    }
)
_FORBIDDEN_INTERNAL_COMPONENTS = frozenset(
    {".git", ".github", ".planning", ".sessions", "reference"}
)
_FORBIDDEN_POLICY_LEAVES = frozenset({"agents.md", "roadmap.md"})
_FORBIDDEN_SUFFIXES = (
    ".env",
    ".log",
    ".mp4",
    ".mkv",
    ".mov",
    ".webm",
    ".avi",
    ".wav",
    ".mp3",
)
_SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(rb"\bghp_[A-Za-z0-9]{36,}\b"),
    re.compile(rb"\bgh[osru]_[A-Za-z0-9]{36,}\b"),
    re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    re.compile(rb"\bglpat-[A-Za-z0-9_-]{20,}\b"),
    re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(rb"\bsk-[A-Za-z0-9]{32,}\b"),
)
_MAX_ARCHIVE_FILE_BYTES = 20 * 1024 * 1024
_MAX_ARCHIVE_TOTAL_BYTES = 100 * 1024 * 1024


class ArchivePolicyError(ValueError):
    """Raised when a candidate Registry archive violates release policy."""


@dataclass(frozen=True)
class ReleaseMetadata:
    node_id: str
    publisher_id: str
    version: str
    display_name: str
    repository: str


@dataclass(frozen=True)
class ArchiveReport:
    path: Path
    file_count: int
    total_uncompressed_bytes: int
    sha256: str


def load_release_metadata(pyproject_path: Path) -> ReleaseMetadata:
    """Load the immutable identity fields from a PEP 621 project file."""

    with pyproject_path.open("rb") as handle:
        document = tomllib.load(handle)
    project = document.get("project") or {}
    urls = project.get("urls") or {}
    comfy = (document.get("tool") or {}).get("comfy") or {}
    return ReleaseMetadata(
        node_id=str(project.get("name", "")),
        publisher_id=str(comfy.get("PublisherId", "")),
        version=str(project.get("version", "")),
        display_name=str(comfy.get("DisplayName", "")),
        repository=str(urls.get("Repository", "")),
    )


def validate_release_metadata(repo_root: Path) -> ReleaseMetadata:
    """Validate the approved fork identity and public repository guidance."""

    metadata = load_release_metadata(repo_root / "pyproject.toml")
    expected = ReleaseMetadata(
        node_id=APPROVED_NODE_ID,
        publisher_id=APPROVED_PUBLISHER_ID,
        version=APPROVED_VERSION,
        display_name=APPROVED_DISPLAY_NAME,
        repository=APPROVED_REPOSITORY,
    )
    if metadata != expected:
        raise ValueError(f"release metadata does not match the approved identity: {metadata!r}")
    if _SEMVER.fullmatch(metadata.version) is None:
        raise ValueError(f"project version is not semantic: {metadata.version!r}")

    readme = (repo_root / "README.md").read_text(encoding="utf-8")
    required_public_text = (
        APPROVED_DISPLAY_NAME,
        f"git clone {APPROVED_REPOSITORY}.git",
    )
    missing = [text for text in required_public_text if text not in readme]
    if missing:
        raise ValueError(f"README is missing approved public identity text: {missing!r}")
    return metadata


def _git_output(repo_root: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ("git", "-C", str(repo_root), *arguments),
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ArchivePolicyError(f"Git manifest command failed: {detail}")
    return result.stdout


def _nul_paths(output: bytes) -> set[str]:
    return {
        item.decode("utf-8", errors="strict").replace("\\", "/")
        for item in output.split(b"\0")
        if item
    }


def release_manifest(repo_root: Path) -> tuple[str, ...]:
    """Return the exact official-CLI-style tracked manifest after `.comfyignore`."""

    repo_root = repo_root.resolve()
    ignore_path = repo_root / ".comfyignore"
    if not ignore_path.is_file():
        raise ArchivePolicyError(".comfyignore is required for fail-closed packaging")

    tracked = _nul_paths(_git_output(repo_root, "ls-files", "-z"))
    excluded = _nul_paths(
        _git_output(
            repo_root,
            "ls-files",
            "-c",
            "-i",
            "--exclude-from=.comfyignore",
            "-z",
        )
    )
    manifest = tuple(sorted(tracked - excluded))
    if not manifest:
        raise ArchivePolicyError("release manifest is empty")

    stage_rows = _git_output(repo_root, "ls-files", "-s", "-z").split(b"\0")
    modes: dict[str, str] = {}
    for row in stage_rows:
        if not row:
            continue
        metadata, raw_path = row.split(b"\t", 1)
        mode = metadata.split(b" ", 1)[0].decode("ascii")
        modes[raw_path.decode("utf-8").replace("\\", "/")] = mode

    for relative in manifest:
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts or "\\" in relative:
            raise ArchivePolicyError(f"unsafe Git path in release manifest: {relative!r}")
        if modes.get(relative) == "120000":
            # SECURITY: never dereference a tracked symlink into an external path.
            raise ArchivePolicyError(f"symlinks are forbidden in release archives: {relative!r}")
        if not (repo_root / Path(*pure.parts)).is_file():
            raise ArchivePolicyError(f"tracked release file is missing: {relative!r}")
    return manifest


def _zip_info(relative: str, mode: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = ((0o100000 | mode) & 0xFFFF) << 16
    return info


def build_release_archive(repo_root: Path, output_path: Path) -> ArchiveReport:
    """Build the exact deterministic candidate archive without network access."""

    repo_root = repo_root.resolve()
    output_path = output_path.resolve()
    if output_path.exists():
        raise FileExistsError(f"refusing to overwrite existing archive: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = release_manifest(repo_root)

    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            dir=output_path.parent,
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
        with zipfile.ZipFile(temp_path, "w", allowZip64=False) as archive:
            for relative in manifest:
                source = repo_root / Path(*PurePosixPath(relative).parts)
                archive.writestr(_zip_info(relative, 0o644), source.read_bytes())
        temp_path.replace(output_path)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)

    return inspect_release_archive(output_path)


def _validate_archive_name(name: str) -> str:
    normalized = name.rstrip("/")
    pure = PurePosixPath(normalized)
    lowered = normalized.casefold()
    if not normalized or name.endswith("/"):
        raise ArchivePolicyError(f"directory entries are forbidden: {name!r}")
    if pure.is_absolute() or ".." in pure.parts or "\\" in name:
        raise ArchivePolicyError(f"unsafe archive path: {name!r}")
    folded_parts = {part.casefold() for part in pure.parts}
    if folded_parts & _FORBIDDEN_INTERNAL_COMPONENTS:
        raise ArchivePolicyError(f"internal archive path component: {name!r}")
    if lowered in _FORBIDDEN_EXACT_NAMES:
        raise ArchivePolicyError(f"forbidden archive path: {name!r}")
    if any(lowered.startswith(prefix) for prefix in _FORBIDDEN_PREFIXES):
        raise ArchivePolicyError(f"forbidden archive prefix: {name!r}")
    leaf = pure.name.casefold()
    if leaf in _FORBIDDEN_POLICY_LEAVES or leaf.startswith("roadmap."):
        raise ArchivePolicyError(f"internal policy file is forbidden: {name!r}")
    if leaf == ".env" or leaf.startswith(".env.") or leaf.endswith(_FORBIDDEN_SUFFIXES):
        raise ArchivePolicyError(f"forbidden environment/log/media path: {name!r}")
    return normalized


def inspect_release_archive(archive_path: Path) -> ArchiveReport:
    """Inspect a ZIP for required runtime content, unsafe paths, and secrets."""

    archive_path = archive_path.resolve()
    names: set[str] = set()
    folded_names: set[str] = set()
    total_size = 0
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            for info in archive.infolist():
                name = _validate_archive_name(info.filename)
                folded = name.casefold()
                if folded in folded_names:
                    raise ArchivePolicyError(f"duplicate case-insensitive archive path: {name!r}")
                folded_names.add(folded)
                names.add(name)
                unix_mode = (info.external_attr >> 16) & 0o170000
                if unix_mode == 0o120000:
                    raise ArchivePolicyError(f"archive symlink is forbidden: {name!r}")
                if info.file_size > _MAX_ARCHIVE_FILE_BYTES:
                    raise ArchivePolicyError(f"archive member exceeds size limit: {name!r}")
                total_size += info.file_size
                if total_size > _MAX_ARCHIVE_TOTAL_BYTES:
                    raise ArchivePolicyError("archive exceeds total uncompressed size limit")
                content = archive.read(info)
                if any(pattern.search(content) for pattern in _SECRET_PATTERNS):
                    raise ArchivePolicyError(f"secret-like content detected in archive member: {name!r}")
    except (OSError, zipfile.BadZipFile) as exc:
        raise ArchivePolicyError(f"invalid release archive: {exc}") from exc

    missing = sorted(_REQUIRED_ARCHIVE_PATHS - names)
    if missing:
        raise ArchivePolicyError(f"release archive is missing required runtime paths: {missing!r}")
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    return ArchiveReport(
        path=archive_path,
        file_count=len(names),
        total_uncompressed_bytes=total_size,
        sha256=digest,
    )


RegistryReader = Callable[[str, bool], tuple[int, Any]]


def _registry_reader() -> RegistryReader:
    base_url = "https://api.comfy.org"

    class RejectRedirects(urllib.request.HTTPRedirectHandler):
        # SECURITY: never forward the Registry bearer token across a redirect.
        def redirect_request(self, request, file_pointer, code, message, headers, new_url):
            return None

    opener = urllib.request.build_opener(RejectRedirects())

    def read_json(path: str, token_required: bool) -> tuple[int, Any]:
        if token_required:
            raise ValueError("public Registry preflight cannot send credentials")
        headers = {"Accept": "application/json"}
        request = urllib.request.Request(f"{base_url}{path}", headers=headers, method="GET")
        try:
            with opener.open(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
                return int(response.status), payload
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return 404, None
            raise RuntimeError(f"Registry read failed with HTTP {exc.code} for {path}") from exc
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Registry read failed for {path}") from exc

    return read_json


def preflight_registry(
    publisher_id: str,
    node_id: str,
    token: str,
    *,
    read_json: RegistryReader | None = None,
) -> dict[str, Any]:
    """Perform credential-presence and public ownership/collision checks.

    Publisher-scoped Registry keys are publish credentials, not general user bearer
    tokens. The returned payload is deliberately content-free, and the key is never
    sent to read APIs or returned/logged. The publish endpoint remains the authority.
    """

    if _REGISTRY_ID.fullmatch(publisher_id) is None:
        raise ValueError("publisher ID is not a valid lowercase Registry identifier")
    if _REGISTRY_ID.fullmatch(node_id) is None:
        raise ValueError("node ID is not a valid lowercase Registry identifier")
    if not token:
        raise PermissionError("Registry token is required for publish preflight")
    # SECURITY: do not send a publisher-scoped publish key to user-identity APIs;
    # the official publish endpoint validates the key's publisher authorization.
    reader = read_json or _registry_reader()

    global_status, global_payload = reader(f"/nodes/{node_id}", False)
    if global_status == 200 and isinstance(global_payload, dict):
        publisher = global_payload.get("publisher") or {}
        if publisher.get("id") != publisher_id:
            raise PermissionError("global node ownership does not match approved publisher")
        state = "owned"
    elif global_status == 404:
        state = "available"
    else:
        raise PermissionError("approved node ID is already owned by another publisher")

    return {
        "credential_configured": True,
        "publisher_id": publisher_id,
        "node_id": node_id,
        "node_state": state,
    }

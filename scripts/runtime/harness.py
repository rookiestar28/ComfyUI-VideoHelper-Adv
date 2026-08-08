"""Containment and launch primitives for the owned ComfyUI runtime harness."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


class RuntimeHarnessError(RuntimeError):
    """Base error for an invalid or unsafe runtime-harness operation."""


class ContainmentError(RuntimeHarnessError):
    """Raised when a runtime path is not contained by the workspace."""


class TrustedHostError(RuntimeHarnessError):
    """Raised when the selected ComfyUI host cannot be trusted for execution."""


class PluginSnapshotError(RuntimeHarnessError):
    """Raised when the contained plugin snapshot cannot be created safely."""


class ProcessOwnershipError(RuntimeHarnessError):
    """Raised when process lifecycle control would target an unowned process."""


def _resolved(path: os.PathLike[str] | str) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _assert_contained(root: Path, candidate: Path, label: str) -> None:
    """Reject lexical, cross-drive, and resolved symlink/junction escapes."""
    resolved_root = _resolved(root)
    resolved_candidate = _resolved(candidate)
    try:
        common = Path(os.path.commonpath((str(resolved_root), str(resolved_candidate))))
    except ValueError as exc:
        raise ContainmentError(f"{label} is on a different filesystem root") from exc
    if os.path.normcase(str(common)) != os.path.normcase(str(resolved_root)):
        raise ContainmentError(f"{label} must stay inside the workspace")


def _is_contained(root: Path, candidate: Path) -> bool:
    try:
        _assert_contained(root, candidate, "path")
    except ContainmentError:
        return False
    return True


@dataclass(frozen=True)
class RuntimeLayout:
    sandbox: Path
    base: Path
    input: Path
    output: Path
    temp: Path
    user: Path
    results: Path
    logs: Path

    @classmethod
    def create(cls, workspace: Path, sandbox: Path) -> "RuntimeLayout":
        workspace = _resolved(workspace)
        if not workspace.is_dir():
            raise ContainmentError("workspace must be an existing directory")

        # SECURITY: resolve before creation so junction/symlink aliases cannot escape containment.
        _assert_contained(workspace, sandbox, "runtime sandbox")
        resolved_sandbox = _resolved(sandbox)
        layout = cls(
            sandbox=resolved_sandbox,
            base=resolved_sandbox / "base",
            input=resolved_sandbox / "input",
            output=resolved_sandbox / "output",
            temp=resolved_sandbox / "temp",
            user=resolved_sandbox / "user",
            results=resolved_sandbox / "results",
            logs=resolved_sandbox / "logs",
        )
        for label, path in layout.as_dict().items():
            _assert_contained(workspace, path, f"runtime {label}")
            path.mkdir(parents=True, exist_ok=True)
            _assert_contained(workspace, path, f"resolved runtime {label}")
        return layout

    def as_dict(self) -> dict[str, Path]:
        return {
            "sandbox": self.sandbox,
            "base": self.base,
            "input": self.input,
            "output": self.output,
            "temp": self.temp,
            "user": self.user,
            "results": self.results,
            "logs": self.logs,
        }


@dataclass(frozen=True)
class TrustedHost:
    root: Path
    interpreter: Path
    main: Path


def validate_trusted_host(
    workspace: Path,
    comfyui_root: Path,
    interpreter: Path,
) -> TrustedHost:
    workspace = _resolved(workspace)
    root = _resolved(comfyui_root)
    python = _resolved(interpreter)
    reference_root = workspace / "reference"

    # SECURITY: reference repositories are untrusted, read-only evidence sources.
    if _is_contained(reference_root, root):
        raise TrustedHostError("ComfyUI root under reference is forbidden")
    if _is_contained(reference_root, python):
        raise TrustedHostError("interpreter under reference is forbidden")

    main = root / "main.py"
    if not root.is_dir() or not main.is_file():
        raise TrustedHostError("trusted ComfyUI root must contain main.py")
    if not python.is_file():
        raise TrustedHostError("trusted ComfyUI interpreter must be an existing file")
    resolved_main = main.resolve()
    # SECURITY: a trusted checkout must not redirect its executable entry point elsewhere.
    if not _is_contained(root, resolved_main) or _is_contained(reference_root, resolved_main):
        raise TrustedHostError("trusted ComfyUI main.py must stay inside its source root")
    return TrustedHost(root=root, interpreter=python, main=resolved_main)


def build_comfyui_command(
    host: TrustedHost,
    layout: RuntimeLayout,
    port: int,
    additional_whitelist: Sequence[str] = (),
) -> list[str]:
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    whitelist = ["ComfyUI-VideoHelper_Adv", *additional_whitelist]
    if len(whitelist) != len(set(whitelist)) or not all(
        name and all(character.isalnum() or character in "-_." for character in name)
        for name in whitelist
    ):
        raise ValueError("custom-node whitelist names must be unique and filename-safe")
    return [
        str(host.interpreter),
        str(host.main),
        "--listen",
        "127.0.0.1",
        "--port",
        str(port),
        "--base-directory",
        str(layout.base),
        "--input-directory",
        str(layout.input),
        "--output-directory",
        str(layout.output),
        "--temp-directory",
        str(layout.temp),
        "--user-directory",
        str(layout.user),
        # CRITICAL: ComfyUI's database default is rooted beside host source, not --user-directory.
        "--database-url",
        "sqlite:///:memory:",
        "--disable-auto-launch",
        "--cpu",
        "--disable-all-custom-nodes",
        "--whitelist-custom-nodes",
        *whitelist,
    ]


_RUNTIME_PLUGIN_ALLOWLIST = (
    "__init__.py",
    "videohelpersuite",
    "video_formats",
    "web",
)


def _runtime_source_files(plugin_root: Path) -> list[Path]:
    files = []
    for relative_name in _RUNTIME_PLUGIN_ALLOWLIST:
        source = plugin_root / relative_name
        if not source.exists():
            raise PluginSnapshotError(f"required runtime source is missing: {relative_name}")
        _validate_snapshot_source(plugin_root, source)
        if source.is_file():
            files.append(source)
            continue
        files.extend(
            path
            for path in source.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix not in {".pyc", ".pyo"}
        )
    return sorted(files, key=lambda path: path.relative_to(plugin_root).as_posix())


def runtime_plugin_sha256(plugin_root: Path) -> str:
    """Digest exactly the source files copied into an owned runtime sandbox."""
    plugin_root = _resolved(plugin_root)
    digest = hashlib.sha256()
    for path in _runtime_source_files(plugin_root):
        relative = path.relative_to(plugin_root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(path.stat().st_size.to_bytes(8, "big"))
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _is_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    file_attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse_flag and file_attributes & reparse_flag)


def _validate_snapshot_source(plugin_root: Path, source: Path) -> None:
    paths = (source,) if source.is_file() else (source, *source.rglob("*"))
    for path in paths:
        if _is_reparse_point(path):
            raise PluginSnapshotError(f"runtime snapshot source contains a reparse point: {path.name}")
        if not _is_contained(plugin_root, path):
            raise PluginSnapshotError("runtime snapshot source escapes the plugin root")


def copy_runtime_plugin(plugin_root: Path, layout: RuntimeLayout) -> Path:
    """Copy only runtime-required public files into the contained custom_nodes root."""
    plugin_root = _resolved(plugin_root)
    target = layout.base / "custom_nodes" / "ComfyUI-VideoHelper_Adv"
    if target.exists():
        raise PluginSnapshotError("runtime plugin snapshot target already exists")

    sources: list[tuple[Path, Path]] = []
    for relative_name in _RUNTIME_PLUGIN_ALLOWLIST:
        source = plugin_root / relative_name
        if not source.exists():
            raise PluginSnapshotError(f"required runtime source is missing: {relative_name}")
        _validate_snapshot_source(plugin_root, source)
        sources.append((source, target / relative_name))

    target.parent.mkdir(parents=True, exist_ok=True)
    target.mkdir()
    try:
        for source, destination in sources:
            if source.is_dir():
                shutil.copytree(
                    source,
                    destination,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
                )
            else:
                shutil.copy2(source, destination)
    except Exception:
        # SAFETY: cleanup is limited to the newly created, prevalidated sandbox target.
        shutil.rmtree(target)
        raise
    return target


class OwnedComfyUIProcess:
    """Lifecycle wrapper that can stop only the process it created."""

    def __init__(
        self,
        command: Sequence[str],
        cwd: Path,
        *,
        popen_factory: Callable[..., subprocess.Popen] = subprocess.Popen,
        stop_timeout: float = 10.0,
        env: dict[str, str] | None = None,
    ) -> None:
        self._command = list(command)
        self._cwd = Path(cwd)
        self._popen_factory = popen_factory
        self._stop_timeout = stop_timeout
        self._env = dict(env) if env is not None else None
        self._process = None

    @property
    def pid(self) -> int:
        if self._process is None:
            raise ProcessOwnershipError("owned ComfyUI process has not started")
        return self._process.pid

    def poll(self):
        if self._process is None:
            raise ProcessOwnershipError("owned ComfyUI process has not started")
        return self._process.poll()

    def start(self) -> "OwnedComfyUIProcess":
        if self._process is not None:
            raise ProcessOwnershipError("owned ComfyUI process may only be started once")
        kwargs = {
            "cwd": str(self._cwd),
            "stdin": subprocess.DEVNULL,
            # SECURITY: host logs can contain private absolute paths; do not persist them.
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.STDOUT,
        }
        if self._env is not None:
            kwargs["env"] = self._env
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        self._process = self._popen_factory(self._command, **kwargs)
        return self

    def stop(self) -> None:
        if self._process is None:
            raise ProcessOwnershipError("refusing to stop a process not created by this harness")
        if self._process.poll() is not None:
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=self._stop_timeout)
        except (subprocess.TimeoutExpired, TimeoutError):
            self._process.kill()
            self._process.wait(timeout=self._stop_timeout)

    def __enter__(self) -> "OwnedComfyUIProcess":
        return self.start()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.stop()

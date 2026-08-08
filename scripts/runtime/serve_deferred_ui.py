"""Serve a temporary owned ComfyUI host for a bounded production-UI probe."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sys
import time
import uuid
from pathlib import Path
from typing import Sequence


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.runtime.api import LoopbackComfyApiClient
from scripts.runtime.harness import (
    OwnedComfyUIProcess,
    RuntimeLayout,
    build_comfyui_command,
    copy_runtime_plugin,
    runtime_plugin_sha256,
    validate_trusted_host,
)


class DeferredUIServerError(RuntimeError):
    """Raised when the bounded UI host cannot be served safely."""


def _contained(root: Path, candidate: Path) -> bool:
    root = root.resolve(strict=False)
    candidate = candidate.resolve(strict=False)
    try:
        common = os.path.commonpath((str(root), str(candidate)))
    except ValueError:
        return False
    return os.path.normcase(common) == os.path.normcase(str(root))


def _reserve_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--comfyui-root", type=Path, required=True)
    parser.add_argument("--comfyui-python", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--stop-file", type=Path, required=True)
    parser.add_argument("--startup-timeout", type=float, default=120.0)
    parser.add_argument("--max-seconds", type=float, default=300.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = Path(__file__).resolve().parents[2]
    workspace = args.workspace.resolve(strict=True)
    if workspace != repo_root:
        raise DeferredUIServerError("workspace must be the repository root")
    manifest = args.manifest if args.manifest.is_absolute() else workspace / args.manifest
    stop_file = args.stop_file if args.stop_file.is_absolute() else workspace / args.stop_file
    sandbox = workspace / ".tmp" / "runtime" / f"deferred-ui-{uuid.uuid4().hex}"
    for label, path in (("manifest", manifest), ("stop file", stop_file), ("sandbox", sandbox)):
        if not _contained(workspace, path):
            raise DeferredUIServerError(f"{label} must stay inside the workspace")
    if stop_file.exists():
        raise DeferredUIServerError("stop file must not exist before launch")

    layout = None
    try:
        layout = RuntimeLayout.create(workspace, sandbox)
        trusted_host = validate_trusted_host(workspace, args.comfyui_root, args.comfyui_python)
        copy_runtime_plugin(repo_root, layout)
        port = _reserve_loopback_port()
        command = build_comfyui_command(trusted_host, layout, port)
        client = LoopbackComfyApiClient(f"http://127.0.0.1:{port}")
        manifest.parent.mkdir(parents=True, exist_ok=True)
        stop_file.parent.mkdir(parents=True, exist_ok=True)

        with OwnedComfyUIProcess(command, trusted_host.root) as owned:
            client.wait_until_ready(owned, timeout=args.startup_timeout)
            document = {
                "schema_version": 1,
                "status": "ready",
                "port": port,
                "owned_pid": owned.pid,
                "plugin_runtime_sha256": runtime_plugin_sha256(repo_root),
            }
            manifest.write_text(
                json.dumps(document, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            print(
                f"deferred UI host ready: port={port} pid={owned.pid} "
                f"manifest={manifest.relative_to(workspace).as_posix()}",
                flush=True,
            )
            deadline = time.monotonic() + args.max_seconds
            while time.monotonic() < deadline and not stop_file.is_file():
                if owned.poll() is not None:
                    raise DeferredUIServerError("owned UI host exited before stop signal")
                time.sleep(0.25)
            if not stop_file.is_file():
                raise DeferredUIServerError("owned UI host timed out without stop signal")
        document["status"] = "stopped"
        manifest.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return 0
    finally:
        if stop_file.exists() and _contained(workspace, stop_file):
            stop_file.unlink()
        if layout is not None and layout.sandbox.exists():
            if not _contained(workspace, layout.sandbox) or layout.sandbox == workspace:
                raise DeferredUIServerError("refusing to remove an unsafe sandbox path")
            shutil.rmtree(layout.sandbox)


if __name__ == "__main__":
    raise SystemExit(main())

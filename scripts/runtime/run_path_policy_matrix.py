"""Run filesystem/URL policy checks in owned, contained ComfyUI hosts."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import struct
import sys
import uuid
import zlib
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.runtime.api import ComfyApiError, LoopbackComfyApiClient
from scripts.runtime.harness import (
    OwnedComfyUIProcess,
    RuntimeLayout,
    build_comfyui_command,
    copy_runtime_plugin,
    runtime_plugin_sha256,
    validate_trusted_host,
)


class PathMatrixError(RuntimeError):
    """Raised when path-policy runtime evidence is incomplete or unsafe."""


_POLICY_ENVIRONMENT = {
    "VHS_DEPLOYMENT_PROFILE",
    "VHS_PATH_POLICY",
    "VHS_EXTERNAL_READ_ROOTS",
    "VHS_URL_POLICY",
    "VHS_STRICT_PATHS",
}


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


def _git_commit(root: Path) -> str:
    import subprocess

    completed = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    commit = completed.stdout.strip()
    if completed.returncode != 0 or len(commit) != 40:
        raise PathMatrixError("source commit could not be identified")
    return commit


def _policy_environment(overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    environment = {
        name: value
        for name, value in os.environ.items()
        if name not in _POLICY_ENVIRONMENT
    }
    environment.update(overrides or {})
    return environment


def _request(port: int, path: str, query: Mapping[str, str]) -> tuple[int, bytes]:
    opener = build_opener(ProxyHandler({}))
    url = f"http://127.0.0.1:{port}{path}?{urlencode(query)}"
    request = Request(url, headers={"Accept": "*/*"}, method="GET")
    try:
        with opener.open(request, timeout=60) as response:
            return int(response.status), response.read()
    except HTTPError as exc:
        return int(exc.code), exc.read()


def _history_succeeded(history: Mapping[str, Any]) -> bool:
    status = history.get("status", {})
    return (
        isinstance(status, Mapping)
        and status.get("completed") is True
        and status.get("status_str") == "success"
    )


def _history_failed(history: Mapping[str, Any]) -> bool:
    status = history.get("status", {})
    return isinstance(status, Mapping) and status.get("status_str") == "error"


def _submit_expect_rejection(
    client: LoopbackComfyApiClient,
    fixture: Mapping[str, Any],
    timeout: float,
) -> bool:
    try:
        prompt_id = client.submit_prompt(fixture)
        return _history_failed(client.wait_for_history(prompt_id, timeout=timeout))
    except ComfyApiError:
        return True


def _load_images_prompt(directory: Path, prefix: str) -> dict[str, Any]:
    return {
        "prompt": {
            "1": {
                "class_type": "VHS_LoadImagesPath",
                "inputs": {
                    "directory": str(directory),
                    "image_load_cap": 1,
                    "skip_first_images": 0,
                    "select_every_nth": 1,
                },
            },
            "2": {
                "class_type": "VHS_VideoCombine",
                "inputs": {
                    "images": ["1", 0],
                    "frame_rate": 8,
                    "loop_count": 0,
                    "filename_prefix": prefix,
                    "format": "video/h264-mp4",
                    "pingpong": False,
                    "save_output": True,
                    "save_metadata": False,
                },
            },
        }
    }


def _synthetic_png(width: int = 64, height: int = 48) -> bytes:
    def chunk(name: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + name
            + payload
            + struct.pack(">I", zlib.crc32(name + payload) & 0xFFFFFFFF)
        )

    scanline = b"\x00" + (b"\x20\x60\xa0" * width)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(scanline * height))
        + chunk(b"IEND", b"")
    )


def _prune_prompt(inside: Path, outside: Path) -> dict[str, Any]:
    return {
        "prompt": {
            "1": {
                "class_type": "VHS_RuntimeFilenames",
                "inputs": {
                    "inside_path": str(inside),
                    "outside_path": str(outside),
                },
            },
            "2": {
                "class_type": "VHS_PruneOutputs",
                "inputs": {"filenames": ["1", 0], "options": "All"},
            },
        }
    }


def _write_support_node(layout: RuntimeLayout) -> None:
    root = layout.base / "custom_nodes" / "VHS-RuntimeHarness"
    root.mkdir(parents=True)
    source = '''class RuntimeFilenames:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"inside_path": ("STRING",), "outside_path": ("STRING",)}}

    RETURN_TYPES = ("VHS_FILENAMES",)
    FUNCTION = "build"
    CATEGORY = "VHS Runtime Validation"

    def build(self, inside_path, outside_path):
        return ((True, [inside_path, outside_path]),)


NODE_CLASS_MAPPINGS = {"VHS_RuntimeFilenames": RuntimeFilenames}
'''
    (root / "__init__.py").write_text(source, encoding="utf-8", newline="\n")


def _run_host(
    trusted_host,
    layout: RuntimeLayout,
    environment: Mapping[str, str],
    callback,
    startup_timeout: float,
):
    port = _reserve_loopback_port()
    command = build_comfyui_command(
        trusted_host,
        layout,
        port,
        additional_whitelist=("VHS-RuntimeHarness",),
    )
    owned = OwnedComfyUIProcess(
        command,
        trusted_host.root,
        env=dict(environment),
    )
    client = LoopbackComfyApiClient(f"http://127.0.0.1:{port}")
    with owned:
        client.wait_until_ready(owned, timeout=startup_timeout)
        return callback(client, port)


def _default_checks(
    client: LoopbackComfyApiClient,
    port: int,
    layout: RuntimeLayout,
    outside_directory: Path,
    timeout: float,
) -> dict[str, bool]:
    checks = {}
    status, body = _request(
        port,
        "/vhs/getpath",
        {"path": str(layout.input / "images"), "extensions": "png"},
    )
    checks["host_root_list_allowed"] = status == 200 and b"frame.png" in body

    status, body = _request(
        port,
        "/vhs/getpath",
        {"path": str(outside_directory), "extensions": "png"},
    )
    checks["outside_list_denied_without_disclosure"] = (
        status == 403
        and str(outside_directory).encode() not in body
        and b"private.png" not in body
    )

    allowed_prompt = _load_images_prompt(
        layout.input / "images",
        "runtime/path_policy/allowed",
    )
    allowed_id = client.submit_prompt(allowed_prompt)
    allowed_history = client.wait_for_history(allowed_id, timeout=timeout)
    generated = sorted((layout.output / "runtime" / "path_policy").glob("*.mp4"))
    checks["host_root_load_and_write_allowed"] = (
        _history_succeeded(allowed_history) and len(generated) == 1
    )

    checks["outside_load_denied"] = _submit_expect_rejection(
        client,
        _load_images_prompt(outside_directory, "runtime/path_policy/denied"),
        timeout,
    )

    if generated:
        relative = generated[0].relative_to(layout.output)
        preview_query = {
            "filename": relative.name,
            "subfolder": relative.parent.as_posix(),
            "type": "output",
        }
        status, _body = _request(
            port,
            "/vhs/viewvideo",
            preview_query,
        )
        checks["authorized_preview_allowed"] = status == 200
        if status != 200:
            print(f"path policy diagnostic: authorized_preview_status={status}")
        status, _body = _request(
            port,
            "/vhs/queryvideo",
            preview_query,
        )
        checks["authorized_query_allowed"] = status == 200
        if status != 200:
            print(f"path policy diagnostic: authorized_query_status={status}")
    else:
        checks["authorized_preview_allowed"] = False
        checks["authorized_query_allowed"] = False

    status, body = _request(
        port,
        "/vhs/viewvideo",
        {"filename": str(outside_directory / "private.png"), "type": "path"},
    )
    checks["outside_preview_denied_without_disclosure"] = (
        status == 403 and str(outside_directory).encode() not in body
    )

    inside = layout.output / "runtime" / "path_policy" / "prune-inside.tmp"
    outside = outside_directory / "prune-outside.tmp"
    inside.write_text("inside", encoding="utf-8")
    outside.write_text("outside", encoding="utf-8")
    prune_id = client.submit_prompt(_prune_prompt(inside, outside))
    prune_history = client.wait_for_history(prune_id, timeout=timeout)
    checks["prune_validates_all_before_delete"] = (
        _history_failed(prune_history) and inside.is_file() and outside.is_file()
    )
    return checks


def _allowlist_checks(
    _client: LoopbackComfyApiClient,
    port: int,
    external_directory: Path,
) -> dict[str, bool]:
    status, body = _request(
        port,
        "/vhs/getpath",
        {"path": str(external_directory), "extensions": "png"},
    )
    return {
        "external_allowlist_read_allowed": status == 200 and b"external.png" in body,
    }


def _remote_checks(
    _client: LoopbackComfyApiClient,
    port: int,
) -> dict[str, bool]:
    sensitive_url = "https://example.com/video.mp4?private=value"
    status, body = _request(
        port,
        "/vhs/viewvideo",
        {"filename": sensitive_url},
    )
    return {
        "remote_url_disabled_without_disclosure": (
            status == 502
            and sensitive_url.encode() not in body
            and b"private=value" not in body
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--comfyui-root", required=True, type=Path)
    parser.add_argument("--comfyui-python", required=True, type=Path)
    parser.add_argument("--workspace", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument(
        "--result-file",
        type=Path,
        default=Path(".tmp/runtime_results/path_policy_results.json"),
    )
    parser.add_argument("--sandbox", type=Path)
    parser.add_argument("--keep-sandbox", action="store_true")
    parser.add_argument("--startup-timeout", type=float, default=180.0)
    parser.add_argument("--scenario-timeout", type=float, default=180.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = Path(__file__).resolve().parents[2]
    workspace = args.workspace.resolve(strict=True)
    if workspace != repo_root:
        raise PathMatrixError("workspace must be the repository root")

    sandbox = args.sandbox or workspace / ".tmp" / "runtime" / f"path-{uuid.uuid4().hex}"
    if not sandbox.is_absolute():
        sandbox = workspace / sandbox
    result_file = args.result_file
    if not result_file.is_absolute():
        result_file = workspace / result_file
    if not _contained(workspace, sandbox) or not _contained(workspace, result_file):
        raise PathMatrixError("sandbox and result file must stay inside the workspace")

    layout = None
    try:
        layout = RuntimeLayout.create(workspace, sandbox)
        if _contained(layout.sandbox, result_file):
            raise PathMatrixError("result file must be outside the disposable sandbox")
        result_file.parent.mkdir(parents=True, exist_ok=True)
        trusted_host = validate_trusted_host(
            workspace,
            args.comfyui_root,
            args.comfyui_python,
        )
        copy_runtime_plugin(repo_root, layout)
        _write_support_node(layout)

        input_images = layout.input / "images"
        input_images.mkdir()
        outside_directory = layout.user / "outside"
        outside_directory.mkdir()
        external_directory = layout.user / "external"
        external_directory.mkdir()
        png = _synthetic_png()
        (input_images / "frame.png").write_bytes(png)
        (outside_directory / "private.png").write_bytes(png)
        (external_directory / "external.png").write_bytes(png)

        checks = {}
        checks.update(_run_host(
            trusted_host,
            layout,
            _policy_environment(),
            lambda client, port: _default_checks(
                client,
                port,
                layout,
                outside_directory,
                args.scenario_timeout,
            ),
            args.startup_timeout,
        ))
        checks.update(_run_host(
            trusted_host,
            layout,
            _policy_environment({
                "VHS_PATH_POLICY": "allowlist",
                "VHS_EXTERNAL_READ_ROOTS": str(external_directory),
            }),
            lambda client, port: _allowlist_checks(client, port, external_directory),
            args.startup_timeout,
        ))
        checks.update(_run_host(
            trusted_host,
            layout,
            _policy_environment({"VHS_DEPLOYMENT_PROFILE": "remote_restricted"}),
            _remote_checks,
            args.startup_timeout,
        ))

        document = {
            "schema_version": 1,
            "status": "passed" if checks and all(checks.values()) else "failed",
            "checks": checks,
            "host_commit": _git_commit(trusted_host.root),
            "plugin_commit": _git_commit(repo_root),
            "plugin_runtime_sha256": runtime_plugin_sha256(repo_root),
        }
        result_file.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(
            f"path policy matrix: status={document['status']} "
            f"checks={sum(checks.values())}/{len(checks)} "
            f"result={result_file.relative_to(workspace).as_posix()}"
        )
        return 0 if document["status"] == "passed" else 1
    finally:
        if layout is not None and not args.keep_sandbox and layout.sandbox.exists():
            if not _contained(workspace, layout.sandbox) or layout.sandbox == workspace:
                raise PathMatrixError("refusing to remove an unsafe sandbox path")
            shutil.rmtree(layout.sandbox)


if __name__ == "__main__":
    raise SystemExit(main())

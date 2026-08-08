"""Validate deferred-path closure in an owned, contained ComfyUI host."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import uuid
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


class DeferredPathMatrixError(RuntimeError):
    """Raised when contained deferred-path evidence is incomplete or unsafe."""


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
    completed = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    commit = completed.stdout.strip()
    if completed.returncode != 0 or len(commit) != 40:
        raise DeferredPathMatrixError("source commit could not be identified")
    return commit


def _request(
    port: int,
    method: str,
    path: str,
    payload: Mapping[str, Any] | None = None,
) -> tuple[int, bytes]:
    opener = build_opener(ProxyHandler({}))
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with opener.open(request, timeout=60) as response:
            return int(response.status), response.read()
    except HTTPError as exc:
        return int(exc.code), exc.read()


def _json_body(payload: bytes) -> Any:
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _all_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from _all_strings(key)
            yield from _all_strings(item)
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        for item in value:
            yield from _all_strings(item)


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


def _video_prompt(prefix: str, loop_count: int) -> dict[str, Any]:
    return {
        "prompt": {
            "1": {
                "class_type": "EmptyImage",
                "inputs": {"width": 64, "height": 48, "batch_size": 2, "color": 0},
            },
            "2": {
                "class_type": "VHS_VideoCombine",
                "inputs": {
                    "images": ["1", 0],
                    "frame_rate": 8,
                    "loop_count": loop_count,
                    "filename_prefix": prefix,
                    "format": "video/ffmpeg-gif",
                    "pingpong": False,
                    "save_output": True,
                    "save_metadata": False,
                },
            },
        }
    }


def _write_support_node(layout: RuntimeLayout) -> None:
    root = layout.base / "custom_nodes" / "VHS-DeferredRuntime"
    root.mkdir(parents=True)
    source = '''class RuntimeStringSink:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"value": ("STRING", {"forceInput": True})}}

    RETURN_TYPES = ()
    FUNCTION = "consume"
    OUTPUT_NODE = True
    CATEGORY = "VHS Runtime Validation"

    def consume(self, value):
        return {}


NODE_CLASS_MAPPINGS = {"VHS_RuntimeStringSink": RuntimeStringSink}
'''
    (root / "__init__.py").write_text(source, encoding="utf-8", newline="\n")


def _run_prompt(client: LoopbackComfyApiClient, fixture: Mapping[str, Any], timeout: float) -> Mapping[str, Any]:
    prompt_id = client.submit_prompt(fixture)
    return client.wait_for_history(prompt_id, timeout=timeout)


def _write_video(path: Path, width: int, height: int) -> None:
    command = [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c=blue:s={width}x{height}:d=0.25",
        "-pix_fmt",
        "yuv420p",
        str(path),
    ]
    completed = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0 or not path.is_file():
        raise DeferredPathMatrixError("failed to create contained synthetic video")


def _query(port: int, filename: str) -> tuple[int, Any]:
    query = urlencode({"filename": filename, "type": "output"})
    status, payload = _request(port, "GET", f"/vhs/queryvideo?{query}")
    return status, _json_body(payload)


def _checks(
    client: LoopbackComfyApiClient,
    port: int,
    layout: RuntimeLayout,
    scenario_timeout: float,
) -> dict[str, bool]:
    checks: dict[str, bool] = {}

    schema_status, schema_payload = _request(port, "GET", "/object_info/VHS_SelectLatest")
    schema = _json_body(schema_payload)
    checks["select_latest_schema_available"] = (
        schema_status == 200
        and isinstance(schema, Mapping)
        and "VHS_SelectLatest" in schema
    )

    rejection_status, rejection_payload = _request(
        port,
        "POST",
        "/prompt",
        {
            "prompt": {
                "1": {
                    "class_type": "VHS_SelectLatest",
                    "inputs": {
                        "filename_prefix": "output/contained",
                        "filename_postfix": ".mp4",
                    },
                },
                "2": {
                    "class_type": "VHS_RuntimeStringSink",
                    "inputs": {"value": ["1", 0]},
                },
            }
        },
    )
    rejection = _json_body(rejection_payload)
    rejection_text = " ".join(_all_strings(rejection)).lower()
    checks["select_latest_safe_validation"] = (
        rejection_status == 400
        and "frontend-only virtual node" in rejection_text
        and "assert" not in rejection_text
    )

    extensions_status, extensions_payload = _request(port, "GET", "/extensions")
    extensions = _json_body(extensions_payload)
    module_names = (
        "pasteHandler.js",
        "selectLatest.js",
        "pathWidgets.js",
    )
    core_entry = next((
        item for item in extensions
        if isinstance(item, str) and item.endswith("/VHS.core.js")
    ), None) if isinstance(extensions, list) else None
    served = []
    if core_entry:
        module_root = core_entry.rsplit("/", 1)[0]
        for name in module_names:
            status, body = _request(port, "GET", f"{module_root}/{name}")
            served.append(status == 200 and bool(body))
    checks["frontend_modules_registered"] = (
        extensions_status == 200 and core_entry is not None and all(served)
    )

    sample = layout.output / "query-cache.mp4"
    _write_video(sample, 64, 48)
    first_status, first = _query(port, sample.name)
    second_status, second = _query(port, sample.name)
    first_size = first.get("source", {}).get("size") if isinstance(first, Mapping) else None
    second_size = second.get("source", {}).get("size") if isinstance(second, Mapping) else None
    checks["query_cache_stable_hit"] = (
        first_status == 200
        and second_status == 200
        and first_size == [64, 48]
        and second_size == first_size
    )

    _write_video(sample, 80, 60)
    changed_status, changed = _query(port, sample.name)
    changed_size = changed.get("source", {}).get("size") if isinstance(changed, Mapping) else None
    checks["query_cache_file_state_invalidation"] = changed_status == 200 and changed_size == [80, 60]

    complex_history = _run_prompt(client, _video_prompt("deferred/complex-only", 0), scenario_timeout)
    checks["complex_filter_only_succeeds"] = _history_succeeded(complex_history)

    try:
        mixed_history = _run_prompt(client, _video_prompt("deferred/mixed-rejected", 1), scenario_timeout)
        mixed_failed = _history_failed(mixed_history)
        mixed_text = " ".join(_all_strings(mixed_history)).lower()
        mixed_reason = "filter_complex" in mixed_text and "unsupported" in mixed_text
    except ComfyApiError:
        mixed_failed = False
        mixed_reason = False
    checks["mixed_filter_rejected"] = mixed_failed and mixed_reason
    return checks


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--comfyui-root", type=Path, required=True)
    parser.add_argument("--comfyui-python", type=Path, required=True)
    parser.add_argument("--sandbox", type=Path)
    parser.add_argument(
        "--result-file",
        type=Path,
        default=Path(".tmp/runtime_results/deferred_paths_results.json"),
    )
    parser.add_argument("--startup-timeout", type=float, default=120.0)
    parser.add_argument("--scenario-timeout", type=float, default=120.0)
    parser.add_argument("--keep-sandbox", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = Path(__file__).resolve().parents[2]
    workspace = args.workspace.resolve(strict=True)
    if workspace != repo_root:
        raise DeferredPathMatrixError("workspace must be the repository root")

    sandbox = args.sandbox or workspace / ".tmp" / "runtime" / f"deferred-{uuid.uuid4().hex}"
    if not sandbox.is_absolute():
        sandbox = workspace / sandbox
    result_file = args.result_file
    if not result_file.is_absolute():
        result_file = workspace / result_file
    if not _contained(workspace, sandbox) or not _contained(workspace, result_file):
        raise DeferredPathMatrixError("sandbox and result file must stay inside the workspace")

    layout = None
    try:
        layout = RuntimeLayout.create(workspace, sandbox)
        if _contained(layout.sandbox, result_file):
            raise DeferredPathMatrixError("result file must be outside the disposable sandbox")
        result_file.parent.mkdir(parents=True, exist_ok=True)
        trusted_host = validate_trusted_host(
            workspace,
            args.comfyui_root,
            args.comfyui_python,
        )
        copy_runtime_plugin(repo_root, layout)
        _write_support_node(layout)

        port = _reserve_loopback_port()
        command = build_comfyui_command(
            trusted_host,
            layout,
            port,
            additional_whitelist=("VHS-DeferredRuntime",),
        )
        client = LoopbackComfyApiClient(f"http://127.0.0.1:{port}")
        with OwnedComfyUIProcess(command, trusted_host.root) as owned:
            client.wait_until_ready(owned, timeout=args.startup_timeout)
            checks = _checks(client, port, layout, args.scenario_timeout)

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
            f"deferred paths matrix: status={document['status']} "
            f"checks={sum(checks.values())}/{len(checks)} "
            f"result={result_file.relative_to(workspace).as_posix()}"
        )
        return 0 if document["status"] == "passed" else 1
    finally:
        if layout is not None and not args.keep_sandbox and layout.sandbox.exists():
            if not _contained(workspace, layout.sandbox) or layout.sandbox == workspace:
                raise DeferredPathMatrixError("refusing to remove an unsafe sandbox path")
            shutil.rmtree(layout.sandbox)


if __name__ == "__main__":
    raise SystemExit(main())

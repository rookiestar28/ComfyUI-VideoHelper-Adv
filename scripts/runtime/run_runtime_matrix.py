"""Owned, workspace-contained ComfyUI runtime-matrix runner."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable, Mapping, Sequence


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.runtime.api import ComfyApiError, LoopbackComfyApiClient
from scripts.runtime.harness import (
    OwnedComfyUIProcess,
    RuntimeHarnessError,
    RuntimeLayout,
    build_comfyui_command,
    copy_runtime_plugin,
    runtime_plugin_sha256,
    validate_trusted_host,
)


class ResultSafetyError(RuntimeError):
    """Raised when scenario selection or result evidence is incomplete or unsafe."""


def build_argument_parser() -> argparse.ArgumentParser:
    repo_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(
        description="Run VHS scenarios in an owned workspace-contained ComfyUI process.",
    )
    parser.add_argument("--comfyui-root", required=True, type=Path)
    parser.add_argument("--comfyui-python", required=True, type=Path)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--all", action="store_true", dest="run_all")
    selection.add_argument("--scenario", action="append", default=[])
    parser.add_argument("--workspace", type=Path, default=repo_root)
    parser.add_argument(
        "--matrix",
        type=Path,
        default=repo_root / "tests" / "runtime_validation_matrix.json",
    )
    parser.add_argument(
        "--result-file",
        type=Path,
        default=Path(".tmp/runtime_results/runtime_validation_results.json"),
    )
    parser.add_argument("--sandbox", type=Path)
    parser.add_argument("--keep-sandbox", action="store_true")
    parser.add_argument(
        "--ui-restore-evidence",
        type=Path,
        help="Content-free Playwright evidence for metadata workflow restoration.",
    )
    parser.add_argument("--startup-timeout", type=float, default=180.0)
    parser.add_argument("--scenario-timeout", type=float, default=180.0)
    return parser


def _contained(root: Path, candidate: Path) -> bool:
    root = root.resolve(strict=False)
    candidate = candidate.resolve(strict=False)
    try:
        common = os.path.commonpath((str(root), str(candidate)))
    except ValueError:
        return False
    return os.path.normcase(common) == os.path.normcase(str(root))


def load_runtime_scenarios(repo_root: Path, matrix_path: Path) -> list[dict[str, Any]]:
    repo_root = repo_root.resolve(strict=True)
    matrix_path = matrix_path.resolve(strict=True)
    if not _contained(repo_root, matrix_path):
        raise ResultSafetyError("runtime matrix must be inside the repository")
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    scenarios = matrix.get("scenarios")
    if not isinstance(scenarios, list) or len(scenarios) != 8:
        raise ResultSafetyError("runtime matrix must contain exactly eight scenarios")

    loaded = []
    seen_ids = set()
    for matrix_row in scenarios:
        scenario_id = matrix_row.get("id")
        fixture_relative = matrix_row.get("api_fixture_path")
        if not isinstance(scenario_id, str) or not scenario_id or scenario_id in seen_ids:
            raise ResultSafetyError("runtime matrix scenario IDs must be unique non-empty strings")
        if not isinstance(fixture_relative, str):
            raise ResultSafetyError(f"scenario {scenario_id} is missing an API fixture")
        fixture_path = (repo_root / fixture_relative).resolve(strict=True)
        fixture_root = (repo_root / "tests" / "runtime_prompts").resolve(strict=True)
        if not _contained(fixture_root, fixture_path):
            raise ResultSafetyError("API fixtures must stay inside tests/runtime_prompts")
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        if fixture.get("scenario_id") != scenario_id:
            raise ResultSafetyError("runtime fixture scenario ID does not match the matrix")
        workflow_relative = matrix_row.get("workflow_fixture_path")
        if workflow_relative is not None:
            if not isinstance(workflow_relative, str):
                raise ResultSafetyError("workflow fixture path must be a string")
            workflow_path = (repo_root / workflow_relative).resolve(strict=True)
            workflow_root = (repo_root / "tests" / "runtime_workflows").resolve(strict=True)
            if not _contained(workflow_root, workflow_path):
                raise ResultSafetyError("frontend workflow fixtures must stay inside tests/runtime_workflows")
            if "workflow" in fixture:
                raise ResultSafetyError("API prompt fixtures must not embed frontend workflows")
            fixture["workflow"] = json.loads(workflow_path.read_text(encoding="utf-8"))
        loaded.append({**deepcopy(matrix_row), "fixture": fixture})
        seen_ids.add(scenario_id)
    return loaded


def select_runtime_scenarios(
    scenarios: Sequence[Mapping[str, Any]],
    *,
    run_all: bool,
    requested_ids: Sequence[str],
) -> list[Mapping[str, Any]]:
    available = {scenario["id"] for scenario in scenarios}
    if run_all and requested_ids:
        raise ResultSafetyError("use either --all or explicit --scenario values")
    if run_all:
        return list(scenarios)
    if not requested_ids:
        raise ResultSafetyError("select --all or at least one --scenario")
    requested = set(requested_ids)
    unknown = requested - available
    if unknown:
        raise ResultSafetyError("unknown runtime scenario requested")
    return [scenario for scenario in scenarios if scenario["id"] in requested]


def resolve_result_file(workspace: Path, result_file: Path) -> Path:
    workspace = workspace.resolve(strict=True)
    resolved = result_file.expanduser().resolve(strict=False)
    if not _contained(workspace, resolved):
        raise ResultSafetyError("result file must stay inside the workspace")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    if not _contained(workspace, resolved.parent):
        raise ResultSafetyError("resolved result directory escapes the workspace")
    return resolved


_UI_EVIDENCE_KEYS = {
    "schema_version",
    "scenario_id",
    "observed",
    "host_commit",
    "plugin_commit",
    "plugin_runtime_sha256",
    "workflow_sha256",
    "screenshot",
    "browser_driver",
    "observed_at",
    "node_count",
    "node_types",
    "link_count",
    "widget_checks",
}
_UI_WIDGET_CHECKS = {
    "empty_image_dimensions",
    "empty_image_batch_color",
    "combine_base",
    "combine_format",
    "combine_metadata_controls",
}


def load_ui_restore_evidence(
    workspace: Path,
    evidence_path: Path,
    workflow_path: Path,
    *,
    host_commit: str,
    plugin_commit: str,
    plugin_runtime_sha256: str,
) -> set[str]:
    """Validate content-free, source-bound evidence from a production UI restore."""
    workspace = workspace.resolve(strict=True)
    evidence_root = (workspace / ".tmp" / "runtime_results").resolve(strict=True)
    evidence_path = evidence_path.resolve(strict=True)
    if not _contained(evidence_root, evidence_path):
        raise ResultSafetyError("UI restore evidence must stay inside .tmp/runtime_results")

    workflow_root = (workspace / "tests" / "runtime_workflows").resolve(strict=True)
    workflow_path = workflow_path.resolve(strict=True)
    if not _contained(workflow_root, workflow_path):
        raise ResultSafetyError("UI restore workflow must stay inside tests/runtime_workflows")

    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    if not isinstance(evidence, dict) or set(evidence) != _UI_EVIDENCE_KEYS:
        raise ResultSafetyError("UI restore evidence has an unexpected schema")
    if evidence["schema_version"] != 2:
        raise ResultSafetyError("UI restore evidence schema version is unsupported")
    if evidence["scenario_id"] != "metadata_enabled_roundtrip" or evidence["observed"] is not True:
        raise ResultSafetyError("UI restore evidence does not prove the metadata scenario")
    _validate_commit(evidence["host_commit"], "UI evidence host_commit")
    _validate_commit(evidence["plugin_commit"], "UI evidence plugin_commit")
    if evidence["host_commit"] != host_commit or evidence["plugin_commit"] != plugin_commit:
        raise ResultSafetyError("UI restore evidence does not match the selected sources")
    _validate_sha256(evidence["plugin_runtime_sha256"], "UI evidence plugin_runtime_sha256")
    if evidence["plugin_runtime_sha256"] != plugin_runtime_sha256:
        raise ResultSafetyError("UI restore evidence runtime source digest is stale")

    workflow_digest = hashlib.sha256(workflow_path.read_bytes()).hexdigest()
    if evidence["workflow_sha256"] != workflow_digest:
        raise ResultSafetyError("UI restore evidence workflow digest is stale")
    if evidence["browser_driver"] != "@playwright/cli" or not isinstance(
        evidence["observed_at"], str
    ) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z",
        evidence["observed_at"],
    ):
        raise ResultSafetyError("UI restore evidence has invalid observation metadata")
    if (
        evidence["node_count"] != 2
        or evidence["node_types"] != ["EmptyImage", "VHS_VideoCombine"]
        or evidence["link_count"] != 1
    ):
        raise ResultSafetyError("UI restore evidence has an unexpected graph shape")
    widget_checks = evidence["widget_checks"]
    if (
        not isinstance(widget_checks, dict)
        or set(widget_checks) != _UI_WIDGET_CHECKS
        or not all(value is True for value in widget_checks.values())
    ):
        raise ResultSafetyError("UI restore evidence has incomplete widget checks")

    _validate_artifact(evidence["screenshot"])
    screenshot_root = (workspace / "output" / "playwright").resolve(strict=True)
    screenshot = (workspace / evidence["screenshot"]).resolve(strict=True)
    if not _contained(screenshot_root, screenshot) or not screenshot.is_file():
        raise ResultSafetyError("UI restore screenshot is missing or outside output/playwright")
    with screenshot.open("rb") as screenshot_file:
        screenshot_signature = screenshot_file.read(8)
    if screenshot_signature != b"\x89PNG\r\n\x1a\n":
        raise ResultSafetyError("UI restore screenshot is not a PNG file")
    return {"metadata_enabled_roundtrip"}


_VIDEO_SUFFIXES = {".mp4", ".webm", ".mkv", ".mov", ".avi", ".gif"}


def _new_artifacts(output_root: Path, files_before: set[str]) -> tuple[list[str], list[Path]]:
    output_root = output_root.resolve(strict=True)
    relative_paths = []
    absolute_paths = []
    for path in output_root.rglob("*"):
        if not path.is_file():
            continue
        resolved = path.resolve(strict=True)
        if not _contained(output_root, resolved):
            raise ResultSafetyError("runtime artifact resolves outside the output root")
        relative = resolved.relative_to(output_root).as_posix()
        if relative not in files_before:
            _validate_artifact(relative)
            relative_paths.append(relative)
            absolute_paths.append(resolved)
    ordered = sorted(zip(relative_paths, absolute_paths), key=lambda pair: pair[0])
    return [pair[0] for pair in ordered], [pair[1] for pair in ordered]


def _all_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, child in value.items():
            yield from _all_strings(key)
            yield from _all_strings(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _all_strings(child)


def _history_previews(history: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    previews = []
    outputs = history.get("outputs", {})
    if not isinstance(outputs, Mapping):
        return previews
    for output in outputs.values():
        if not isinstance(output, Mapping):
            continue
        gifs = output.get("gifs", [])
        if isinstance(gifs, list):
            previews.extend(item for item in gifs if isinstance(item, Mapping))
    return previews


def _preview_targets_artifact(
    output_root: Path,
    previews: Sequence[Mapping[str, Any]],
    artifact_relatives: set[str],
) -> bool:
    for preview in previews:
        filename = preview.get("filename")
        subfolder = preview.get("subfolder", "")
        if not isinstance(filename, str) or not isinstance(subfolder, str):
            continue
        candidate = (output_root / subfolder / filename).resolve(strict=False)
        if not _contained(output_root, candidate):
            continue
        relative = candidate.relative_to(output_root.resolve()).as_posix()
        if relative in artifact_relatives and candidate.is_file():
            return True
    return False


def _format_tags(probe: Mapping[str, Any]) -> Mapping[str, Any]:
    format_info = probe.get("format", {})
    if not isinstance(format_info, Mapping):
        return {}
    tags = format_info.get("tags", {})
    return tags if isinstance(tags, Mapping) else {}


def _tags_have_workflow_metadata(tags: Mapping[str, Any]) -> bool:
    return any(
        marker in str(key).lower() or marker in str(value).lower()
        for key, value in tags.items()
        for marker in ("workflow", "prompt")
    )


def png_contains_workflow_metadata(path: Path) -> bool:
    """Inspect PNG text keywords without loading image pixels or external libraries."""
    with path.open("rb") as handle:
        if handle.read(8) != b"\x89PNG\r\n\x1a\n":
            raise ResultSafetyError("utility PNG has an invalid signature")
        while True:
            header = handle.read(8)
            if not header:
                return False
            if len(header) != 8:
                raise ResultSafetyError("utility PNG has a truncated chunk header")
            length, chunk_type = struct.unpack(">I4s", header)
            if length > 64 * 1024 * 1024:
                raise ResultSafetyError("utility PNG chunk exceeds the inspection limit")
            if chunk_type in {b"tEXt", b"zTXt", b"iTXt"}:
                data = handle.read(length)
                if len(data) != length:
                    raise ResultSafetyError("utility PNG has a truncated text chunk")
                keyword = data.split(b"\0", 1)[0].decode("latin-1", errors="ignore").lower()
                if keyword in {"workflow", "prompt"}:
                    return True
            else:
                handle.seek(length, os.SEEK_CUR)
            if len(handle.read(4)) != 4:
                raise ResultSafetyError("utility PNG has a truncated chunk checksum")
            if chunk_type == b"IEND":
                return False


def evaluate_scenario(
    scenario: Mapping[str, Any],
    history: Mapping[str, Any],
    output_root: Path,
    *,
    files_before: set[str],
    media_probe,
    observed_ui_restore: bool = False,
) -> dict[str, Any]:
    scenario_id = scenario["id"]
    fixture = scenario["fixture"]
    expected_outcome = fixture["expected_outcome"]
    relative_artifacts, artifact_paths = _new_artifacts(output_root, files_before)
    video_paths = [path for path in artifact_paths if path.suffix.lower() in _VIDEO_SUFFIXES]
    probes = {path: media_probe(path) for path in video_paths}
    status_info = history.get("status", {})
    reported_completed = isinstance(status_info, Mapping) and status_info.get("completed") is True
    status_str = status_info.get("status_str") if isinstance(status_info, Mapping) else None
    terminal = reported_completed or status_str == "error"
    succeeded = reported_completed and status_str == "success"
    checks: dict[str, bool] = {
        "completed": terminal,
        "expected_outcome": succeeded if expected_outcome == "success" else terminal and not succeeded,
    }
    for assertion in fixture["assertions"]:
        checks[assertion] = False

    previews = _history_previews(history)
    artifact_set = set(relative_artifacts)
    stream_types = {
        path: {
            stream.get("codec_type")
            for stream in probe.get("streams", [])
            if isinstance(stream, Mapping)
        }
        for path, probe in probes.items()
        if isinstance(probe, Mapping)
    }

    if scenario_id == "no_audio_video_output":
        checks["final_video"] = len(video_paths) == 1
        checks["preview_payload"] = _preview_targets_artifact(output_root, previews, artifact_set)
        checks["video_only_stream"] = bool(video_paths) and all(types == {"video"} for types in stream_types.values())
        checks["filename_dimensions"] = any("64x48" in path.name for path in video_paths)
    elif scenario_id == "audio_connected_output":
        checks["muxed_final"] = len(video_paths) == 1 and "-audio" in video_paths[0].stem
        checks["audio_stream"] = len(video_paths) == 1 and stream_types.get(video_paths[0]) == {"video", "audio"}
        checks["silent_intermediate_hidden"] = len(video_paths) == 1 and "-audio" in video_paths[0].stem
        checks["metadata_preserved"] = any(
            _tags_have_workflow_metadata(_format_tags(probe))
            for probe in probes.values()
        )
    elif scenario_id == "unsupported_audio_format_failure":
        error_text = " ".join(_all_strings(status_info)).lower()
        checks["unsupported_audio_error"] = "audio" in error_text and (
            "unsupported" in error_text or "not support" in error_text
        )
        checks["zero_durable_artifacts"] = not relative_artifacts
    elif scenario_id == "metadata_enabled_roundtrip":
        checks["video_workflow_metadata"] = any(
            _tags_have_workflow_metadata(_format_tags(probe))
            for probe in probes.values()
        )
        checks["production_ui_workflow_restore"] = observed_ui_restore
    elif scenario_id == "metadata_disabled_utility_png":
        png_paths = [path for path in artifact_paths if path.suffix.lower() == ".png"]
        checks["utility_png_has_no_workflow_text"] = bool(png_paths) and all(
            not png_contains_workflow_metadata(path)
            for path in png_paths
        )
        checks["video_has_no_workflow_metadata"] = bool(probes) and all(
            not _tags_have_workflow_metadata(_format_tags(probe))
            for probe in probes.values()
        )
    elif scenario_id == "image_sequence_output_and_prune":
        png_paths = [path for path in artifact_paths if path.suffix.lower() == ".png"]
        checks["concrete_frame_paths"] = len(png_paths) >= 2
        checks["frame_001_preview"] = any(path.name.endswith("001.png") for path in png_paths) and _preview_targets_artifact(output_root, previews, artifact_set)
        checks["prune_all_removes_frames"] = False
        checks["prune_intermediate_keeps_frames"] = False
    elif scenario_id == "filename_template_path_ux":
        checks["filename_contains_64x48"] = any("64x48" in path.name for path in artifact_paths)
        checks["outside_prefix_rejected"] = False
        checks["cross_drive_prefix_rejected"] = False
    elif scenario_id == "partial_artifact_cleanup_and_prune_safety":
        checks["partial_artifacts_removed"] = not relative_artifacts
        checks["outside_prune_rejected_before_delete"] = False

    return {
        "scenario_id": scenario_id,
        "status": "passed" if checks and all(checks.values()) else "failed",
        "checks": checks,
        "artifacts": relative_artifacts,
    }


def _current_relative_files(output_root: Path) -> set[str]:
    output_root = output_root.resolve(strict=True)
    files = set()
    for path in output_root.rglob("*"):
        if not path.is_file():
            continue
        resolved = path.resolve(strict=True)
        if not _contained(output_root, resolved):
            raise ResultSafetyError("pre-existing artifact resolves outside the output root")
        files.add(resolved.relative_to(output_root).as_posix())
    return files


def build_image_sequence_variants(fixture: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    variants = {}
    for name in ("concrete", "preview", "keep", "remove"):
        variant = deepcopy(fixture)
        prompt = variant.get("prompt", {})
        if "1" not in prompt or "2" not in prompt:
            raise ResultSafetyError("image-sequence fixture must use canonical nodes 1 and 2")
        prompt["2"]["inputs"]["filename_prefix"] = (
            f"runtime/image_sequence_output_and_prune/{name}"
        )
        if name == "preview":
            prompt["1"]["inputs"]["batch_size"] = 1
        if name in {"keep", "remove"}:
            prompt["3"] = {
                "class_type": "VHS_PruneOutputs",
                "inputs": {
                    "filenames": ["2", 0],
                    "options": "Intermediate and Utility" if name == "keep" else "All",
                },
            }
        variants[name] = variant
    return variants


def run_image_sequence_scenario(
    client,
    scenario: Mapping[str, Any],
    output_root: Path,
    *,
    media_probe,
    scenario_timeout: float,
) -> dict[str, Any]:
    files_before = _current_relative_files(output_root)
    variants = build_image_sequence_variants(scenario["fixture"])
    histories = {}
    all_subruns_succeeded = True
    for name, fixture in variants.items():
        prompt_id = client.submit_prompt(fixture)
        history = client.wait_for_history(
            prompt_id,
            timeout=scenario_timeout,
            poll_interval=0.25,
        )
        histories[name] = history
        status = history.get("status", {})
        all_subruns_succeeded = all_subruns_succeeded and (
            isinstance(status, Mapping)
            and status.get("completed") is True
            and status.get("status_str") == "success"
        )

    row = evaluate_scenario(
        scenario,
        histories["preview"],
        output_root,
        files_before=files_before,
        media_probe=media_probe,
    )
    artifacts = row["artifacts"]
    concrete_frames = [
        path for path in artifacts
        if "/concrete_" in f"/{path}" and re.search(r"\.\d{3}\.png$", path)
    ]
    keep_frames = [
        path for path in artifacts
        if "/keep_" in f"/{path}" and re.search(r"\.\d{3}\.png$", path)
    ]
    remove_artifacts = [path for path in artifacts if "/remove_" in f"/{path}"]
    row["checks"]["expected_outcome"] = all_subruns_succeeded
    row["checks"]["concrete_frame_paths"] = len(concrete_frames) >= 2
    row["checks"]["prune_intermediate_keeps_frames"] = len(keep_frames) >= 2
    row["checks"]["prune_all_removes_frames"] = not remove_artifacts
    row["status"] = "passed" if all(row["checks"].values()) else "failed"
    return row


def build_filename_negative_variants(fixture: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    variants = {}
    prefixes = {
        "outside": "../vhs-runtime-outside/clip",
        "cross_drive": "Z:\\vhs-runtime-cross-drive\\clip",
    }
    for name, prefix in prefixes.items():
        variant = deepcopy(fixture)
        prompt = variant.get("prompt", {})
        if "2" not in prompt:
            raise ResultSafetyError("filename fixture must use canonical combine node 2")
        prompt["2"]["inputs"]["filename_prefix"] = prefix
        variants[name] = variant
    return variants


def run_filename_scenario(
    client,
    scenario: Mapping[str, Any],
    output_root: Path,
    *,
    containment_root: Path,
    media_probe,
    scenario_timeout: float,
) -> dict[str, Any]:
    files_before = _current_relative_files(output_root)
    prompt_id = client.submit_prompt(scenario["fixture"])
    history = client.wait_for_history(
        prompt_id,
        timeout=scenario_timeout,
        poll_interval=0.25,
    )
    row = evaluate_scenario(
        scenario,
        history,
        output_root,
        files_before=files_before,
        media_probe=media_probe,
    )

    results = {}
    for name, fixture in build_filename_negative_variants(scenario["fixture"]).items():
        containment_before = _current_relative_files(containment_root)
        rejected = False
        try:
            negative_id = client.submit_prompt(fixture)
            negative_history = client.wait_for_history(
                negative_id,
                timeout=scenario_timeout,
                poll_interval=0.25,
            )
            status = negative_history.get("status", {})
            rejected = isinstance(status, Mapping) and status.get("status_str") == "error"
        except ComfyApiError:
            rejected = True
        containment_after = _current_relative_files(containment_root)
        results[name] = rejected and containment_after == containment_before

    row["checks"]["outside_prefix_rejected"] = results["outside"]
    row["checks"]["cross_drive_prefix_rejected"] = results["cross_drive"]
    row["status"] = "passed" if all(row["checks"].values()) else "failed"
    return row


def write_failing_ffmpeg_shim(layout: RuntimeLayout) -> Path:
    """Create a disposable encoder that writes the requested output and exits 23."""
    if os.name == "nt":
        shim = layout.temp / "vhs-failing-ffmpeg.cmd"
        content = """@echo off
setlocal
set "vhs_last="
:vhs_args
if "%~1"=="" goto vhs_run
set "vhs_last=%~1"
shift
goto vhs_args
:vhs_run
if "%vhs_last%"=="" exit /b 24
> "%vhs_last%" <nul set /p "=partial"
if not "%VHS_RUNTIME_SHIM_MARKER%"=="" > "%VHS_RUNTIME_SHIM_MARKER%" echo partial-created
more >nul
exit /b 23
"""
        shim.write_text(content, encoding="utf-8", newline="\r\n")
    else:
        shim = layout.temp / "vhs-failing-ffmpeg.sh"
        content = """#!/bin/sh
vhs_last=""
for vhs_arg in "$@"; do vhs_last="$vhs_arg"; done
[ -n "$vhs_last" ] || exit 24
printf partial > "$vhs_last"
if [ -n "$VHS_RUNTIME_SHIM_MARKER" ]; then printf partial-created > "$VHS_RUNTIME_SHIM_MARKER"; fi
cat >/dev/null
exit 23
"""
        shim.write_text(content, encoding="utf-8", newline="\n")
        shim.chmod(0o700)
    return shim


def write_runtime_support_node(layout: RuntimeLayout) -> Path:
    """Create a sandbox-only typed source node used by the prune safety scenario."""
    node_root = layout.base / "custom_nodes" / "VHS-RuntimeHarness"
    if node_root.exists():
        raise ResultSafetyError("runtime support node target already exists")
    node_root.mkdir(parents=True)
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
NODE_DISPLAY_NAME_MAPPINGS = {"VHS_RuntimeFilenames": "VHS Runtime Filenames"}
'''
    (node_root / "__init__.py").write_text(source, encoding="utf-8", newline="\n")
    return node_root


def build_prune_safety_prompt(inside_path: Path, outside_path: Path) -> dict[str, Any]:
    return {
        "prompt": {
            "1": {
                "class_type": "VHS_RuntimeFilenames",
                "inputs": {
                    "inside_path": str(inside_path),
                    "outside_path": str(outside_path),
                },
            },
            "2": {
                "class_type": "VHS_PruneOutputs",
                "inputs": {
                    "filenames": ["1", 0],
                    "options": "All",
                },
            }
        }
    }


def run_partial_cleanup_scenario(
    client,
    scenario: Mapping[str, Any],
    output_root: Path,
    *,
    temp_root: Path,
    user_root: Path,
    fault_marker: Path,
    media_probe,
    scenario_timeout: float,
) -> dict[str, Any]:
    files_before = _current_relative_files(output_root)
    prompt_id = client.submit_prompt(scenario["fixture"])
    history = client.wait_for_history(
        prompt_id,
        timeout=scenario_timeout,
        poll_interval=0.25,
    )
    fault_executed = fault_marker.is_file()
    if fault_marker.exists():
        fault_marker.unlink()
    row = evaluate_scenario(
        scenario,
        history,
        output_root,
        files_before=files_before,
        media_probe=media_probe,
    )
    row["checks"]["fault_injection_executed"] = fault_executed

    inside = output_root / "runtime" / scenario["id"] / "prune-inside.tmp"
    outside = user_root / "prune-outside.tmp"
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_text("inside", encoding="utf-8")
    outside.write_text("outside", encoding="utf-8")
    try:
        prune_id = client.submit_prompt(build_prune_safety_prompt(inside, outside))
        prune_history = client.wait_for_history(
            prune_id,
            timeout=scenario_timeout,
            poll_interval=0.25,
        )
        prune_status = prune_history.get("status", {})
        prune_text = " ".join(_all_strings(prune_status)).lower()
        rejected = (
            isinstance(prune_status, Mapping)
            and prune_status.get("status_str") == "error"
            and ("invalid directory" in prune_text or "prune" in prune_text)
        )
        row["checks"]["outside_prune_rejected_before_delete"] = (
            rejected and inside.is_file() and outside.is_file()
        )
    finally:
        for sentinel in (inside, outside):
            if sentinel.exists():
                sentinel.unlink()
    row["status"] = "passed" if all(row["checks"].values()) else "failed"
    return row


def partition_runtime_scenarios(
    scenarios: Sequence[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    fault_id = "partial_artifact_cleanup_and_prune_safety"
    normal = [scenario for scenario in scenarios if scenario["id"] != fault_id]
    fault = [scenario for scenario in scenarios if scenario["id"] == fault_id]
    return normal, fault


def build_fault_environment(
    base_environment: Mapping[str, str],
    shim: Path,
    marker: Path,
) -> dict[str, str]:
    environment = dict(base_environment)
    environment["VHS_FORCE_FFMPEG_PATH"] = str(shim)
    environment["VHS_RUNTIME_SHIM_MARKER"] = str(marker)
    return environment


def run_scenarios(
    client,
    scenarios: Sequence[Mapping[str, Any]],
    output_root: Path,
    *,
    media_probe,
    scenario_timeout: float,
    observed_ui_restore_ids: set[str] | None = None,
    containment_root: Path | None = None,
) -> list[dict[str, Any]]:
    observed_ui_restore_ids = observed_ui_restore_ids or set()
    containment_root = containment_root or output_root.parent
    rows = []
    for scenario in scenarios:
        if scenario["id"] == "partial_artifact_cleanup_and_prune_safety":
            raise ResultSafetyError("partial cleanup scenario requires the isolated fault host")
        if scenario["id"] == "image_sequence_output_and_prune":
            rows.append(
                run_image_sequence_scenario(
                    client,
                    scenario,
                    output_root,
                    media_probe=media_probe,
                    scenario_timeout=scenario_timeout,
                )
            )
            continue
        if scenario["id"] == "filename_template_path_ux":
            rows.append(
                run_filename_scenario(
                    client,
                    scenario,
                    output_root,
                    containment_root=containment_root,
                    media_probe=media_probe,
                    scenario_timeout=scenario_timeout,
                )
            )
            continue
        files_before = _current_relative_files(output_root)
        prompt_id = client.submit_prompt(scenario["fixture"])
        history = client.wait_for_history(
            prompt_id,
            timeout=scenario_timeout,
            poll_interval=0.25,
        )
        rows.append(
            evaluate_scenario(
                scenario,
                history,
                output_root,
                files_before=files_before,
                media_probe=media_probe,
                observed_ui_restore=scenario["id"] in observed_ui_restore_ids,
            )
        )
    return rows


def _validate_commit(value: str, label: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ResultSafetyError(f"{label} must be a full lowercase Git commit")


def _validate_sha256(value: str, label: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ResultSafetyError(f"{label} must be a lowercase SHA-256 digest")


def _validate_artifact(value: Any) -> None:
    if not isinstance(value, str) or not value:
        raise ResultSafetyError("artifact evidence must be a non-empty relative path")
    posix = PurePosixPath(value.replace("\\", "/"))
    windows = PureWindowsPath(value)
    if posix.is_absolute() or windows.is_absolute() or windows.drive or ".." in posix.parts or "://" in value:
        raise ResultSafetyError("artifact evidence must not contain an absolute or escaping path")


def build_result_document(
    expected_scenario_ids: Sequence[str],
    rows: Iterable[Mapping[str, Any]],
    *,
    host_commit: str,
    plugin_commit: str,
    plugin_runtime_sha256: str,
) -> dict[str, Any]:
    _validate_commit(host_commit, "host_commit")
    _validate_commit(plugin_commit, "plugin_commit")
    _validate_sha256(plugin_runtime_sha256, "plugin_runtime_sha256")
    expected = list(expected_scenario_ids)
    if len(expected) != len(set(expected)):
        raise ResultSafetyError("expected scenario IDs must be unique")

    by_id: dict[str, dict[str, Any]] = {}
    for raw_row in rows:
        scenario_id = raw_row.get("scenario_id")
        if scenario_id in by_id:
            raise ResultSafetyError("result rows contain a duplicate scenario ID")
        status = raw_row.get("status")
        checks = raw_row.get("checks")
        artifacts = raw_row.get("artifacts")
        if scenario_id not in expected or status not in {"passed", "failed"}:
            raise ResultSafetyError("result row has an unknown ID or status")
        if not isinstance(checks, dict) or not checks or not all(
            isinstance(key, str) and isinstance(value, bool)
            for key, value in checks.items()
        ):
            raise ResultSafetyError("result checks must be named booleans")
        expected_status = "passed" if checks and all(checks.values()) else "failed"
        if status != expected_status:
            raise ResultSafetyError("result status does not match its boolean checks")
        if not isinstance(artifacts, list):
            raise ResultSafetyError("result artifacts must be a list")
        for artifact in artifacts:
            _validate_artifact(artifact)
        by_id[scenario_id] = {
            "scenario_id": scenario_id,
            "status": status,
            "checks": dict(checks),
            "artifacts": list(artifacts),
        }
    if set(by_id) != set(expected):
        raise ResultSafetyError("result rows must cover every selected scenario exactly once")

    ordered = [by_id[scenario_id] for scenario_id in expected]
    passed = sum(row["status"] == "passed" for row in ordered)
    return {
        "schema_version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "deployment_profile": "trusted_local",
        "host_commit": host_commit,
        "plugin_commit": plugin_commit,
        "plugin_runtime_sha256": plugin_runtime_sha256,
        "summary": {"passed": passed, "failed": len(ordered) - passed},
        "scenarios": ordered,
    }


def _reserve_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _git_commit(root: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    commit = completed.stdout.strip().lower()
    if completed.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ResultSafetyError("unable to resolve a full Git commit for runtime evidence")
    return commit


def _probe_media_file(path: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise ResultSafetyError("ffprobe failed for a contained runtime artifact")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ResultSafetyError("ffprobe returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ResultSafetyError("ffprobe result must be an object")
    return payload


def _remove_sandbox(workspace: Path, sandbox: Path) -> None:
    if not _contained(workspace, sandbox):
        raise ResultSafetyError("refusing to remove a sandbox outside the workspace")
    if sandbox == workspace.resolve():
        raise ResultSafetyError("refusing to remove the workspace as a sandbox")
    if sandbox.exists():
        shutil.rmtree(sandbox)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    repo_root = Path(__file__).resolve().parents[2]
    workspace = args.workspace.resolve(strict=True)
    if workspace != repo_root:
        parser.error("--workspace must resolve to this repository root")

    sandbox = args.sandbox
    if sandbox is None:
        sandbox = workspace / ".tmp" / "runtime" / f"run-{uuid.uuid4().hex}"
    elif not sandbox.is_absolute():
        sandbox = workspace / sandbox

    result_candidate = args.result_file
    if not result_candidate.is_absolute():
        result_candidate = workspace / result_candidate

    layout = None
    try:
        scenarios = load_runtime_scenarios(repo_root, args.matrix)
        selected = select_runtime_scenarios(
            scenarios,
            run_all=args.run_all,
            requested_ids=args.scenario,
        )
        result_file = resolve_result_file(workspace, result_candidate)
        layout = RuntimeLayout.create(workspace, sandbox)
        if _contained(layout.sandbox, result_file):
            raise ResultSafetyError("result file must be outside the disposable runtime sandbox")

        trusted_host = validate_trusted_host(
            workspace,
            args.comfyui_root,
            args.comfyui_python,
        )
        host_commit = _git_commit(trusted_host.root)
        plugin_commit = _git_commit(repo_root)
        plugin_runtime_digest = runtime_plugin_sha256(repo_root)
        observed_ui_restore_ids: set[str] = set()
        if args.ui_restore_evidence is not None:
            selected_ids = {scenario["id"] for scenario in selected}
            if "metadata_enabled_roundtrip" not in selected_ids:
                raise ResultSafetyError(
                    "UI restore evidence requires the metadata-enabled scenario selection"
                )
            evidence_candidate = args.ui_restore_evidence
            if not evidence_candidate.is_absolute():
                evidence_candidate = workspace / evidence_candidate
            observed_ui_restore_ids = load_ui_restore_evidence(
                workspace,
                evidence_candidate,
                repo_root / "tests" / "runtime_workflows" / "metadata_enabled_roundtrip.json",
                host_commit=host_commit,
                plugin_commit=plugin_commit,
                plugin_runtime_sha256=plugin_runtime_digest,
            )
        copy_runtime_plugin(repo_root, layout)
        normal_scenarios, fault_scenarios = partition_runtime_scenarios(selected)
        rows = []
        if normal_scenarios:
            port = _reserve_loopback_port()
            command = build_comfyui_command(trusted_host, layout, port)
            owned = OwnedComfyUIProcess(command, trusted_host.root)
            client = LoopbackComfyApiClient(f"http://127.0.0.1:{port}")
            with owned:
                client.wait_until_ready(
                    owned,
                    timeout=args.startup_timeout,
                    poll_interval=0.25,
                )
                rows.extend(
                    run_scenarios(
                        client,
                        normal_scenarios,
                        layout.output,
                        media_probe=_probe_media_file,
                        scenario_timeout=args.scenario_timeout,
                        observed_ui_restore_ids=observed_ui_restore_ids,
                        containment_root=layout.sandbox,
                    )
                )

        if fault_scenarios:
            shim = write_failing_ffmpeg_shim(layout)
            write_runtime_support_node(layout)
            marker = layout.temp / "vhs-runtime-fault-marker.txt"
            fault_environment = build_fault_environment(os.environ, shim, marker)
            fault_port = _reserve_loopback_port()
            fault_command = build_comfyui_command(
                trusted_host,
                layout,
                fault_port,
                additional_whitelist=("VHS-RuntimeHarness",),
            )
            fault_owned = OwnedComfyUIProcess(
                fault_command,
                trusted_host.root,
                env=fault_environment,
            )
            fault_client = LoopbackComfyApiClient(f"http://127.0.0.1:{fault_port}")
            with fault_owned:
                fault_client.wait_until_ready(
                    fault_owned,
                    timeout=args.startup_timeout,
                    poll_interval=0.25,
                )
                for fault_scenario in fault_scenarios:
                    rows.append(
                        run_partial_cleanup_scenario(
                            fault_client,
                            fault_scenario,
                            layout.output,
                            temp_root=layout.temp,
                            user_root=layout.user,
                            fault_marker=marker,
                            media_probe=_probe_media_file,
                            scenario_timeout=args.scenario_timeout,
                        )
                    )

        document = build_result_document(
            [scenario["id"] for scenario in selected],
            rows,
            host_commit=host_commit,
            plugin_commit=plugin_commit,
            plugin_runtime_sha256=plugin_runtime_digest,
        )
        result_file.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        safe_result = result_file.relative_to(workspace).as_posix()
        print(
            f"runtime matrix: passed={document['summary']['passed']} "
            f"failed={document['summary']['failed']} result={safe_result}"
        )
        return 0 if document["summary"]["failed"] == 0 else 1
    except (ComfyApiError, ResultSafetyError, RuntimeHarnessError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"runtime harness failed safely: {type(exc).__name__}", file=sys.stderr)
        return 2
    finally:
        if layout is not None and not args.keep_sandbox:
            try:
                _remove_sandbox(workspace, layout.sandbox)
            except (OSError, ResultSafetyError) as exc:
                print(f"runtime sandbox cleanup failed safely: {type(exc).__name__}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())

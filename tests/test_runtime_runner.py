import hashlib
import json
import os
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from scripts.runtime.run_runtime_matrix import (
    ResultSafetyError,
    build_argument_parser,
    build_result_document,
    evaluate_scenario,
    load_runtime_scenarios,
    load_ui_restore_evidence,
    resolve_result_file,
    run_scenarios,
    png_contains_workflow_metadata,
    build_image_sequence_variants,
    run_image_sequence_scenario,
    build_filename_negative_variants,
    run_filename_scenario,
    build_prune_safety_prompt,
    run_partial_cleanup_scenario,
    write_failing_ffmpeg_shim,
    build_fault_environment,
    partition_runtime_scenarios,
    write_runtime_support_node,
    select_runtime_scenarios,
)
from scripts.runtime.api import ComfyApiError


REPO_ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = REPO_ROOT / "tests" / "runtime_validation_matrix.json"


class RuntimeRunnerContractTests(unittest.TestCase):
    def test_partial_scenario_is_partitioned_into_an_isolated_fault_host(self):
        scenarios = load_runtime_scenarios(REPO_ROOT, MATRIX_PATH)
        normal, fault = partition_runtime_scenarios(scenarios)
        self.assertEqual(len(normal), 7)
        self.assertEqual([row["id"] for row in fault], ["partial_artifact_cleanup_and_prune_safety"])
        self.assertNotIn("partial_artifact_cleanup_and_prune_safety", {row["id"] for row in normal})

        environment = build_fault_environment(
            {"SAFE_BASE": "retained"},
            Path("shim"),
            Path("marker"),
        )
        self.assertEqual(environment["SAFE_BASE"], "retained")
        self.assertEqual(environment["VHS_FORCE_FFMPEG_PATH"], str(Path("shim")))
        self.assertEqual(environment["VHS_RUNTIME_SHIM_MARKER"], str(Path("marker")))

    def test_argument_parser_requires_explicit_host_and_scenario_selection(self):
        parser = build_argument_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args([])

        args = parser.parse_args([
            "--comfyui-root", "trusted-host",
            "--comfyui-python", "trusted-python",
            "--all",
        ])
        self.assertTrue(args.run_all)
        self.assertEqual(args.scenario, [])
        self.assertFalse(args.keep_sandbox)
        self.assertIsNone(args.ui_restore_evidence)
        self.assertEqual(
            args.result_file.as_posix(),
            ".tmp/runtime_results/runtime_validation_results.json",
        )

    def test_ui_restore_evidence_is_digest_bound_and_content_free(self):
        with tempfile.TemporaryDirectory() as workspace_text, tempfile.TemporaryDirectory() as outside_text:
            workspace = Path(workspace_text)
            workflow = workspace / "tests" / "runtime_workflows" / "metadata_enabled_roundtrip.json"
            screenshot = workspace / "output" / "playwright" / "r32-ui" / "passed.png"
            evidence_path = workspace / ".tmp" / "runtime_results" / "ui_restore_evidence.json"
            workflow.parent.mkdir(parents=True)
            screenshot.parent.mkdir(parents=True)
            evidence_path.parent.mkdir(parents=True)
            workflow.write_text('{"version": 0.4}\n', encoding="utf-8")
            screenshot.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
            host_commit = "a" * 40
            plugin_commit = "b" * 40
            evidence = {
                "schema_version": 2,
                "scenario_id": "metadata_enabled_roundtrip",
                "observed": True,
                "host_commit": host_commit,
                "plugin_commit": plugin_commit,
                "plugin_runtime_sha256": "c" * 64,
                "workflow_sha256": hashlib.sha256(workflow.read_bytes()).hexdigest(),
                "screenshot": "output/playwright/r32-ui/passed.png",
                "browser_driver": "@playwright/cli",
                "observed_at": "2026-08-08T10:02:23Z",
                "node_count": 2,
                "node_types": ["EmptyImage", "VHS_VideoCombine"],
                "link_count": 1,
                "widget_checks": {
                    "empty_image_dimensions": True,
                    "empty_image_batch_color": True,
                    "combine_base": True,
                    "combine_format": True,
                    "combine_metadata_controls": True,
                },
            }
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

            self.assertEqual(
                load_ui_restore_evidence(
                    workspace,
                    evidence_path,
                    workflow,
                    host_commit=host_commit,
                    plugin_commit=plugin_commit,
                    plugin_runtime_sha256="c" * 64,
                ),
                {"metadata_enabled_roundtrip"},
            )

            evidence["widget_checks"]["combine_format"] = False
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
            with self.assertRaises(ResultSafetyError):
                load_ui_restore_evidence(
                    workspace,
                    evidence_path,
                    workflow,
                    host_commit=host_commit,
                    plugin_commit=plugin_commit,
                    plugin_runtime_sha256="c" * 64,
                )

            evidence["widget_checks"]["combine_format"] = True
            evidence["plugin_runtime_sha256"] = "0" * 64
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
            with self.assertRaises(ResultSafetyError):
                load_ui_restore_evidence(
                    workspace,
                    evidence_path,
                    workflow,
                    host_commit=host_commit,
                    plugin_commit=plugin_commit,
                    plugin_runtime_sha256="c" * 64,
                )

            evidence["plugin_runtime_sha256"] = "c" * 64
            evidence["workflow_sha256"] = "0" * 64
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
            with self.assertRaises(ResultSafetyError):
                load_ui_restore_evidence(
                    workspace,
                    evidence_path,
                    workflow,
                    host_commit=host_commit,
                    plugin_commit=plugin_commit,
                    plugin_runtime_sha256="c" * 64,
                )

            outside_evidence = Path(outside_text) / "evidence.json"
            outside_evidence.write_text(json.dumps(evidence), encoding="utf-8")
            with self.assertRaises(ResultSafetyError):
                load_ui_restore_evidence(
                    workspace,
                    outside_evidence,
                    workflow,
                    host_commit=host_commit,
                    plugin_commit=plugin_commit,
                    plugin_runtime_sha256="c" * 64,
                )

    def test_loader_returns_exactly_the_eight_matrix_scenarios(self):
        scenarios = load_runtime_scenarios(REPO_ROOT, MATRIX_PATH)
        self.assertEqual(len(scenarios), 8)
        self.assertEqual(len({scenario["id"] for scenario in scenarios}), 8)
        for scenario in scenarios:
            self.assertEqual(scenario["fixture"]["scenario_id"], scenario["id"])

    def test_metadata_roundtrip_loads_a_distinct_frontend_workflow_fixture(self):
        scenarios = load_runtime_scenarios(REPO_ROOT, MATRIX_PATH)
        scenario = next(row for row in scenarios if row["id"] == "metadata_enabled_roundtrip")
        self.assertNotEqual(scenario["api_fixture_path"], scenario["workflow_fixture_path"])
        self.assertEqual(scenario["fixture"]["workflow"]["version"], 0.4)
        self.assertEqual(
            {node["type"] for node in scenario["fixture"]["workflow"]["nodes"]},
            {"EmptyImage", "VHS_VideoCombine"},
        )
        empty_image = next(
            node for node in scenario["fixture"]["workflow"]["nodes"]
            if node["type"] == "EmptyImage"
        )
        self.assertEqual(empty_image["widgets_values"], [64, 48, 2, 4491468])
        self.assertEqual(empty_image["widgets_values_named"], {
            "width": 64,
            "height": 48,
            "batch_size": 2,
            "color": 4491468,
        })
        combine = next(
            node for node in scenario["fixture"]["workflow"]["nodes"]
            if node["type"] == "VHS_VideoCombine"
        )
        self.assertIsInstance(combine["widgets_values"], dict)
        self.assertEqual(combine["widgets_values"], combine["widgets_values_named"])
        self.assertEqual(combine["widgets_values"]["pix_fmt"], "yuv420p")
        self.assertEqual(combine["widgets_values"]["crf"], 19)
        self.assertEqual(combine["widgets_values_named"]["save_metadata"], True)

    def test_selection_requires_all_or_known_explicit_ids(self):
        scenarios = load_runtime_scenarios(REPO_ROOT, MATRIX_PATH)
        selected = select_runtime_scenarios(scenarios, run_all=False, requested_ids=[
            "metadata_disabled_utility_png",
            "no_audio_video_output",
        ])
        self.assertEqual(
            [scenario["id"] for scenario in selected],
            ["no_audio_video_output", "metadata_disabled_utility_png"],
        )
        with self.assertRaises(ResultSafetyError):
            select_runtime_scenarios(scenarios, run_all=False, requested_ids=[])
        with self.assertRaises(ResultSafetyError):
            select_runtime_scenarios(scenarios, run_all=False, requested_ids=["unknown"])

    def test_result_file_must_resolve_inside_workspace_and_not_through_alias(self):
        with tempfile.TemporaryDirectory() as workspace_text, tempfile.TemporaryDirectory() as outside_text:
            workspace = Path(workspace_text)
            result_file = resolve_result_file(workspace, workspace / ".tmp" / "results" / "result.json")
            self.assertTrue(result_file.parent.is_dir())
            self.assertTrue(result_file.resolve().is_relative_to(workspace.resolve()))

            with self.assertRaises(ResultSafetyError):
                resolve_result_file(workspace, Path(outside_text) / "result.json")

    def test_result_document_has_exact_scenario_ids_and_safe_relative_evidence(self):
        scenario_ids = [scenario["id"] for scenario in load_runtime_scenarios(REPO_ROOT, MATRIX_PATH)]
        rows = [
            {
                "scenario_id": scenario_id,
                "status": "passed",
                "checks": {"completed": True},
                "artifacts": [f"runtime/{scenario_id}/artifact.mp4"],
            }
            for scenario_id in scenario_ids
        ]

        document = build_result_document(
            scenario_ids,
            rows,
            host_commit="a" * 40,
            plugin_commit="b" * 40,
            plugin_runtime_sha256="c" * 64,
        )

        self.assertEqual(document["schema_version"], 2)
        self.assertEqual(document["plugin_runtime_sha256"], "c" * 64)
        self.assertEqual(document["summary"], {"passed": 8, "failed": 0})
        self.assertEqual([row["scenario_id"] for row in document["scenarios"]], scenario_ids)
        serialized = json.dumps(document)
        self.assertNotIn(str(REPO_ROOT), serialized)
        self.assertNotIn("prompt", serialized.lower())
        self.assertNotIn("history", serialized.lower())
        self.assertNotIn(":\\", serialized)

    def test_result_document_rejects_missing_duplicate_or_unsafe_rows(self):
        expected = ["one", "two"]
        with self.assertRaises(ResultSafetyError):
            build_result_document(expected, [{"scenario_id": "one", "status": "passed", "checks": {}, "artifacts": []}], host_commit="a" * 40, plugin_commit="b" * 40, plugin_runtime_sha256="c" * 64)
        with self.assertRaises(ResultSafetyError):
            build_result_document(expected, [
                {"scenario_id": "one", "status": "passed", "checks": {}, "artifacts": []},
                {"scenario_id": "one", "status": "passed", "checks": {}, "artifacts": []},
            ], host_commit="a" * 40, plugin_commit="b" * 40, plugin_runtime_sha256="c" * 64)
        with self.assertRaises(ResultSafetyError):
            build_result_document(expected, [
                {"scenario_id": "one", "status": "passed", "checks": {}, "artifacts": ["C:\\private\\artifact.mp4"]},
                {"scenario_id": "two", "status": "passed", "checks": {}, "artifacts": []},
            ], host_commit="a" * 40, plugin_commit="b" * 40, plugin_runtime_sha256="c" * 64)
        with self.assertRaises(ResultSafetyError):
            build_result_document(
                ["one"],
                [{"scenario_id": "one", "status": "passed", "checks": {"completed": False}, "artifacts": []}],
                host_commit="a" * 40,
                plugin_commit="b" * 40,
                plugin_runtime_sha256="c" * 64,
            )


class ScenarioEvaluationTests(unittest.TestCase):
    def _scenario(self, scenario_id):
        return next(
            row for row in load_runtime_scenarios(REPO_ROOT, MATRIX_PATH)
            if row["id"] == scenario_id
        )

    def test_no_audio_scenario_uses_contained_artifacts_and_ignores_history_fullpath(self):
        with tempfile.TemporaryDirectory() as output_text:
            output = Path(output_text)
            artifact = output / "runtime" / "no_audio_video_output" / "clip_64x48_00001.mp4"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"media")
            history = {
                "status": {"completed": True, "status_str": "success"},
                "outputs": {
                    "2": {"gifs": [{
                        "filename": artifact.name,
                        "subfolder": "runtime/no_audio_video_output",
                        "type": "output",
                        "fullpath": "C:\\private\\must-not-be-trusted.mp4"
                    }]}
                },
            }

            row = evaluate_scenario(
                self._scenario("no_audio_video_output"),
                history,
                output,
                files_before=set(),
                media_probe=lambda _path: {"streams": [{"codec_type": "video"}], "format": {"tags": {}}},
            )

            self.assertEqual(row["status"], "passed")
            self.assertTrue(all(row["checks"].values()))
            self.assertEqual(row["artifacts"], ["runtime/no_audio_video_output/clip_64x48_00001.mp4"])
            self.assertNotIn("private", json.dumps(row).lower())

    def test_run_scenarios_snapshots_files_before_each_prompt(self):
        scenario = self._scenario("no_audio_video_output")
        with tempfile.TemporaryDirectory() as output_text:
            output = Path(output_text)
            existing = output / "preexisting.txt"
            existing.write_text("keep", encoding="utf-8")

            class FakeClient:
                def submit_prompt(self, fixture):
                    self.fixture = fixture
                    artifact = output / "runtime" / fixture["scenario_id"] / "clip_64x48_00001.mp4"
                    artifact.parent.mkdir(parents=True)
                    artifact.write_bytes(b"media")
                    return "11111111-1111-4111-8111-111111111111"

                def wait_for_history(self, prompt_id, timeout, poll_interval):
                    self.prompt_id = prompt_id
                    return {
                        "status": {"completed": True, "status_str": "success"},
                        "outputs": {"2": {"gifs": [{
                            "filename": "clip_64x48_00001.mp4",
                            "subfolder": "runtime/no_audio_video_output",
                            "type": "output",
                        }]}},
                    }

            client = FakeClient()
            rows = run_scenarios(
                client,
                [scenario],
                output,
                media_probe=lambda _path: {"streams": [{"codec_type": "video"}], "format": {"tags": {}}},
                scenario_timeout=1,
            )

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "passed")
            self.assertNotIn("preexisting.txt", rows[0]["artifacts"])
            self.assertEqual(client.prompt_id, "11111111-1111-4111-8111-111111111111")

    def test_unsupported_audio_failure_requires_error_signal_and_zero_artifacts(self):
        with tempfile.TemporaryDirectory() as output_text:
            output = Path(output_text)
            history = {
                "status": {
                    "completed": False,
                    "status_str": "error",
                    "messages": [["execution_error", {"exception_message": "Audio is unsupported for this format"}]],
                },
                "outputs": {},
            }
            row = evaluate_scenario(
                self._scenario("unsupported_audio_format_failure"),
                history,
                output,
                files_before=set(),
                media_probe=lambda _path: {},
            )
            self.assertEqual(row["status"], "passed")
            self.assertEqual(row["artifacts"], [])
            self.assertTrue(all(row["checks"].values()))

            unexpected = output / "runtime" / "unsupported_audio_format_failure" / "partial.png"
            unexpected.parent.mkdir(parents=True)
            unexpected.write_bytes(b"partial")
            failed_row = evaluate_scenario(
                self._scenario("unsupported_audio_format_failure"),
                history,
                output,
                files_before=set(),
                media_probe=lambda _path: {},
            )
            self.assertEqual(failed_row["status"], "failed")
            self.assertFalse(failed_row["checks"]["zero_durable_artifacts"])

    def test_audio_scenario_requires_mux_name_both_streams_and_metadata(self):
        with tempfile.TemporaryDirectory() as output_text:
            output = Path(output_text)
            artifact = output / "runtime" / "audio_connected_output" / "clip_00001-audio.mp4"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"media")
            history = {
                "status": {"completed": True, "status_str": "success"},
                "outputs": {"3": {"gifs": [{"filename": artifact.name, "subfolder": "runtime/audio_connected_output", "type": "output"}]}},
            }
            row = evaluate_scenario(
                self._scenario("audio_connected_output"),
                history,
                output,
                files_before=set(),
                media_probe=lambda _path: {
                    "streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
                    "format": {"tags": {"comment": "workflow=present"}},
                },
            )
            self.assertEqual(row["status"], "passed")
            self.assertTrue(all(row["checks"].values()))

    def test_metadata_disabled_inspects_png_text_chunks_and_video_tags(self):
        def write_text_png(path, keyword):
            payload = keyword.encode("latin-1") + b"\0value"
            chunk_type = b"tEXt"
            crc = zlib.crc32(chunk_type + payload) & 0xFFFFFFFF
            path.write_bytes(
                b"\x89PNG\r\n\x1a\n"
                + struct.pack(">I", len(payload))
                + chunk_type
                + payload
                + struct.pack(">I", crc)
            )

        with tempfile.TemporaryDirectory() as output_text:
            output = Path(output_text)
            directory = output / "runtime" / "metadata_disabled_utility_png"
            directory.mkdir(parents=True)
            video = directory / "clip_00001.mp4"
            video.write_bytes(b"media")
            utility = directory / "clip_00001.png"
            write_text_png(utility, "CreationTime")
            history = {
                "status": {"completed": True, "status_str": "success"},
                "outputs": {"2": {"gifs": [{"filename": video.name, "subfolder": "runtime/metadata_disabled_utility_png", "type": "output"}]}},
            }
            probe = lambda _path: {"streams": [{"codec_type": "video"}], "format": {"tags": {"encoder": "test"}}}

            self.assertFalse(png_contains_workflow_metadata(utility))
            row = evaluate_scenario(
                self._scenario("metadata_disabled_utility_png"),
                history,
                output,
                files_before=set(),
                media_probe=probe,
            )
            self.assertEqual(row["status"], "passed")

            write_text_png(utility, "workflow")
            self.assertTrue(png_contains_workflow_metadata(utility))
            failed_row = evaluate_scenario(
                self._scenario("metadata_disabled_utility_png"),
                history,
                output,
                files_before=set(),
                media_probe=probe,
            )
            self.assertEqual(failed_row["status"], "failed")
            self.assertFalse(failed_row["checks"]["utility_png_has_no_workflow_text"])

    def test_image_sequence_variants_and_prune_execution(self):
        scenario = self._scenario("image_sequence_output_and_prune")
        variants = build_image_sequence_variants(scenario["fixture"])
        self.assertEqual(set(variants), {"concrete", "preview", "keep", "remove"})
        self.assertEqual(variants["preview"]["prompt"]["1"]["inputs"]["batch_size"], 1)
        self.assertEqual(variants["keep"]["prompt"]["3"]["inputs"]["filenames"], ["2", 0])
        self.assertEqual(variants["keep"]["prompt"]["3"]["inputs"]["options"], "Intermediate and Utility")
        self.assertEqual(variants["remove"]["prompt"]["3"]["inputs"]["options"], "All")

        with tempfile.TemporaryDirectory() as output_text:
            output = Path(output_text)

            class FakeClient:
                def __init__(self):
                    self.histories = {}
                    self.counter = 0

                def submit_prompt(self, fixture):
                    self.counter += 1
                    prompt_id = f"11111111-1111-4111-8111-{self.counter:012d}"
                    combine = fixture["prompt"]["2"]
                    prefix = combine["inputs"]["filename_prefix"]
                    directory = output / Path(prefix).parent
                    directory.mkdir(parents=True, exist_ok=True)
                    stem = Path(prefix).name + "_00001"
                    count = fixture["prompt"]["1"]["inputs"]["batch_size"]
                    frames = [directory / f"{stem}.{index:03d}.png" for index in range(1, count + 1)]
                    utility = directory / f"{stem}.png"
                    for path in [*frames, utility]:
                        path.write_bytes(b"png")
                    prune = fixture["prompt"].get("3")
                    if prune is not None:
                        utility.unlink()
                        if prune["inputs"]["options"] == "All":
                            for path in frames:
                                path.unlink()
                    preview_name = frames[0].name if count == 1 else f"{stem}.%03d.png"
                    self.histories[prompt_id] = {
                        "status": {"completed": True, "status_str": "success"},
                        "outputs": {"2": {"gifs": [{
                            "filename": preview_name,
                            "subfolder": Path(prefix).parent.as_posix(),
                            "type": "output",
                        }]}},
                    }
                    return prompt_id

                def wait_for_history(self, prompt_id, timeout, poll_interval):
                    return self.histories[prompt_id]

            row = run_image_sequence_scenario(
                FakeClient(),
                scenario,
                output,
                media_probe=lambda _path: {},
                scenario_timeout=1,
            )

            self.assertEqual(row["status"], "passed")
            self.assertTrue(all(row["checks"].values()))
            self.assertFalse(any("remove_" in path for path in row["artifacts"]))

    def test_filename_negative_variants_require_rejection_without_side_effects(self):
        scenario = self._scenario("filename_template_path_ux")
        variants = build_filename_negative_variants(scenario["fixture"])
        self.assertEqual(set(variants), {"outside", "cross_drive"})
        self.assertTrue(variants["outside"]["prompt"]["2"]["inputs"]["filename_prefix"].startswith("../"))
        self.assertEqual(
            Path(variants["cross_drive"]["prompt"]["2"]["inputs"]["filename_prefix"]).drive.upper(),
            "Z:",
        )

        with tempfile.TemporaryDirectory() as sandbox_text:
            sandbox = Path(sandbox_text)
            output = sandbox / "output"
            output.mkdir()

            class FakeClient:
                def __init__(self):
                    self.history = None

                def submit_prompt(self, fixture):
                    prefix = fixture["prompt"]["2"]["inputs"]["filename_prefix"]
                    if prefix.startswith("../") or Path(prefix).drive:
                        raise ComfyApiError("rejected")
                    artifact = output / "runtime" / "filename_template_path_ux" / "clip_64x48_00001.mp4"
                    artifact.parent.mkdir(parents=True)
                    artifact.write_bytes(b"media")
                    self.history = {
                        "status": {"completed": True, "status_str": "success"},
                        "outputs": {"2": {"gifs": [{
                            "filename": artifact.name,
                            "subfolder": "runtime/filename_template_path_ux",
                            "type": "output",
                        }]}},
                    }
                    return "11111111-1111-4111-8111-111111111111"

                def wait_for_history(self, prompt_id, timeout, poll_interval):
                    return self.history

            row = run_filename_scenario(
                FakeClient(),
                scenario,
                output,
                containment_root=sandbox,
                media_probe=lambda _path: {"streams": [{"codec_type": "video"}], "format": {"tags": {}}},
                scenario_timeout=1,
            )

            self.assertEqual(row["status"], "passed")
            self.assertTrue(all(row["checks"].values()))
            self.assertFalse((sandbox / "vhs-runtime-outside").exists())

    def test_failing_ffmpeg_shim_and_partial_cleanup_orchestration(self):
        scenario = self._scenario("partial_artifact_cleanup_and_prune_safety")
        with tempfile.TemporaryDirectory() as workspace_text:
            workspace = Path(workspace_text)
            layout = __import__("scripts.runtime.harness", fromlist=["RuntimeLayout"]).RuntimeLayout.create(
                workspace,
                workspace / ".tmp" / "runtime" / "fault",
            )
            shim = write_failing_ffmpeg_shim(layout)
            marker = layout.temp / "fault-marker.txt"
            direct_output = layout.output / "direct-partial.mp4"
            environment = os.environ.copy()
            environment["VHS_RUNTIME_SHIM_MARKER"] = str(marker)
            completed = __import__("subprocess").run(
                [str(shim), "-v", "error", str(direct_output)],
                input=b"frame-bytes",
                capture_output=True,
                env=environment,
                timeout=10,
                check=False,
            )
            self.assertEqual(completed.returncode, 23)
            self.assertTrue(direct_output.is_file())
            self.assertTrue(marker.is_file())
            direct_output.unlink()
            marker.unlink()

            inside = layout.output / "runtime" / scenario["id"] / "prune-inside.tmp"
            outside = layout.user / "prune-outside.tmp"
            support_node = write_runtime_support_node(layout)
            self.assertTrue((support_node / "__init__.py").is_file())
            prune_prompt = build_prune_safety_prompt(inside, outside)
            self.assertEqual(prune_prompt["prompt"]["1"]["class_type"], "VHS_RuntimeFilenames")
            self.assertEqual(prune_prompt["prompt"]["1"]["inputs"], {
                "inside_path": str(inside),
                "outside_path": str(outside),
            })
            self.assertEqual(prune_prompt["prompt"]["2"]["inputs"]["filenames"], ["1", 0])

            class FakeClient:
                def __init__(self):
                    self.counter = 0
                    self.histories = {}

                def submit_prompt(self, fixture):
                    self.counter += 1
                    prompt_id = f"11111111-1111-4111-8111-{self.counter:012d}"
                    if self.counter == 1:
                        marker.write_text("partial-created", encoding="utf-8")
                    message = (
                        "controlled failure"
                        if self.counter == 1
                        else "Tried to prune output from invalid directory"
                    )
                    self.histories[prompt_id] = {
                        "status": {
                            "completed": False,
                            "status_str": "error",
                            "messages": [["execution_error", {"exception_message": message}]],
                        },
                        "outputs": {},
                    }
                    return prompt_id

                def wait_for_history(self, prompt_id, timeout, poll_interval):
                    return self.histories[prompt_id]

            row = run_partial_cleanup_scenario(
                FakeClient(),
                scenario,
                layout.output,
                temp_root=layout.temp,
                user_root=layout.user,
                fault_marker=marker,
                media_probe=lambda _path: {},
                scenario_timeout=1,
            )
            self.assertEqual(row["status"], "passed")
            self.assertTrue(all(row["checks"].values()))
            self.assertFalse(marker.exists())
            self.assertFalse(inside.exists())
            self.assertFalse(outside.exists())


if __name__ == "__main__":
    unittest.main()

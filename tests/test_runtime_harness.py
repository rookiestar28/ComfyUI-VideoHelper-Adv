import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.runtime.harness import (
    ContainmentError,
    OwnedComfyUIProcess,
    PluginSnapshotError,
    ProcessOwnershipError,
    RuntimeLayout,
    TrustedHostError,
    build_comfyui_command,
    copy_runtime_plugin,
    runtime_plugin_sha256,
    validate_trusted_host,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = REPO_ROOT / "tests" / "runtime_validation_matrix.json"
RUNTIME_PROMPT_ROOT = REPO_ROOT / "tests" / "runtime_prompts"


def _all_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from _all_strings(key)
            yield from _all_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _all_strings(child)


class RuntimeLayoutTests(unittest.TestCase):
    def test_layout_creates_canonical_roots_inside_workspace(self):
        with tempfile.TemporaryDirectory() as workspace_text:
            workspace = Path(workspace_text)
            layout = RuntimeLayout.create(workspace, workspace / ".tmp" / "runtime" / "run-1")

            self.assertEqual(
                {"sandbox", "base", "input", "output", "temp", "user", "results", "logs"},
                set(layout.as_dict()),
            )
            for path in layout.as_dict().values():
                self.assertTrue(path.is_dir())
                self.assertTrue(path.resolve().is_relative_to(workspace.resolve()))

    def test_layout_rejects_outside_workspace_before_creation(self):
        with tempfile.TemporaryDirectory() as workspace_text, tempfile.TemporaryDirectory() as outside_text:
            workspace = Path(workspace_text)
            outside = Path(outside_text) / "runtime"

            with self.assertRaises(ContainmentError):
                RuntimeLayout.create(workspace, outside)
            self.assertFalse(outside.exists())

    def test_layout_rejects_resolved_symlink_or_junction_escape(self):
        with tempfile.TemporaryDirectory() as workspace_text, tempfile.TemporaryDirectory() as outside_text:
            workspace = Path(workspace_text)
            sandbox = workspace / ".tmp" / "runtime" / "run-escape"
            original_resolve = Path.resolve

            def escaped_resolve(path, *args, **kwargs):
                if path == sandbox:
                    return Path(outside_text).resolve()
                return original_resolve(path, *args, **kwargs)

            with mock.patch.object(Path, "resolve", escaped_resolve):
                with self.assertRaises(ContainmentError):
                    RuntimeLayout.create(workspace, sandbox)

    def test_layout_rejects_cross_drive_commonpath_failure(self):
        with tempfile.TemporaryDirectory() as workspace_text:
            workspace = Path(workspace_text)
            sandbox = workspace / ".tmp" / "runtime" / "run-cross-drive"
            with mock.patch("scripts.runtime.harness.os.path.commonpath", side_effect=ValueError("different drives")):
                with self.assertRaises(ContainmentError):
                    RuntimeLayout.create(workspace, sandbox)


class TrustedHostTests(unittest.TestCase):
    def test_reference_comfyui_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as workspace_text:
            workspace = Path(workspace_text)
            reference_host = workspace / "reference" / "ComfyUI"
            reference_host.mkdir(parents=True)
            (reference_host / "main.py").write_text("", encoding="utf-8")
            interpreter = workspace / ".venv" / "Scripts" / "python.exe"
            interpreter.parent.mkdir(parents=True)
            interpreter.write_text("", encoding="utf-8")

            with self.assertRaisesRegex(TrustedHostError, "reference"):
                validate_trusted_host(workspace, reference_host, interpreter)

    def test_reference_interpreter_is_rejected(self):
        with tempfile.TemporaryDirectory() as workspace_text, tempfile.TemporaryDirectory() as host_text:
            workspace = Path(workspace_text)
            host = Path(host_text)
            (host / "main.py").write_text("", encoding="utf-8")
            interpreter = workspace / "reference" / "ComfyUI" / ".venv" / "Scripts" / "python.exe"
            interpreter.parent.mkdir(parents=True)
            interpreter.write_text("", encoding="utf-8")

            with self.assertRaisesRegex(TrustedHostError, "reference"):
                validate_trusted_host(workspace, host, interpreter)

    def test_missing_host_contract_is_rejected(self):
        with tempfile.TemporaryDirectory() as workspace_text, tempfile.TemporaryDirectory() as host_text:
            workspace = Path(workspace_text)
            host = Path(host_text)
            interpreter = workspace / ".venv" / "Scripts" / "python.exe"
            interpreter.parent.mkdir(parents=True)
            interpreter.write_text("", encoding="utf-8")

            with self.assertRaisesRegex(TrustedHostError, "main.py"):
                validate_trusted_host(workspace, host, interpreter)


class LaunchCommandTests(unittest.TestCase):
    def test_command_uses_only_loopback_and_all_contained_roots(self):
        with tempfile.TemporaryDirectory() as workspace_text, tempfile.TemporaryDirectory() as host_text:
            workspace = Path(workspace_text)
            layout = RuntimeLayout.create(workspace, workspace / ".tmp" / "runtime" / "run-command")
            host = Path(host_text)
            (host / "main.py").write_text("", encoding="utf-8")
            interpreter = workspace / ".venv" / "Scripts" / "python.exe"
            interpreter.parent.mkdir(parents=True)
            interpreter.write_text("", encoding="utf-8")

            trusted = validate_trusted_host(workspace, host, interpreter)
            command = build_comfyui_command(trusted, layout, port=18288)

            self.assertEqual(command[0], str(interpreter.resolve()))
            self.assertEqual(command[1], str((host / "main.py").resolve()))
            expected_pairs = {
                "--listen": "127.0.0.1",
                "--port": "18288",
                "--base-directory": str(layout.base),
                "--input-directory": str(layout.input),
                "--output-directory": str(layout.output),
                  "--temp-directory": str(layout.temp),
                  "--user-directory": str(layout.user),
                  "--database-url": "sqlite:///:memory:",
              }
            for flag, expected in expected_pairs.items():
                index = command.index(flag)
                self.assertEqual(command[index + 1], expected)
            self.assertIn("--disable-auto-launch", command)

            extended = build_comfyui_command(
                trusted,
                layout,
                port=18289,
                additional_whitelist=("VHS-RuntimeHarness",),
            )
            whitelist_index = extended.index("--whitelist-custom-nodes")
            self.assertEqual(
                extended[whitelist_index + 1:],
                ["ComfyUI-VideoHelper_Adv", "VHS-RuntimeHarness"],
            )


class PluginSnapshotTests(unittest.TestCase):
    def _make_plugin(self, workspace):
        (workspace / "__init__.py").write_text("NODE_CLASS_MAPPINGS = {}\n", encoding="utf-8")
        for directory in ("videohelpersuite", "video_formats", "web"):
            path = workspace / directory
            path.mkdir()
            (path / "kept.txt").write_text(directory, encoding="utf-8")
        (workspace / "private-note.txt").write_text("must not copy", encoding="utf-8")

    def test_snapshot_copies_only_runtime_allowlist(self):
        with tempfile.TemporaryDirectory() as workspace_text:
            workspace = Path(workspace_text)
            self._make_plugin(workspace)
            layout = RuntimeLayout.create(workspace, workspace / ".tmp" / "runtime" / "snapshot")

            target = copy_runtime_plugin(workspace, layout)

            self.assertEqual(target.parent, layout.base / "custom_nodes")
            self.assertEqual(
                {"__init__.py", "videohelpersuite", "video_formats", "web"},
                {path.name for path in target.iterdir()},
            )
            self.assertFalse((target / "private-note.txt").exists())

    def test_runtime_plugin_digest_tracks_copied_sources_only(self):
        with tempfile.TemporaryDirectory() as workspace_text:
            workspace = Path(workspace_text)
            self._make_plugin(workspace)
            first = runtime_plugin_sha256(workspace)
            cache = workspace / "web" / "__pycache__"
            cache.mkdir()
            (cache / "ignored.pyc").write_bytes(b"ignored")
            self.assertEqual(runtime_plugin_sha256(workspace), first)

            (workspace / "web" / "kept.txt").write_text("changed", encoding="utf-8")
            self.assertNotEqual(runtime_plugin_sha256(workspace), first)

    def test_snapshot_refuses_existing_target(self):
        with tempfile.TemporaryDirectory() as workspace_text:
            workspace = Path(workspace_text)
            self._make_plugin(workspace)
            layout = RuntimeLayout.create(workspace, workspace / ".tmp" / "runtime" / "existing")
            target = layout.base / "custom_nodes" / "ComfyUI-VideoHelper_Adv"
            target.mkdir(parents=True)

            with self.assertRaisesRegex(PluginSnapshotError, "already exists"):
                copy_runtime_plugin(workspace, layout)

    def test_snapshot_rejects_reparse_points_before_copy(self):
        with tempfile.TemporaryDirectory() as workspace_text:
            workspace = Path(workspace_text)
            self._make_plugin(workspace)
            layout = RuntimeLayout.create(workspace, workspace / ".tmp" / "runtime" / "reparse")
            escaped = workspace / "videohelpersuite" / "kept.txt"

            with mock.patch(
                "scripts.runtime.harness._is_reparse_point",
                side_effect=lambda path: Path(path).name == escaped.name,
            ):
                with self.assertRaisesRegex(PluginSnapshotError, "reparse"):
                    copy_runtime_plugin(workspace, layout)

            self.assertFalse((layout.base / "custom_nodes" / "ComfyUI-VideoHelper_Adv").exists())


class _FakeProcess:
    def __init__(self, wait_timeout=False):
        self.pid = 4242
        self.returncode = None
        self.terminated = False
        self.killed = False
        self.wait_timeout = wait_timeout
        self.wait_calls = 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def wait(self, timeout):
        self.wait_calls += 1
        if self.wait_timeout and self.wait_calls == 1:
            raise TimeoutError("still running")
        self.returncode = -9 if self.killed else 0
        return self.returncode

    def kill(self):
        self.killed = True


class OwnedProcessTests(unittest.TestCase):
    def test_owned_process_starts_once_and_terminates_cleanly(self):
        fake = _FakeProcess()
        calls = []

        def factory(command, **kwargs):
            calls.append((command, kwargs))
            return fake

        owned = OwnedComfyUIProcess(
            ["python", "main.py"],
            Path("trusted-host"),
            popen_factory=factory,
            env={"VHS_RUNTIME_TEST": "1"},
        )
        with owned as running:
            self.assertEqual(running.pid, 4242)
            self.assertIsNone(running.poll())
            with self.assertRaises(ProcessOwnershipError):
                owned.start()

        self.assertTrue(fake.terminated)
        self.assertFalse(fake.killed)
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0][1]["stdout"], __import__("subprocess").DEVNULL)
        self.assertEqual(calls[0][1]["stderr"], __import__("subprocess").STDOUT)
        self.assertEqual(calls[0][1]["cwd"], str(Path("trusted-host")))
        self.assertEqual(calls[0][1]["env"], {"VHS_RUNTIME_TEST": "1"})

    def test_owned_process_uses_bounded_kill_fallback(self):
        fake = _FakeProcess(wait_timeout=True)
        owned = OwnedComfyUIProcess(
            ["python", "main.py"],
            Path("trusted-host"),
            popen_factory=lambda *_args, **_kwargs: fake,
            stop_timeout=0.01,
        )
        owned.start()
        owned.stop()

        self.assertTrue(fake.terminated)
        self.assertTrue(fake.killed)
        self.assertEqual(fake.wait_calls, 2)

    def test_stop_never_targets_an_unowned_process(self):
        owned = OwnedComfyUIProcess(
            ["python", "main.py"],
            Path("trusted-host"),
            popen_factory=lambda *_args, **_kwargs: _FakeProcess(),
        )
        with self.assertRaises(ProcessOwnershipError):
            owned.stop()


class RuntimePromptFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))

    def test_matrix_maps_exactly_one_api_fixture_per_scenario(self):
        mapped = {}
        for scenario in self.matrix["scenarios"]:
            fixture_path = scenario.get("api_fixture_path")
            self.assertIsInstance(fixture_path, str, scenario["id"])
            self.assertTrue(fixture_path.startswith("tests/runtime_prompts/"), scenario["id"])
            self.assertTrue((REPO_ROOT / fixture_path).is_file(), fixture_path)
            mapped[scenario["id"]] = fixture_path

        self.assertEqual(len(mapped), 8)
        self.assertEqual(len(set(mapped.values())), 8)

    def test_api_fixtures_are_deterministic_model_free_and_external_url_free(self):
        allowed_nodes = {"EmptyImage", "EmptyAudio", "VHS_VideoCombine", "VHS_PruneOutputs"}
        fixture_paths = sorted(RUNTIME_PROMPT_ROOT.glob("*.json"))
        self.assertEqual(len(fixture_paths), 8)

        for fixture_path in fixture_paths:
            fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
            self.assertEqual(fixture["schema_version"], 1)
            self.assertEqual(fixture_path.stem, fixture["scenario_id"])
            self.assertIn(fixture["expected_outcome"], {"success", "failure"})
            self.assertIsInstance(fixture["assertions"], list)
            self.assertTrue(fixture["assertions"])
            self.assertIsInstance(fixture["prompt"], dict)
            self.assertTrue(fixture["prompt"])

            for node in fixture["prompt"].values():
                self.assertIn(node["class_type"], allowed_nodes, fixture_path.name)
            for value in _all_strings(fixture):
                self.assertNotIn("://", value, fixture_path.name)
                self.assertNotIn("reference/", value.replace("\\", "/").lower(), fixture_path.name)

    def test_api_fixture_filename_prefixes_are_scenario_scoped(self):
        for fixture_path in sorted(RUNTIME_PROMPT_ROOT.glob("*.json")):
            fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
            scenario_id = fixture["scenario_id"]
            combines = [
                node for node in fixture["prompt"].values()
                if node["class_type"] == "VHS_VideoCombine"
            ]
            self.assertTrue(combines, scenario_id)
            for combine in combines:
                prefix = combine["inputs"]["filename_prefix"].replace("\\", "/")
                self.assertTrue(prefix.startswith(f"runtime/{scenario_id}/"), prefix)
                self.assertNotIn("..", Path(prefix).parts)


if __name__ == "__main__":
    unittest.main()

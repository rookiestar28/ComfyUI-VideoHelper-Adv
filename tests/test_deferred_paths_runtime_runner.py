import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "runtime" / "run_deferred_paths_matrix.py"
UI_SERVER = ROOT / "scripts" / "runtime" / "serve_deferred_ui.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location("deferred_paths_runner", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DeferredPathRuntimeRunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runner = _load_runner()

    def test_video_prompts_separate_complex_only_and_mixed_filter_paths(self):
        complex_only = self.runner._video_prompt("contained/complex", 0)
        mixed = self.runner._video_prompt("contained/mixed", 1)

        self.assertEqual(complex_only["prompt"]["2"]["inputs"]["format"], "video/ffmpeg-gif")
        self.assertEqual(complex_only["prompt"]["2"]["inputs"]["loop_count"], 0)
        self.assertEqual(mixed["prompt"]["2"]["inputs"]["loop_count"], 1)

    def test_result_shape_is_content_free(self):
        source = RUNNER.read_text(encoding="utf-8")

        self.assertIn('"checks": checks', source)
        self.assertIn('"plugin_runtime_sha256"', source)
        self.assertNotIn('"workflow":', source)
        self.assertNotIn('"media_path":', source)

    def test_runner_uses_owned_host_and_workspace_containment(self):
        source = RUNNER.read_text(encoding="utf-8")

        self.assertIn("with OwnedComfyUIProcess", source)
        self.assertIn("validate_trusted_host", source)
        self.assertIn("copy_runtime_plugin", source)
        self.assertIn("VHS_RuntimeStringSink", source)
        self.assertIn('"127.0.0.1"', source)
        self.assertIn("_contained(workspace, sandbox)", source)

    def test_ui_server_is_bounded_owned_and_stop_file_controlled(self):
        source = UI_SERVER.read_text(encoding="utf-8")

        self.assertIn("with OwnedComfyUIProcess", source)
        self.assertIn("client.wait_until_ready", source)
        self.assertIn("args.max_seconds", source)
        self.assertIn("stop_file.is_file()", source)
        self.assertIn("runtime_plugin_sha256", source)


if __name__ == "__main__":
    unittest.main()

import json
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = REPO_ROOT / "tests" / "runtime_validation_matrix.json"

REQUIRED_SCENARIOS = {
    "no_audio_video_output",
    "audio_connected_output",
    "unsupported_audio_format_failure",
    "metadata_enabled_roundtrip",
    "metadata_disabled_utility_png",
    "image_sequence_output_and_prune",
    "filename_template_path_ux",
    "partial_artifact_cleanup_and_prune_safety",
}

REQUIRED_ITEMS = {"R21", "R22", "R23", "R24", "R25", "R26"}
VALID_RUNTIME_STATUSES = {"not_run_no_local_harness", "passed"}


class RuntimeValidationMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
        cls.scenarios = cls.matrix["scenarios"]

    def test_required_scenarios_are_present(self):
        scenario_ids = {scenario["id"] for scenario in self.scenarios}
        self.assertEqual(REQUIRED_SCENARIOS - scenario_ids, set())

    def test_phase_items_are_covered(self):
        covered_items = {
            item
            for scenario in self.scenarios
            for item in scenario.get("items", [])
        }
        self.assertEqual(REQUIRED_ITEMS - covered_items, set())

    def test_fixture_paths_exist_or_have_gap_reason(self):
        for scenario in self.scenarios:
            fixture_paths = scenario.get("fixture_paths", [])
            if not fixture_paths:
                self.assertTrue(scenario.get("fixture_gap_reason"), scenario["id"])
                continue
            for fixture_path in fixture_paths:
                self.assertTrue((REPO_ROOT / fixture_path).is_file(), fixture_path)

    def test_repo_substitute_paths_exist(self):
        for scenario in self.scenarios:
            substitutes = scenario.get("repo_substitutes", [])
            self.assertTrue(substitutes, scenario["id"])
            for substitute in substitutes:
                path = substitute.get("path")
                self.assertTrue(path, scenario["id"])
                if substitute.get("type") == "source_reference":
                    normalized = path.replace("\\", "/").lower()
                    self.assertTrue(normalized.startswith("reference/"), path)
                    # Internal reference repositories are intentionally absent in clean CI.
                    continue
                self.assertTrue((REPO_ROOT / path).exists(), path)

    def test_runtime_statuses_are_explicit_and_safe(self):
        for scenario in self.scenarios:
            status = scenario.get("runtime_status")
            self.assertIn(status, VALID_RUNTIME_STATUSES, scenario["id"])
            self.assertIs(scenario.get("runtime_required"), True, scenario["id"])
            self.assertTrue(scenario.get("runtime_observations_required"), scenario["id"])
            if status == "not_run_no_local_harness":
                self.assertTrue(scenario.get("not_run_reason"), scenario["id"])
            if status == "passed":
                evidence = scenario.get("runtime_evidence", [])
                self.assertTrue(evidence, scenario["id"])


if __name__ == "__main__":
    unittest.main()

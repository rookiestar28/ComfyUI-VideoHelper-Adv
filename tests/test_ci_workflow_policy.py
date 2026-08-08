import re
import unittest
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "validate.yml"


class CIWorkflowPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = WORKFLOW_PATH.read_text(encoding="utf-8")
        cls.workflow = yaml.safe_load(cls.raw)
        cls.triggers = cls.workflow.get("on", cls.workflow.get(True))
        cls.job = cls.workflow["jobs"]["validate"]

    def test_triggers_concurrency_permissions_and_timeout(self):
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})
        self.assertIn("pull_request", self.triggers)
        self.assertEqual(set(self.triggers["push"]["branches"]), {"dev", "main"})
        self.assertIs(self.workflow["concurrency"]["cancel-in-progress"], True)
        self.assertLessEqual(self.job["timeout-minutes"], 30)

    def test_required_os_python_and_node_matrix_is_explicit(self):
        include = self.job["strategy"]["matrix"]["include"]
        lanes = {(row["os"], str(row["python"])) for row in include}
        self.assertEqual(
            lanes,
            {
                ("ubuntu-latest", "3.10"),
                ("ubuntu-latest", "3.12"),
                ("windows-latest", "3.12"),
            },
        )
        setup_node = next(
            step for step in self.job["steps"]
            if str(step.get("uses", "")).startswith("actions/setup-node@")
        )
        self.assertGreaterEqual(int(setup_node["with"]["node-version"]), 18)

    def test_actions_are_full_sha_pinned_and_checkout_drops_credentials(self):
        action_steps = [step for step in self.job["steps"] if "uses" in step]
        self.assertTrue(action_steps)
        for step in action_steps:
            self.assertRegex(step["uses"], r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}$")
        checkout = next(
            step for step in action_steps
            if step["uses"].startswith("actions/checkout@")
        )
        self.assertIs(checkout["with"]["persist-credentials"], False)

    def test_jobs_are_publication_incapable_and_keep_r32_separate(self):
        lowered = self.raw.lower()
        for forbidden in (
            "secrets.",
            "registry_access_token",
            "publish-node",
            "upload-artifact",
            "run_runtime_matrix",
            "runtime_validation_matrix.json",
            "reference/",
            ".planning/",
            "roadmap.md",
            "output/playwright",
        ):
            self.assertNotIn(forbidden, lowered)
        self.assertNotIn("environment", self.job)
        self.assertNotIn("permissions", self.job)
        self.assertNotRegex(lowered, r"\b(write|write-all)\b")

    def test_workflow_uses_native_wrappers_and_declared_test_requirements(self):
        run_source = "\n".join(
            step.get("run", "") for step in self.job["steps"] if "run" in step
        )
        self.assertIn("requirements-test.txt", run_source)
        self.assertIn("scripts/run_pre_push_checks.sh", run_source)
        self.assertIn("scripts/run_repo_checks.ps1", run_source)
        self.assertNotIn("${{ github.event", run_source)

        requirements = (REPO_ROOT / "requirements-test.txt").read_text(encoding="utf-8")
        self.assertIn("-r requirements.txt", requirements)
        self.assertIn("Pillow", requirements)
        self.assertIn("PyYAML", requirements)


if __name__ == "__main__":
    unittest.main()

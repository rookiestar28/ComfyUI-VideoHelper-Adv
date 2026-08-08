import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts.run_repo_checks import (
    CheckStep,
    build_check_steps,
    is_project_venv_python,
    parse_node_major,
    run_steps,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


class RepoCheckRunnerTests(unittest.TestCase):
    def test_build_steps_cover_repo_contract_without_runtime_or_reference(self):
        python = Path("project-python")
        node = Path("node")
        steps = build_check_steps(REPO_ROOT, python, node)
        names = [step.name for step in steps]

        self.assertEqual(names[0:2], ["python-compile", "python-unit"])
        self.assertEqual(names[-2:], ["video-format-validation", "git-diff-check"])
        self.assertEqual(
            sum(name.startswith("javascript-syntax:") for name in names),
            len(list((REPO_ROOT / "web" / "js").glob("*.js"))),
        )
        self.assertEqual(
            sum(name.startswith("javascript-test:") for name in names),
            len(list((REPO_ROOT / "tests" / "js").glob("*.test.mjs"))),
        )
        serialized = "\n".join(" ".join(map(str, step.command)) for step in steps).lower()
        self.assertNotIn("reference", serialized)
        self.assertNotIn("run_runtime_matrix", serialized)
        self.assertNotIn("runtime_validation_matrix.json", serialized)
        self.assertTrue(all(step.command and isinstance(step.command, tuple) for step in steps))

    def test_run_steps_stops_at_and_returns_first_real_failure(self):
        steps = [
            CheckStep("first", ("tool", "first")),
            CheckStep("second", ("tool", "second")),
            CheckStep("must-not-run", ("tool", "third")),
        ]
        calls = []

        def fake_runner(command, **kwargs):
            calls.append((tuple(command), kwargs))
            return SimpleNamespace(returncode=7 if len(calls) == 2 else 0)

        result = run_steps(steps, REPO_ROOT, runner=fake_runner)

        self.assertEqual(result, 7)
        self.assertEqual([call[0][-1] for call in calls], ["first", "second"])
        self.assertTrue(all(call[1]["cwd"] == str(REPO_ROOT) for call in calls))
        self.assertTrue(all(call[1]["check"] is False for call in calls))

    def test_node_floor_and_project_venv_detection_are_fail_closed(self):
        self.assertEqual(parse_node_major("v18.20.8"), 18)
        self.assertEqual(parse_node_major("v20.20.2"), 20)
        with self.assertRaises(ValueError):
            parse_node_major("v17.9.1")
        with self.assertRaises(ValueError):
            parse_node_major("not-a-version")

        with tempfile.TemporaryDirectory() as root_text, tempfile.TemporaryDirectory() as outside_text:
            root = Path(root_text)
            local = root / ".venv" / "Scripts" / "python.exe"
            local.parent.mkdir(parents=True)
            local.write_text("", encoding="utf-8")
            outside = Path(outside_text) / "python.exe"
            outside.write_text("", encoding="utf-8")
            self.assertTrue(is_project_venv_python(root, local))
            self.assertFalse(is_project_venv_python(root, outside))

    def test_shell_wrappers_delegate_to_the_shared_runner(self):
        bash_source = (REPO_ROOT / "scripts" / "run_pre_push_checks.sh").read_text(encoding="utf-8")
        windows_source = (REPO_ROOT / "scripts" / "run_repo_checks.ps1").read_text(encoding="utf-8")

        self.assertIn("scripts/run_repo_checks.py", bash_source)
        self.assertNotIn("compileall", bash_source)
        self.assertIn("wslpath -w", bash_source)
        self.assertIn("scripts\\run_repo_checks.py", windows_source)
        self.assertIn(".venv\\Scripts\\python.exe", windows_source)


if __name__ == "__main__":
    unittest.main()

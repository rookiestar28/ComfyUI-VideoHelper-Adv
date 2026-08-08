import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.registry_publish_guard import decide_should_publish


REPO_ROOT = Path(__file__).resolve().parents[1]


def _project(version: str) -> str:
    return f'''[project]
name = "comfyui-videohelper-adv"
version = "{version}"
'''


class RegistryPublishGuardTests(unittest.TestCase):
    def test_version_change_is_detected_across_explicit_baseline(self):
        with tempfile.TemporaryDirectory() as root_text:
            root = Path(root_text)
            current = root / "current.toml"
            previous = root / "previous.toml"
            current.write_text(_project("2.0.0"), encoding="utf-8")
            previous.write_text(_project("1.7.9"), encoding="utf-8")

            decision = decide_should_publish(
                pyproject=current,
                previous_pyproject=previous,
            )

        self.assertTrue(decision.should_publish)
        self.assertEqual(decision.reason, "version_changed")
        self.assertEqual(decision.current_version, "2.0.0")
        self.assertEqual(decision.previous_version, "1.7.9")

    def test_unchanged_version_is_skipped(self):
        with tempfile.TemporaryDirectory() as root_text:
            root = Path(root_text)
            current = root / "current.toml"
            previous = root / "previous.toml"
            current.write_text(_project("2.0.0"), encoding="utf-8")
            previous.write_text(_project("2.0.0"), encoding="utf-8")

            decision = decide_should_publish(
                pyproject=current,
                previous_pyproject=previous,
            )

        self.assertFalse(decision.should_publish)
        self.assertEqual(decision.reason, "version_unchanged")

    def test_missing_or_malformed_baseline_fails_closed(self):
        with tempfile.TemporaryDirectory() as root_text:
            root = Path(root_text)
            current = root / "current.toml"
            current.write_text(_project("2.0.0"), encoding="utf-8")
            with self.assertRaises(ValueError):
                decide_should_publish(
                    pyproject=current,
                    previous_pyproject=root / "missing.toml",
                )

            malformed = root / "malformed.toml"
            malformed.write_text("[project]\nname='missing-version'\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                decide_should_publish(
                    pyproject=current,
                    previous_pyproject=malformed,
                )

    def test_cli_detects_pending_multi_commit_fork_version_change(self):
        with tempfile.TemporaryDirectory() as repo_text:
            repo = Path(repo_text)
            subprocess.run(("git", "init", "-q", str(repo)), check=True)
            subprocess.run(
                ("git", "config", "user.name", "Registry Guard Test"),
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ("git", "config", "user.email", "registry-guard@example.invalid"),
                cwd=repo,
                check=True,
            )
            pyproject = repo / "pyproject.toml"
            pyproject.write_text(_project("1.7.9"), encoding="utf-8")
            subprocess.run(("git", "add", "pyproject.toml"), cwd=repo, check=True)
            subprocess.run(("git", "commit", "-qm", "baseline"), cwd=repo, check=True)
            previous_ref = subprocess.run(
                ("git", "rev-parse", "HEAD"),
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            (repo / "unrelated.txt").write_text("middle commit\n", encoding="utf-8")
            subprocess.run(("git", "add", "unrelated.txt"), cwd=repo, check=True)
            subprocess.run(("git", "commit", "-qm", "middle"), cwd=repo, check=True)
            pyproject.write_text(_project("2.0.0"), encoding="utf-8")
            subprocess.run(("git", "add", "pyproject.toml"), cwd=repo, check=True)
            subprocess.run(("git", "commit", "-qm", "release"), cwd=repo, check=True)

            output = repo / "github-output.txt"
            result = subprocess.run(
                (
                    sys.executable,
                    str(REPO_ROOT / "scripts" / "registry_publish_guard.py"),
                    "--pyproject",
                    "pyproject.toml",
                    "--previous-ref",
                    previous_ref,
                    "--github-output",
                    str(output),
                ),
                cwd=repo,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            outputs = dict(
                line.split("=", 1)
                for line in output.read_text(encoding="utf-8").splitlines()
            )
        self.assertEqual(outputs["should_publish"], "true")
        self.assertEqual(outputs["reason"], "version_changed")
        self.assertEqual(outputs["current_version"], "2.0.0")
        self.assertEqual(outputs["previous_version"], "1.7.9")


if __name__ == "__main__":
    unittest.main()

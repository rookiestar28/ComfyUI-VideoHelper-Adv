import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts import run_repo_checks
from scripts.run_repo_checks import (
    CheckStep,
    build_check_steps,
    is_project_venv_python,
    parse_node_major,
    run_steps,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SHELL_ENTRY_POINTS = (
    ".githooks/pre-push",
    "scripts/install_git_hooks.sh",
    "scripts/run_pre_push_checks.sh",
)


def _isolated_git_env():
    # Keep fixture repositories independent of the developer's global/system Git config.
    return {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def _git(repo, *args):
    return subprocess.run(
        ("git", *args),
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
        env=_isolated_git_env(),
    ).stdout.strip()


def _diff_check_steps(steps):
    return [step for step in steps if step.name.startswith("git-diff-check:")]


def _quiet_runner(command, **kwargs):
    kwargs.setdefault("capture_output", True)
    return subprocess.run(command, env=_isolated_git_env(), **kwargs)


class RepoCheckRunnerTests(unittest.TestCase):
    def test_build_steps_cover_repo_contract_without_runtime_or_reference(self):
        python = Path("project-python")
        node = Path("node")
        steps = build_check_steps(REPO_ROOT, python, node)
        names = [step.name for step in steps]

        self.assertEqual(names[0:2], ["python-compile", "python-unit"])
        self.assertEqual(
            names[-3:],
            ["video-format-validation", "git-diff-check:unstaged", "git-diff-check:staged"],
        )
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

            posix_launcher = root / ".venv" / "bin" / "python"
            # POSIX venv launchers normally symlink to a base interpreter outside the venv.
            with mock.patch.object(
                Path,
                "resolve",
                side_effect=AssertionError("venv containment must not dereference the launcher"),
            ):
                self.assertTrue(is_project_venv_python(root, posix_launcher))

    def test_shell_wrappers_delegate_to_the_shared_runner(self):
        bash_source = (REPO_ROOT / "scripts" / "run_pre_push_checks.sh").read_text(encoding="utf-8")
        windows_source = (REPO_ROOT / "scripts" / "run_repo_checks.ps1").read_text(encoding="utf-8")

        self.assertIn("scripts/run_repo_checks.py", bash_source)
        self.assertNotIn("compileall", bash_source)
        self.assertIn("wslpath -w", bash_source)
        self.assertIn('"$runner_path" "$@"', bash_source)
        self.assertIn("scripts\\run_repo_checks.py", windows_source)
        self.assertIn(".venv\\Scripts\\python.exe", windows_source)
        self.assertIn("& $Python $Runner @args", windows_source)

    @unittest.skipUnless(
        shutil.which("powershell") or shutil.which("pwsh"),
        "PowerShell is not available",
    )
    def test_windows_wrapper_reports_missing_venv_with_exit_code_two(self):
        shell = shutil.which("powershell") or shutil.which("pwsh")
        with tempfile.TemporaryDirectory() as root_text:
            wrapper = Path(root_text) / "scripts" / "run_repo_checks.ps1"
            wrapper.parent.mkdir()
            shutil.copyfile(REPO_ROOT / "scripts" / "run_repo_checks.ps1", wrapper)
            result = subprocess.run(
                (shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)),
                cwd=root_text,
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("No repo-local Python found", result.stderr)

    def test_diff_checks_cover_unstaged_staged_and_explicit_candidate_range(self):
        base = "a" * 40
        without_base = _diff_check_steps(build_check_steps(REPO_ROOT, Path("python"), Path("node")))
        with_base = _diff_check_steps(
            build_check_steps(REPO_ROOT, Path("python"), Path("node"), diff_base=base)
        )

        self.assertEqual(
            [(step.name, step.command) for step in without_base],
            [
                ("git-diff-check:unstaged", ("git", "diff", "--check")),
                ("git-diff-check:staged", ("git", "diff", "--cached", "--check")),
            ],
        )
        self.assertEqual(
            (with_base[-1].name, with_base[-1].command),
            ("git-diff-check:candidate", ("git", "diff", "--check", f"{base}...HEAD")),
        )

    def test_diff_base_normalization_fails_closed(self):
        for absent in (None, "", "   ", "0" * 40, "0" * 64):
            with self.subTest(absent=absent):
                self.assertIsNone(run_repo_checks.normalize_diff_base(absent))
        for accepted in ("origin/main", "main", "c309a6d", "HEAD~1"):
            with self.subTest(accepted=accepted):
                self.assertEqual(run_repo_checks.normalize_diff_base(f" {accepted} "), accepted)
        for rejected in ("-x", "--output=diff.txt", "main dev", "main\ndev", "@{u}", "a" * 300):
            with self.subTest(rejected=rejected):
                with self.assertRaises(ValueError):
                    run_repo_checks.normalize_diff_base(rejected)

    @unittest.skipUnless(shutil.which("git"), "git is not available")
    def test_git_diff_checks_detect_staged_and_candidate_whitespace(self):
        with tempfile.TemporaryDirectory() as root_text:
            repo = Path(root_text)
            _git(repo, "init", "-q")
            _git(repo, "config", "user.email", "checks@example.invalid")
            _git(repo, "config", "user.name", "Repo Checks")
            (repo / "clean.txt").write_text("clean\n", encoding="utf-8")
            _git(repo, "add", "clean.txt")
            _git(repo, "commit", "-q", "-m", "base")
            base = _git(repo, "rev-parse", "HEAD")

            self.assertEqual(run_steps(_diff_check_steps(build_check_steps(repo, Path("p"), Path("n"))), repo, runner=_quiet_runner), 0)

            (repo / "staged.txt").write_text("trailing \n", encoding="utf-8")
            _git(repo, "add", "staged.txt")
            staged_steps = _diff_check_steps(build_check_steps(repo, Path("p"), Path("n")))
            self.assertNotEqual(run_steps(staged_steps, repo, runner=_quiet_runner), 0)

            _git(repo, "commit", "-q", "-m", "candidate")
            resolved = run_repo_checks.resolve_diff_base(repo, base, runner=_quiet_runner)
            self.assertEqual(resolved, base)
            clean_tree_steps = _diff_check_steps(build_check_steps(repo, Path("p"), Path("n")))
            self.assertEqual(run_steps(clean_tree_steps, repo, runner=_quiet_runner), 0)
            candidate_steps = _diff_check_steps(
                build_check_steps(repo, Path("p"), Path("n"), diff_base=resolved)
            )
            self.assertNotEqual(run_steps(candidate_steps, repo, runner=_quiet_runner), 0)
            head_steps = _diff_check_steps(
                build_check_steps(repo, Path("p"), Path("n"), diff_base=run_repo_checks.resolve_diff_base(repo, "HEAD", runner=_quiet_runner))
            )
            self.assertEqual(run_steps(head_steps, repo, runner=_quiet_runner), 0)

            self.assertIsNone(run_repo_checks.resolve_diff_base(repo, "0" * 40, runner=_quiet_runner))
            with self.assertRaises(ValueError):
                run_repo_checks.resolve_diff_base(repo, "f" * 40, runner=_quiet_runner)

    @unittest.skipUnless(shutil.which("git"), "git is not available")
    def test_shell_entry_points_are_tracked_executable(self):
        inside = subprocess.run(
            ("git", "rev-parse", "--is-inside-work-tree"),
            cwd=str(REPO_ROOT),
            check=False,
            capture_output=True,
            text=True,
        )
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            self.skipTest("not a git checkout")
        listing = _git(REPO_ROOT, "ls-files", "-s", "--", *SHELL_ENTRY_POINTS)
        modes = {line.split("\t", 1)[1]: line.split(" ", 1)[0] for line in listing.splitlines()}

        self.assertEqual(modes, {path: "100755" for path in SHELL_ENTRY_POINTS})


if __name__ == "__main__":
    unittest.main()

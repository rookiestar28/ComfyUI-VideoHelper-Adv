#!/usr/bin/env python3
"""Run the deterministic repository checks shared by local and hosted CI."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


@dataclass(frozen=True)
class CheckStep:
    """One shell-free validation command and its optional standard-input file."""

    name: str
    command: tuple[str, ...]
    stdin_path: Path | None = None


def build_check_steps(repo_root: Path, python: Path, node: Path) -> list[CheckStep]:
    """Build the ordered, platform-neutral repository validation contract."""

    python_text = str(python)
    node_text = str(node)
    steps = [
        CheckStep(
            "python-compile",
            (
                python_text,
                "-m",
                "compileall",
                "videohelpersuite",
                "__init__.py",
                "scripts",
                "tests",
            ),
        ),
        CheckStep(
            "python-unit",
            (python_text, "scripts/run_unittests.py"),
        ),
    ]

    for js_file in sorted((repo_root / "web" / "js").glob("*.js")):
        steps.append(
            CheckStep(
                f"javascript-syntax:{js_file.name}",
                (node_text, "--input-type=module", "--check"),
                stdin_path=js_file,
            )
        )

    for js_test in sorted((repo_root / "tests" / "js").glob("*.test.mjs")):
        steps.append(
            CheckStep(
                f"javascript-test:{js_test.name}",
                (node_text, "--test", str(js_test.relative_to(repo_root))),
            )
        )

    steps.extend(
        (
            CheckStep(
                "video-format-validation",
                (python_text, "scripts/validate_video_formats.py"),
            ),
            CheckStep("git-diff-check", ("git", "diff", "--check")),
        )
    )
    return steps


def run_steps(
    steps: Sequence[CheckStep],
    repo_root: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> int:
    """Run checks in order and return immediately on the first non-zero result."""

    for step in steps:
        print(f"[repo-checks] Running {step.name}: {' '.join(step.command)}", flush=True)
        if step.stdin_path is None:
            result = runner(step.command, cwd=str(repo_root), check=False)
        else:
            with step.stdin_path.open("rb") as stdin_file:
                result = runner(
                    step.command,
                    cwd=str(repo_root),
                    check=False,
                    stdin=stdin_file,
                )
        if result.returncode != 0:
            print(
                f"[repo-checks] FAILED {step.name} (exit {result.returncode}).",
                file=sys.stderr,
                flush=True,
            )
            return int(result.returncode)

    print("[repo-checks] All repository checks passed.", flush=True)
    return 0


def parse_node_major(version: str) -> int:
    """Parse a Node version and enforce the repository's minimum major."""

    match = re.fullmatch(r"v(\d+)(?:\.\d+){2}", version.strip())
    if match is None:
        raise ValueError(f"Unrecognized Node.js version: {version!r}")
    major = int(match.group(1))
    if major < 18:
        raise ValueError(f"Node.js 18+ is required; found {version.strip()}")
    return major


def is_project_venv_python(repo_root: Path, executable: Path) -> bool:
    """Return whether the interpreter is contained by an approved repo-local venv."""

    resolved_executable = executable.resolve()
    for venv_name in (".venv", ".venv-wsl"):
        resolved_venv = (repo_root / venv_name).resolve()
        if resolved_executable == resolved_venv or resolved_venv in resolved_executable.parents:
            return True
    return False


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    python = Path(sys.executable)
    if not is_project_venv_python(repo_root, python):
        print(
            "ERROR: run_repo_checks.py requires this repository's .venv or .venv-wsl. "
            "Create it with 'python -m venv .venv', install requirements-test.txt, "
            "then use that environment's Python.",
            file=sys.stderr,
        )
        return 2

    node_text = shutil.which("node")
    if node_text is None:
        print("ERROR: Node.js 18+ is required but node was not found on PATH.", file=sys.stderr)
        return 2
    node = Path(node_text)
    version_result = subprocess.run(
        (str(node), "--version"),
        cwd=str(repo_root),
        check=False,
        capture_output=True,
        text=True,
    )
    try:
        if version_result.returncode != 0:
            raise ValueError(version_result.stderr.strip() or "node --version failed")
        major = parse_node_major(version_result.stdout)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(f"[repo-checks] Python: {python}", flush=True)
    print(f"[repo-checks] Node: {version_result.stdout.strip()} ({node}); major={major}", flush=True)
    return run_steps(build_check_steps(repo_root, python, node), repo_root)


if __name__ == "__main__":
    raise SystemExit(main())

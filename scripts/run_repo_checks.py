#!/usr/bin/env python3
"""Run the deterministic repository checks shared by local and hosted CI."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


DIFF_BASE_ENV = "VHS_DIFF_CHECK_BASE"
_ABSENT_COMMIT = re.compile(r"0{40}|0{64}")
_DIFF_BASE_REF = re.compile(r"[0-9A-Za-z][0-9A-Za-z._/~^-]{0,254}")
_COMMIT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


@dataclass(frozen=True)
class CheckStep:
    """One shell-free validation command and its optional standard-input file."""

    name: str
    command: tuple[str, ...]
    stdin_path: Path | None = None


def build_check_steps(
    repo_root: Path,
    python: Path,
    node: Path,
    diff_base: str | None = None,
) -> list[CheckStep]:
    """Build the ordered, platform-neutral repository validation contract.

    ``diff_base`` is a resolved commit ID; when present, committed candidate changes
    since its merge base with ``HEAD`` are whitespace-checked as well.
    """

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
            CheckStep("git-diff-check:unstaged", ("git", "diff", "--check")),
            CheckStep("git-diff-check:staged", ("git", "diff", "--cached", "--check")),
        )
    )
    if diff_base is not None:
        steps.append(
            CheckStep(
                "git-diff-check:candidate",
                ("git", "diff", "--check", f"{diff_base}...HEAD"),
            )
        )
    return steps


def normalize_diff_base(value: str | None) -> str | None:
    """Return a candidate base ref, or ``None`` when no candidate range applies.

    Empty values and the all-zero ID that Git hosts send for newly created refs mean
    "no candidate range". Anything else must look like a plain commit ID or ref name.
    """

    if value is None:
        return None
    text = value.strip()
    if not text or _ABSENT_COMMIT.fullmatch(text):
        return None
    # SECURITY: the value becomes a git argument; a leading '-' would be parsed as an option.
    if _DIFF_BASE_REF.fullmatch(text) is None:
        raise ValueError(f"Unsupported diff base {text!r}; pass a commit ID or a ref name.")
    return text


def resolve_diff_base(
    repo_root: Path,
    value: str | None,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str | None:
    """Resolve an optional diff base to a commit ID, failing closed when it is unreachable."""

    ref = normalize_diff_base(value)
    if ref is None:
        return None
    result = runner(
        ("git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"),
        cwd=str(repo_root),
        check=False,
        capture_output=True,
        text=True,
    )
    commit = (result.stdout or "").strip() if result.returncode == 0 else ""
    if _COMMIT_ID.fullmatch(commit) is None:
        raise ValueError(
            f"Diff base {ref!r} is not a commit in this checkout; "
            "fetch its history or pass a reachable base."
        )
    return commit


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

    # SECURITY: compare the invoked path lexically; POSIX venv launchers legitimately
    # symlink to a base interpreter, while normpath/commonpath still reject `..` escapes.
    executable_path = os.path.normcase(os.path.abspath(os.fspath(executable)))
    for venv_name in (".venv", ".venv-wsl"):
        venv_path = os.path.normcase(
            os.path.abspath(os.fspath(repo_root / venv_name))
        )
        try:
            contained = os.path.commonpath((venv_path, executable_path)) == venv_path
        except ValueError:
            contained = False
        if contained:
            return True
    return False


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--diff-base",
        default=None,
        help=(
            "Commit or ref whose merge base with HEAD starts the committed candidate range "
            f"for whitespace checks (default: ${DIFF_BASE_ENV}, else no candidate range)."
        ),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
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

    base_source = "--diff-base" if args.diff_base is not None else DIFF_BASE_ENV
    base_value = args.diff_base if args.diff_base is not None else os.environ.get(DIFF_BASE_ENV)
    try:
        diff_base = resolve_diff_base(repo_root, base_value)
    except ValueError as exc:
        print(f"ERROR: {base_source}: {exc}", file=sys.stderr)
        return 2

    print(f"[repo-checks] Python: {python}", flush=True)
    print(f"[repo-checks] Node: {version_result.stdout.strip()} ({node}); major={major}", flush=True)
    if diff_base is None:
        print("[repo-checks] Diff base: none (unstaged and staged changes only)", flush=True)
    else:
        print(f"[repo-checks] Diff base: {diff_base} (from {base_source})", flush=True)
    return run_steps(build_check_steps(repo_root, python, node, diff_base), repo_root)


if __name__ == "__main__":
    raise SystemExit(main())

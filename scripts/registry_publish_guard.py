#!/usr/bin/env python3
"""Fail-closed version guard for automatic Comfy Registry publication."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 CI lane.
    import tomli as tomllib


_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_FULL_COMMIT = re.compile(r"^[0-9a-fA-F]{40}$")


@dataclass(frozen=True)
class PublishDecision:
    should_publish: bool
    reason: str
    current_version: str
    previous_version: str


def _version_from_bytes(payload: bytes, source: str) -> str:
    try:
        document = tomllib.loads(payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"invalid pyproject metadata in {source}") from exc
    project = document.get("project")
    version = project.get("version") if isinstance(project, dict) else None
    if not isinstance(version, str) or _SEMVER.fullmatch(version) is None:
        raise ValueError(f"missing or invalid semantic project version in {source}")
    return version


def read_project_version(path: Path) -> str:
    if not path.is_file():
        raise ValueError("current pyproject is missing")
    return _version_from_bytes(path.read_bytes(), "current pyproject")


def _read_version_at_ref(pyproject: Path, previous_ref: str) -> str:
    # SECURITY: accept only immutable commit IDs supplied by the push event.
    if _FULL_COMMIT.fullmatch(previous_ref) is None:
        raise ValueError("previous ref must be a full commit ID")
    root_result = subprocess.run(
        ("git", "-C", str(pyproject.parent), "rev-parse", "--show-toplevel"),
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if root_result.returncode != 0:
        raise ValueError("repository root could not be identified")
    root = Path(root_result.stdout.strip()).resolve(strict=True)
    try:
        relative = pyproject.resolve(strict=True).relative_to(root).as_posix()
    except (OSError, ValueError) as exc:
        raise ValueError("pyproject must stay inside the repository") from exc
    result = subprocess.run(
        ("git", "-C", str(root), "show", f"{previous_ref}:{relative}"),
        check=False,
        capture_output=True,
        timeout=15,
    )
    if result.returncode != 0:
        raise ValueError("previous pyproject version could not be read")
    return _version_from_bytes(result.stdout, "previous pyproject")


def decide_should_publish(
    *,
    pyproject: Path,
    previous_pyproject: Path | None = None,
    previous_ref: str | None = None,
) -> PublishDecision:
    if (previous_pyproject is None) == (previous_ref is None):
        raise ValueError("exactly one previous-version source is required")
    current_version = read_project_version(pyproject)
    if previous_pyproject is not None:
        if not previous_pyproject.is_file():
            raise ValueError("previous pyproject is missing")
        previous_version = _version_from_bytes(
            previous_pyproject.read_bytes(),
            "previous pyproject",
        )
    else:
        previous_version = _read_version_at_ref(pyproject, previous_ref or "")
    changed = current_version != previous_version
    return PublishDecision(
        should_publish=changed,
        reason="version_changed" if changed else "version_unchanged",
        current_version=current_version,
        previous_version=previous_version,
    )


def _write_outputs(path: Path, decision: PublishDecision) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"should_publish={'true' if decision.should_publish else 'false'}\n")
        handle.write(f"reason={decision.reason}\n")
        handle.write(f"current_version={decision.current_version}\n")
        handle.write(f"previous_version={decision.previous_version}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    previous = parser.add_mutually_exclusive_group(required=True)
    previous.add_argument("--previous-ref")
    previous.add_argument("--previous-pyproject", type=Path)
    parser.add_argument("--github-output", type=Path, required=True)
    args = parser.parse_args()
    decision = decide_should_publish(
        pyproject=args.pyproject,
        previous_pyproject=args.previous_pyproject,
        previous_ref=args.previous_ref,
    )
    _write_outputs(args.github_output, decision)
    print(
        "Registry publish guard: "
        f"should_publish={str(decision.should_publish).lower()} "
        f"reason={decision.reason} "
        f"current_version={decision.current_version} "
        f"previous_version={decision.previous_version}"
    )
    return 0


def cli() -> int:
    try:
        return main()
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(cli())

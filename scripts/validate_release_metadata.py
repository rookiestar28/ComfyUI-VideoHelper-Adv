#!/usr/bin/env python3
"""Validate immutable release identity, dispatch inputs, and a dry-run archive."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

try:
    from scripts.release_guardrails import (
        APPROVED_NODE_ID,
        build_release_archive,
        validate_release_metadata,
    )
except ModuleNotFoundError:  # Direct `python scripts/...` execution.
    from release_guardrails import (  # type: ignore[no-redef]
        APPROVED_NODE_ID,
        build_release_archive,
        validate_release_metadata,
    )


_FULL_COMMIT = re.compile(r"^[0-9a-f]{40}$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-version")
    parser.add_argument("--operation", choices=("preflight", "publish"), default="preflight")
    parser.add_argument("--confirmation", default="")
    parser.add_argument("--github-ref", default="")
    parser.add_argument("--github-sha", default="")
    parser.add_argument("--approved-commit", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--archive", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    metadata = validate_release_metadata(repo_root)
    if args.expected_version and args.expected_version != metadata.version:
        raise ValueError("workflow version input does not match pyproject.toml")

    if args.github_ref:
        allowed_preflight_refs = {"refs/heads/dev", "refs/heads/main"}
        if args.operation == "publish":
            expected_ref = "refs/heads/main"
            expected_confirmation = f"PUBLISH {APPROVED_NODE_ID} {metadata.version}"
            if args.github_ref != expected_ref:
                raise ValueError(f"publish recovery requires the protected ref {expected_ref!r}")
            if args.confirmation != expected_confirmation:
                raise ValueError("publish confirmation does not match the approved node/version")
            if _FULL_COMMIT.fullmatch(args.approved_commit) is None:
                raise ValueError("publish recovery requires an approved full commit SHA")
            if args.github_sha != args.approved_commit:
                raise ValueError("workflow commit does not match the approved commit SHA")
        elif args.github_ref not in allowed_preflight_refs:
            raise ValueError("preflight ref is not an approved branch")

    if args.archive is not None:
        report = build_release_archive(repo_root, args.archive)
        archive_summary = {
            "file_count": report.file_count,
            "sha256": report.sha256,
            "total_uncompressed_bytes": report.total_uncompressed_bytes,
        }
    else:
        archive_summary = None

    print(
        json.dumps(
            {
                "archive": archive_summary,
                "dry_run": bool(args.dry_run),
                "node_id": metadata.node_id,
                "operation": args.operation,
                "publisher_id": metadata.publisher_id,
                "version": metadata.version,
            },
            sort_keys=True,
        )
    )
    return 0


def cli() -> int:
    try:
        return main()
    except (FileExistsError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(cli())

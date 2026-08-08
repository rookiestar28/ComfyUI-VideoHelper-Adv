#!/usr/bin/env python3
"""Inspect an already-built Comfy Registry candidate archive."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    from scripts.release_guardrails import inspect_release_archive
except ModuleNotFoundError:  # Direct `python scripts/...` execution.
    from release_guardrails import inspect_release_archive  # type: ignore[no-redef]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    report = inspect_release_archive(args.archive)
    print(
        json.dumps(
            {
                "file_count": report.file_count,
                "sha256": report.sha256,
                "total_uncompressed_bytes": report.total_uncompressed_bytes,
            },
            sort_keys=True,
        )
    )
    return 0


def cli() -> int:
    try:
        return main()
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(cli())

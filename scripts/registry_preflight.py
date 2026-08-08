#!/usr/bin/env python3
"""Run authenticated, read-only Comfy Registry ownership checks."""

from __future__ import annotations

import argparse
import json
import os
import sys

try:
    from scripts.release_guardrails import preflight_registry
except ModuleNotFoundError:  # Direct `python scripts/...` execution.
    from release_guardrails import preflight_registry  # type: ignore[no-redef]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--publisher", required=True)
    parser.add_argument("--node", required=True)
    args = parser.parse_args()
    # SECURITY: the token is read only from the protected process environment;
    # never accept it as a command argument or include it in the result.
    token = os.environ.get("REGISTRY_ACCESS_TOKEN", "")
    result = preflight_registry(args.publisher, args.node, token)
    print(json.dumps(result, sort_keys=True))
    return 0


def cli() -> int:
    try:
        return main()
    except (PermissionError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(cli())

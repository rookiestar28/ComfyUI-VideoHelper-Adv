#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
cd "$REPO_ROOT"

chmod +x "$REPO_ROOT/.githooks/pre-push" "$REPO_ROOT/scripts/run_pre_push_checks.sh"
git config core.hooksPath .githooks

printf 'Configured core.hooksPath=%s\n' ".githooks"

#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

for candidate in \
  "$REPO_ROOT/.venv-wsl/bin/python" \
  "$REPO_ROOT/.venv/bin/python" \
  "$REPO_ROOT/.venv/Scripts/python.exe"
do
  if [ -x "$candidate" ]; then
    runner_path="$REPO_ROOT/scripts/run_repo_checks.py"
    case "$candidate:$(uname -s 2>/dev/null || true)" in
      *.exe:Linux*)
        # IMPORTANT: WSL must translate the script path before invoking Windows Python.
        if command -v wslpath >/dev/null 2>&1; then
          runner_path=$(wslpath -w "$runner_path")
        fi
        ;;
    esac
    exec "$candidate" "$runner_path"
  fi
done

printf '%s\n' \
  'ERROR: no repo-local Python found. Create .venv (or .venv-wsl), install requirements-test.txt, and retry.' >&2
exit 2

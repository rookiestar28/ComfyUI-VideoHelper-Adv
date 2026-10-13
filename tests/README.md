# Tests

- `test_*.py`: Python unit tests, run by `scripts/run_unittests.py`.
- `js/*.test.mjs`: Node.js built-in tests for the frontend modules in `web/js/`.
- `*.json` (top level): saved ComfyUI workflows kept as compatibility fixtures;
  `runtime_validation_matrix.json` defines the owned runtime matrix scenarios.
- `runtime_prompts/` and `runtime_workflows/`: deterministic API prompts and
  frontend workflows used by the runtime matrix runners in `scripts/runtime/`.

Run every repository check with `scripts/run_repo_checks.ps1` on Windows or
`scripts/run_pre_push_checks.sh` on Linux and Git Bash. See "Testing This Fork"
in the top-level `README.md` for setup and the live-host runners.

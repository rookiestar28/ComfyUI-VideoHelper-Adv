$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$Runner = Join-Path $RepoRoot "scripts\run_repo_checks.py"

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    Write-Error "No repo-local Python found at .venv\Scripts\python.exe. Create .venv, install requirements-test.txt, and retry."
    exit 2
}

& $Python $Runner
exit $LASTEXITCODE

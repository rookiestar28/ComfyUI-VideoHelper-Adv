$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$Runner = Join-Path $RepoRoot "scripts\run_repo_checks.py"

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    # IMPORTANT: Write-Error is terminating under Stop and exits 1; setup errors must exit 2.
    [Console]::Error.WriteLine("ERROR: No repo-local Python found at .venv\Scripts\python.exe. Create .venv, install requirements-test.txt, and retry.")
    exit 2
}

& $Python $Runner @args
exit $LASTEXITCODE

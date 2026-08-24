param(
    [switch]$NoVenvCreate,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repoRoot

$venvPython = Join-Path $repoRoot '.venv\Scripts\python.exe'
$uiScript = Join-Path $repoRoot 'web\light_ui.py'

if (-not (Test-Path $uiScript)) {
    throw "UI script not found: $uiScript"
}

if (-not (Test-Path $venvPython)) {
    if ($NoVenvCreate) {
        throw "Virtual environment missing at .venv and -NoVenvCreate was set."
    }

    Write-Host "[1/3] Creating virtual environment..." -ForegroundColor Cyan
    $basePython = Get-Command python -ErrorAction SilentlyContinue
    if (-not $basePython) {
        throw "Python is not available in PATH. Install Python 3.12+ and retry."
    }
    & python -m venv .venv
}

Write-Host "[2/3] Checking UI script syntax..." -ForegroundColor Cyan
& $venvPython -m py_compile $uiScript

$commandPreview = "`"$venvPython`" `"$uiScript`""
Write-Host "[3/3] Launching Light UI..." -ForegroundColor Cyan
Write-Host "Command: $commandPreview" -ForegroundColor DarkGray

if ($DryRun) {
    Write-Host "Dry run complete. UI was not started." -ForegroundColor Yellow
    exit 0
}

& $venvPython $uiScript

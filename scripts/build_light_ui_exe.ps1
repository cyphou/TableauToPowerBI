param(
    [switch]$Clean
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $repoRoot

$pythonLauncher = Get-Command py -ErrorAction SilentlyContinue
if (-not $pythonLauncher) {
    throw "Python Launcher is required. Install Python 3.13 from python.org or winget."
}

& py -3.13 -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is missing for Python 3.13. Install it with: py -3.13 -m pip install pyinstaller"
}

if ($Clean) {
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue build, dist
}

& py -3.13 -m PyInstaller `
    --noconfirm `
    --clean `
    --onedir `
    --windowed `
    --name TableauToPowerBI `
    --distpath dist\windows `
    --workpath build\windows `
    --add-data "migrate.py;." `
    --add-data "tableau_export;tableau_export" `
    --add-data "powerbi_import;powerbi_import" `
    --add-data "web;web" `
    desktop_launcher.py

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

Get-ChildItem -Path (Join-Path $repoRoot 'dist\windows\TableauToPowerBI') `
    -Directory -Filter '__pycache__' -Recurse -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force

Write-Host "Autonomous app created: $repoRoot\dist\windows\TableauToPowerBI\TableauToPowerBI.exe" -ForegroundColor Green
Write-Host "Copy the complete TableauToPowerBI folder; no Python, PowerShell, or repository checkout is required at runtime." -ForegroundColor Green

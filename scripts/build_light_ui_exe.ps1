param(
    [switch]$Clean
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $repoRoot

$pyInstaller = Get-Command pyinstaller -ErrorAction SilentlyContinue
if (-not $pyInstaller) {
    throw "PyInstaller is required. Install it with: python -m pip install pyinstaller"
}

if ($Clean) {
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue build, dist
}

& pyinstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name TableauToPowerBI `
    desktop_launcher.py

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

Write-Host "EXE created: $repoRoot\dist\TableauToPowerBI.exe" -ForegroundColor Green
Write-Host "The EXE expects the repository checkout beside it or TTPBI_ENGINE_ROOT." -ForegroundColor Yellow

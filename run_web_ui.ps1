param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8765,
    [switch]$NoBrowser,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    $python = (Get-Command python -ErrorAction Stop).Source
}
$server = Join-Path $repoRoot 'web\local_server.py'
$serverArgs = @($server, '--port', "$Port")
if ($NoBrowser) {
    $serverArgs += '--no-browser'
}
Write-Host "Local migration UI: http://127.0.0.1:$Port" -ForegroundColor Cyan
Write-Host 'Stop the server with Ctrl+C. Native diagnostics require a separate confirmation.'
if ($DryRun) {
    Write-Host "Python: $python"
    Write-Host "Server: $server"
    exit 0
}
& $python @serverArgs
exit $LASTEXITCODE

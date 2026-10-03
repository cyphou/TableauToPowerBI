$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$proc = Get-Process PBIDesktop -ErrorAction SilentlyContinue |
    Where-Object { $_.MainWindowTitle -ne '' } | Select-Object -First 1
if (-not $proc) { Write-Output 'NO WINDOW'; exit 0 }
$root = [Windows.Automation.AutomationElement]::FromHandle($proc.MainWindowHandle)
$seen = @{}
foreach ($e in $root.FindAll([Windows.Automation.TreeScope]::Descendants,
                             [Windows.Automation.Condition]::TrueCondition)) {
    $n = $e.Current.Name
    if ([string]::IsNullOrWhiteSpace($n)) { continue }
    if ($n.Length -lt 12) { continue }
    if ($seen.ContainsKey($n)) { continue }
    $seen[$n] = $true
    Write-Output ("[{0}] {1}" -f $e.Current.ControlType.ProgrammaticName, $n)
}

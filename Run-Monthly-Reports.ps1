param(
    [string]$DatabasePath,
    [string]$LogPath,
    [int]$Year,
    [int]$Month
)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$backendPath = Join-Path $projectRoot 'backend'
$runtimePath = Join-Path $projectRoot '.runtime'
$python = Join-Path $runtimePath '.venv\Scripts\python.exe'

if (!(Test-Path -LiteralPath $python)) {
    throw 'RentMate runtime is missing. Run Start-RentMate.cmd once before installing or running the monthly task.'
}
if (!$DatabasePath) { $DatabasePath = Join-Path $runtimePath 'rentmate-demo.db' }
if (!$LogPath) { $LogPath = Join-Path $runtimePath 'monthly-report-runs.jsonl' }
if (($Year -eq 0) -xor ($Month -eq 0)) {
    throw 'Year and Month must be supplied together.'
}

$arguments = @('-m', 'reporting.monthly_runner', '--database', $DatabasePath, '--log', $LogPath)
if ($Year -ne 0) { $arguments += @('--year', $Year, '--month', $Month) }

Set-Location $backendPath
& $python @arguments
if ($LASTEXITCODE -ne 0) {
    throw "Monthly report run completed with failures. Inspect $LogPath"
}
Write-Host "Monthly report run completed. Execution record: $LogPath"

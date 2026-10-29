param(
    [string]$TaskName = 'RentMate Monthly Reports',
    [string]$StartTime = '02:00',
    [string]$DatabasePath,
    [string]$LogPath
)

$ErrorActionPreference = 'Stop'
$runner = Join-Path $PSScriptRoot 'Run-Monthly-Reports.ps1'
if (!(Test-Path -LiteralPath $runner)) { throw "Runner not found: $runner" }
if (!$DatabasePath) { $DatabasePath = Join-Path $PSScriptRoot '.runtime\rentmate-demo.db' }
if (!$LogPath) { $LogPath = Join-Path $PSScriptRoot '.runtime\monthly-report-runs.jsonl' }
if ($StartTime -notmatch '^([01]\d|2[0-3]):[0-5]\d$') {
    throw 'StartTime must use 24-hour HH:mm format.'
}

$taskCommand = (
    "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$runner`" " +
    "-DatabasePath `"$DatabasePath`" -LogPath `"$LogPath`""
)
& schtasks.exe /Create /TN $TaskName /TR $taskCommand /SC MONTHLY /D 1 /ST $StartTime /F
if ($LASTEXITCODE -ne 0) { throw 'Windows Task Scheduler registration failed.' }

Write-Host "Registered '$TaskName' for day 1 of every month at $StartTime."
Write-Host "Database: $DatabasePath"
Write-Host "The task generates the previous calendar month and writes: $LogPath"

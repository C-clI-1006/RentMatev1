param([switch]$SkipLive)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$runner = Join-Path $projectRoot 'Start-RentMate.ps1'

Write-Host 'RentMate Stage 2 verification'
Write-Host 'This checks the full suite, named end-to-end scenarios, and Campus Qwen.'
Write-Host ''

if ($SkipLive) {
    & $runner -Verify
} else {
    & $runner -Verify -Live
}

if ($LASTEXITCODE -ne 0) {
    throw 'RentMate verification failed.'
}

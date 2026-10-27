param([switch]$Verify)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$backendPath = Join-Path $projectRoot 'backend'
$runtimePath = Join-Path $projectRoot '.runtime'
New-Item -ItemType Directory -Force -Path $runtimePath | Out-Null

$python = Join-Path $runtimePath '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $python)) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $pythonCommand) {
        throw 'Python 3.11+ was not found. Install Python and run this launcher again.'
    }
    & $pythonCommand.Source -m venv (Join-Path $runtimePath '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the local demo environment' }
}

& $python -c 'import fastapi, uvicorn, pydantic, multipart, pypdf, pytest'
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Installing RentMate dependencies into the local demo environment...'
    & $python -m pip install -r (Join-Path $backendPath 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
}

$env:DATABASE_PATH = Join-Path $runtimePath 'rentmate-demo.db'
$env:EVIDENCE_STORAGE_ROOT = Join-Path $runtimePath 'evidence'
$env:RENTMATE_SCHEDULER_TOKEN = 'scheduler-demo'
$env:RENTMATE_AUTH_TOKENS = @{
    'tenant-demo' = @{tenant_id='demo-tenant'; property_id='demo-property'}
    'manager-demo' = @{tenant_id='demo-tenant'; property_id='demo-property'; manager_id='demo-manager'}
} | ConvertTo-Json -Compress

Set-Location $backendPath
& $python -m scripts.seed_integrated_demo --database $env:DATABASE_PATH
if ($LASTEXITCODE -ne 0) { throw 'Integrated demo seed failed' }

if ($Verify) {
    $env:PYTHONDONTWRITEBYTECODE = '1'
    & $python -m pytest -q -p no:cacheprovider
    if ($LASTEXITCODE -ne 0) { throw 'Automated checks failed' }
    Write-Host 'All automated checks passed.'
    exit 0
}

Write-Host 'RentMate integrated local prototype'
Write-Host 'Open: http://127.0.0.1:8020/demo'
Write-Host 'API docs: http://127.0.0.1:8020/docs'
Write-Host 'Demo tokens are pre-filled on the demo page. Ctrl+C stops the server.'
& $python -m uvicorn main:app --host 127.0.0.1 --port 8020

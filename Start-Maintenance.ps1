param([switch]$Verify)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$backend = Join-Path $root 'backend'
$runtime = 'D:\ELEC5620\rentmate-maintenance'
New-Item -ItemType Directory -Force -Path $runtime, "$runtime\temp", "$runtime\cache" | Out-Null
$env:TEMP = "$runtime\temp"
$env:TMP = $env:TEMP
$env:UV_CACHE_DIR = "$runtime\cache"
$env:UV_PYTHON_INSTALL_DIR = 'D:\ELEC5620\python'
$python = "$runtime\.venv\Scripts\python.exe"
if (!(Test-Path -LiteralPath $python)) {
    $uv = 'D:\ELEC5620\tools\uv\uv.exe'
    if (!(Test-Path -LiteralPath $uv)) { throw 'Install Python 3.11+, then follow backend/maintenance/README.md.' }
    & $uv venv --python 3.12 "$runtime\.venv"
    if ($LASTEXITCODE -ne 0) { throw 'Python environment creation failed' }
}
& 'D:\ELEC5620\tools\uv\uv.exe' pip install --python $python -r "$backend\maintenance\requirements-tested.txt"
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
$env:DATABASE_PATH = "$runtime\maintenance.db"
# Optional: reuse the locally encrypted school key, never copy it into Git.
$secretPath = 'D:\ELEC5620\secrets\api-key.dpapi'
if (!(Test-Path -LiteralPath (Join-Path $backend '.env')) -and (Test-Path -LiteralPath $secretPath)) {
    Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1')
    $secret = Get-Content -LiteralPath $secretPath | ConvertTo-SecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
    try { $env:LLM_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
    $env:LLM_BASE_URL = 'http://10.167.60.148:8000/v1'
    $env:LLM_MODEL = 'campus-qwen'
}
if ($Verify) {
    Set-Location $backend
    $env:PYTHONDONTWRITEBYTECODE = '1'
    & $python -m pytest -q -p no:cacheprovider
    if ($LASTEXITCODE -ne 0) { throw 'Automated checks failed' }
    & $python -m maintenance.verify_live --output "$runtime\verification\live-result.json"
    if ($LASTEXITCODE -ne 0) { throw 'Live acceptance failed; check VPN, key, proxy and result JSON' }
    Write-Host "Verified. Results: $runtime\verification\live-result.json"
    exit 0
}
$tenantToken = [guid]::NewGuid().ToString('N')
$managerToken = [guid]::NewGuid().ToString('N')
$env:RENTMATE_AUTH_TOKENS = @{
    $tenantToken = @{tenant_id='demo-tenant'; property_id='demo-property'}
    $managerToken = @{tenant_id='demo-tenant'; property_id='demo-property'; manager_id='demo-manager'}
} | ConvertTo-Json -Compress
Write-Host 'RentMate Maintenance - demo accounts, local prototype only'
Write-Host "Tenant token:  $tenantToken"
Write-Host "Manager token: $managerToken"
Write-Host 'Open http://127.0.0.1:8020/api/maintenance/demo'
Write-Host 'API docs: http://127.0.0.1:8020/docs ; Ctrl+C stops server.'
Set-Location $backend
& $python -m uvicorn main:app --host 127.0.0.1 --port 8020

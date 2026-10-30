# Windows Task Scheduler acceptance evidence

Status: **UNVERIFIED DEPLOYMENT DEPENDENCY**. The implementation was inspected
and its Python runner executed on macOS. There is no Windows environment in
this session (`uname -s` = `Darwin`; `pwsh`, `powershell.exe` and `schtasks.exe`
were not found). This is not evidence of a Windows defect or a successful
scheduled Windows execution.

## What has been inspected

- `Register-Monthly-Reports.ps1` registers `schtasks.exe /SC MONTHLY /D 1`
  with default `/ST 02:00` and a quoted PowerShell runner/database/log path.
- The registration script now verifies Windows timezone `AUS Eastern Standard
  Time`, which observes Sydney daylight saving. It refuses to silently register
  against a different machine timezone. It does not change the computer timezone.
- `Run-Monthly-Reports.ps1` invokes the project virtual environment's
  `python -m reporting.monthly_runner`, passing the database and JSONL log path.
  Non-zero Python status throws a PowerShell error and fails the task.
- These files are PowerShell scripts; there is no separate monthly `.cmd`
  batch wrapper in this repository.
- Month selection in Python uses IANA `Australia/Sydney`, independent of the
  process/OS timezone. Timestamps in the log remain UTC.
- Windows needs the `tzdata` package for `zoneinfo`. It is now a Windows-only
  dependency; the launcher checks that Sydney timezone data can be loaded and
  installs requirements if it cannot.

## Prepare the actual Windows computer

1. Check the current project version and run the updated `Start-RentMate.cmd`
   once to prepare `.runtime/.venv` and the demonstration database.
2. Use a disposable demo database first. Registration and the acceptance run
   must target the same database that you inspect afterwards.
3. Ask the computer administrator to verify the timezone:

```powershell
Get-TimeZone
# Expected Id: AUS Eastern Standard Time
```

4. Configure any course model settings in the task account's environment or
   `backend/.env`. Browser AI settings are held in the web server's process;
   they do not transfer to a separate Task Scheduler process. Do not include
   API keys or `.env` contents in acceptance evidence.
5. The default school/demo task uses the registering user's context. Verify the
   machine is awake, the required account is available, and VPN/model access
   is available if live narrative generation is part of acceptance.

## Capture a real task execution

Run these commands in PowerShell from the repository root. They are a manual
acceptance procedure, **not commands executed in this macOS audit**.

```powershell
$taskName = 'RentMate Monthly Reports Acceptance'
$database = Join-Path $PWD '.runtime\rentmate-demo.db'
$log = Join-Path $PWD '.runtime\monthly-report-acceptance.jsonl'
$evidence = Join-Path $PWD '.runtime\windows-monthly-evidence'
New-Item -ItemType Directory -Force -Path $evidence | Out-Null

Get-TimeZone | ConvertTo-Json | Set-Content (Join-Path $evidence 'timezone.json')
.\Register-Monthly-Reports.ps1 -TaskName $taskName -DatabasePath $database -LogPath $log
Export-ScheduledTask -TaskName $taskName | Set-Content (Join-Path $evidence 'task.xml')
Get-ScheduledTask -TaskName $taskName | Select-Object TaskName,State,Actions,Triggers,Principal |
    ConvertTo-Json -Depth 8 | Set-Content (Join-Path $evidence 'task-configuration.json')

$before = if (Test-Path -LiteralPath $log) { @(Get-Content -LiteralPath $log).Count } else { 0 }
$requestedAt = Get-Date
Start-ScheduledTask -TaskName $taskName
$deadline = (Get-Date).AddMinutes(5)
do {
    Start-Sleep -Seconds 1
    $task = Get-ScheduledTask -TaskName $taskName
    $info = Get-ScheduledTaskInfo -TaskName $taskName
    $ran = $info.LastRunTime -ge $requestedAt.AddSeconds(-2)
} while ((!$ran -or $task.State -eq 'Running') -and (Get-Date) -lt $deadline)

if (!$ran -or $task.State -eq 'Running') { throw 'No completed task execution observed before timeout.' }
[pscustomobject]@{
    RequestedAtUtc = $requestedAt.ToUniversalTime().ToString('o')
    LastRunTimeUtc = $info.LastRunTime.ToUniversalTime().ToString('o')
    LastTaskResult = $info.LastTaskResult
    State = [string]$task.State
} | ConvertTo-Json | Set-Content (Join-Path $evidence 'execution.json')

if (!(Test-Path -LiteralPath $log)) { throw 'Application log missing.' }
$newLines = @(Get-Content -LiteralPath $log | Select-Object -Skip $before)
if ($newLines.Count -eq 0) { throw 'Task ran but no fresh application log entry was produced.' }
$newLines | Set-Content (Join-Path $evidence 'application-runs.jsonl')
$run = $newLines[-1] | ConvertFrom-Json
$run | Select-Object started_at,finished_at,period,business_timezone,succeeded,failed,results |
    ConvertTo-Json -Depth 8 | Set-Content (Join-Path $evidence 'report-results.json')

if ($info.LastTaskResult -ne 0 -or $run.failed -ne 0) { throw 'Task/application execution failed; preserve evidence and investigate.' }
$reportId = ($run.results | Where-Object status -eq 'succeeded' | Select-Object -First 1).report_id
if (!$reportId) { throw 'No report ID recorded.' }
& .\.runtime\.venv\Scripts\python.exe -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); r=c.execute('SELECT report_id,property_id,period_year,period_month,generation,created_at FROM report WHERE report_id=?',(sys.argv[2],)).fetchone(); print(r); sys.exit(0 if r else 1)" $database $reportId |
    Set-Content (Join-Path $evidence 'persisted-report.txt')
if ($LASTEXITCODE -ne 0) { throw 'Logged report ID was not found in the application database.' }
```

Record whether `reused` is true: a previously generated scheduled slot should
reuse its report ID. A new execution log plus an existing report ID is valid
idempotent behavior, but is not proof of a new generation.

## Required acceptance bundle

| Evidence | Required observation | Current status |
|---|---|---|
| Task XML / configuration | correct runner, account, database, monthly day 1 at 02:00 Sydney | UNVERIFIED |
| Execution record | actual task start time and terminal state | UNVERIFIED |
| LastTaskResult | 0 on successful application run | UNVERIFIED |
| Fresh application JSONL entry | UTC timestamps, Australia/Sydney, intended prior month, counts | UNVERIFIED on Windows |
| Report ID + database/API readback | matching persisted report, correct property/period | UNVERIFIED on Windows |

The separate macOS Python-run evidence is in
`output/verification/monthly-run-macos.jsonl`. It verifies the runner and report
persistence, not Windows registration, account behavior or Task Scheduler.

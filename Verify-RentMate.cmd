@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Verify-RentMate.ps1"
if errorlevel 1 (
  echo.
  echo RentMate verification FAILED.
) else (
  echo.
  echo RentMate verification PASSED.
)
pause

@echo off
:: Intraday lightweight runner — Phase 1 (CB check) + Phase 3.5 (trail re-eval)
:: Cadence: 12:00 ET and 15:30 ET on weekdays via Windows Task Scheduler
:: (separate from run_daily.bat which fires once pre-market at 09:40 ET).
::
:: This script intentionally does NOT regenerate signals, refresh data, fetch
:: news, retrain models, rescore universe, or evaluate hold-timeouts — those
:: stay on the daily/weekly cadence.  See scripts/intraday_check.py docstring.
::
:: --dry-run vs --no-dry-run:
::   --dry-run (default in the Python script) is safe — runs the CB check
::     and trail evaluation but skips the CB-flatten and trail-conversion
::     paths.  Use this when first wiring up the scheduler to validate
::     timing and Gateway availability before granting authority to mutate.
::   --no-dry-run enables the CB-flatten path AND lets the manager attempt
::     intraday trail conversions when config.risk.intraday_trail_conversion_enabled=True.
::     Use this only after a few days of clean --dry-run runs.
::
:: The script exits with code 0 EVEN on Gateway-down or unhandled errors —
:: this is by design to avoid Task Scheduler retry-storming an already-flaky
:: gateway.  The missed run is visible on Page 8 via intraday_run_log rows
:: with status='gateway_down' or status='error'.

:: Batch files live in batch_files/; cd to the PROJECT ROOT (parent) so every
:: relative path below (.venv, scripts, logs, db/trading.db) resolves correctly.
:: The DB path in config is cwd-relative — running from batch_files/ would create
:: a stray empty batch_files/db/trading.db instead of using the real one.
cd /d "%~dp0.."

:: Force UTF-8 output from all Python scripts (avoids cp1252 encoding errors)
set PYTHONUTF8=1

:: Ensure log directory exists
if not exist "logs\intraday" mkdir "logs\intraday"

:: Locale-safe timestamp via PowerShell (YYYYMMDD_HHMM — multiple runs per day,
:: so include time in the filename).
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmm"') do set ts=%%I
set LOG=logs\intraday\intraday_run_%ts%.log

echo [%date% %time%] === Intraday check starting === >> "%LOG%"

.venv\Scripts\python.exe scripts\intraday_check.py --no-dry-run >> "%LOG%" 2>&1

echo [%date% %time%] === Intraday check complete === >> "%LOG%"

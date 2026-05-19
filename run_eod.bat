@echo off
:: End-of-day bar refresh: Mon-Fri ~16:30 ET (after market close)
:: Step 1 — re-fetch last 5 days' OHLCV bars and recompute indicators for the
::          union of (current universe, recently-acted symbols, held positions).
::          Replaces mid-day partial bars (written by morning signal_runner)
::          with the final post-close values from yfinance.
::
:: Schedule via Windows Task Scheduler at 16:30 ET on weekdays.

cd /d "%~dp0"

:: Force UTF-8 output from Python scripts (avoids cp1252 encoding errors)
set PYTHONUTF8=1

:: Ensure log directory exists
if not exist "logs\eod" mkdir "logs\eod"

:: Locale-safe datestamp via PowerShell (YYYYMMDD)
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set dt=%%I
set LOG=logs\eod\eod_run_%dt%.log

echo [%date% %time%] === EOD bar refresh starting === >> "%LOG%"

echo [%date% %time%] Step 1: scripts\refresh_recent_bars.py >> "%LOG%"
.venv\Scripts\python.exe scripts\refresh_recent_bars.py >> "%LOG%" 2>&1
if errorlevel 1 (
    echo [%date% %time%] ERROR: refresh_recent_bars.py failed >> "%LOG%"
    exit /b 1
)

echo [%date% %time%] === EOD bar refresh complete === >> "%LOG%"

@echo off
:: Daily run: Mon-Fri at 10:00am via Windows Task Scheduler
:: Step 1 — refresh OHLCV, indicators, news, and FinBERT scores
:: Step 2 — re-score universe (Stage 3) so new members are active before training
:: Step 3 — train models for any symbols missing checkpoints (skips existing)
:: Step 4 — signal runner dry-run

cd /d "%~dp0"

:: Force UTF-8 output from all Python scripts (avoids cp1252 encoding errors)
set PYTHONUTF8=1

:: Ensure log directory exists
if not exist "logs\daily" mkdir "logs\daily"

:: Locale-safe datestamp via PowerShell (YYYYMMDD)
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set dt=%%I
set LOG=logs\daily\daily_run_%dt%.log

echo [%date% %time%] === Daily run starting === >> "%LOG%"

echo [%date% %time%] Step 1: scripts\run_pipeline.py >> "%LOG%"
.venv\Scripts\python.exe scripts\run_pipeline.py >> "%LOG%" 2>&1
if errorlevel 1 (
    echo [%date% %time%] ERROR: run_pipeline.py failed -- aborting >> "%LOG%"
    exit /b 1
)

echo [%date% %time%] Step 2: scripts\universe_scheduler.py --rescore-now --no-signal-run >> "%LOG%"
.venv\Scripts\python.exe scripts\universe_scheduler.py --rescore-now --no-signal-run >> "%LOG%" 2>&1
if errorlevel 1 (
    echo [%date% %time%] ERROR: universe_scheduler.py --rescore-now failed >> "%LOG%"
    exit /b 1
)

echo [%date% %time%] Step 3: scripts\train_models.py >> "%LOG%"
.venv\Scripts\python.exe scripts\train_models.py >> "%LOG%" 2>&1
if errorlevel 1 (
    echo [%date% %time%] ERROR: train_models.py failed -- aborting >> "%LOG%"
    exit /b 1
)

echo [%date% %time%] Step 4: scripts\signal_runner.py --no-dry-run >> "%LOG%"
.venv\Scripts\python.exe scripts\signal_runner.py --no-dry-run >> "%LOG%" 2>&1
if errorlevel 1 (
    echo [%date% %time%] ERROR: signal_runner.py failed >> "%LOG%"
    exit /b 1
)

echo [%date% %time%] === Daily run complete === >> "%LOG%"

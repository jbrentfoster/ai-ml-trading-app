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

:: Daily training of newly-promoted universe symbols writes fresh trade_log
:: rows; backfill SPY-relative returns for them before the signal runner reads
:: anything from trade_log via Page 10 / realised-Kelly.  Idempotent — only
:: touches WHERE benchmark_return_pct IS NULL.
echo [%date% %time%] Step 3b: scripts\backfill_benchmark_returns.py >> "%LOG%"
.venv\Scripts\python.exe scripts\backfill_benchmark_returns.py >> "%LOG%" 2>&1
if errorlevel 1 (
    echo [%date% %time%] WARNING: backfill_benchmark_returns.py failed -- Page 10 alpha view will have NULL rows >> "%LOG%"
)

:: Verify the backfill — a noisy log line beats a silent data gap.
echo [%date% %time%] Step 3b verify: NULL benchmark_return_pct count >> "%LOG%"
.venv\Scripts\python.exe -c "from data.database import get_engine; from sqlalchemy import text; e=get_engine(); n=e.connect().execute(text('SELECT COUNT(*) FROM trade_log WHERE benchmark_return_pct IS NULL')).scalar(); print(f'NULL benchmark_return_pct after backfill: {n}')" >> "%LOG%" 2>&1

echo [%date% %time%] Step 4: scripts\signal_runner.py --no-dry-run >> "%LOG%"
.venv\Scripts\python.exe scripts\signal_runner.py --no-dry-run >> "%LOG%" 2>&1
if errorlevel 1 (
    echo [%date% %time%] ERROR: signal_runner.py failed >> "%LOG%"
    exit /b 1
)

echo [%date% %time%] === Daily run complete === >> "%LOG%"

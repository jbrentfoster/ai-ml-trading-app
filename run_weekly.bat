@echo off
:: Weekly run: Sunday
:: Step 1 — refresh OHLCV, indicators, news, and FinBERT scores
:: Step 2 — force-retrain all models on latest data
:: Step 3 — full universe refresh (all 3 stages: Alpaca S1 + liquidity S2 + XGBoost S3)

cd /d "%~dp0"

:: Force UTF-8 output from all Python scripts (avoids cp1252 encoding errors)
set PYTHONUTF8=1

:: Ensure log directory exists
if not exist "logs\weekly" mkdir "logs\weekly"

:: Locale-safe datestamp via PowerShell (YYYYMMDD)
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set dt=%%I
set LOG=logs\weekly\weekly_run_%dt%.log

echo [%date% %time%] === Weekly run starting === >> "%LOG%"

echo [%date% %time%] Step 1: scripts\run_pipeline.py >> "%LOG%"
.venv\Scripts\python.exe scripts\run_pipeline.py >> "%LOG%" 2>&1
if errorlevel 1 (
    echo [%date% %time%] ERROR: run_pipeline.py failed -- aborting >> "%LOG%"
    exit /b 1
)

echo [%date% %time%] Step 2: scripts\train_models.py --force >> "%LOG%"
.venv\Scripts\python.exe scripts\train_models.py --force >> "%LOG%" 2>&1
if errorlevel 1 (
    echo [%date% %time%] ERROR: train_models.py --force failed -- aborting >> "%LOG%"
    exit /b 1
)

echo [%date% %time%] Step 3: scripts\backfill_benchmark_returns.py (after retrain — populates new trade_log rows) >> "%LOG%"
.venv\Scripts\python.exe scripts\backfill_benchmark_returns.py >> "%LOG%" 2>&1
if errorlevel 1 (
    echo [%date% %time%] WARNING: backfill_benchmark_returns.py failed -- Page 10 alpha view will have NULL rows >> "%LOG%"
)

:: Verify the backfill — a noisy log line beats a silent data gap.
echo [%date% %time%] Step 3 verify: NULL benchmark_return_pct count >> "%LOG%"
.venv\Scripts\python.exe -c "from data.database import get_engine; from sqlalchemy import text; e=get_engine(); n=e.connect().execute(text('SELECT COUNT(*) FROM trade_log WHERE benchmark_return_pct IS NULL')).scalar(); print(f'NULL benchmark_return_pct after backfill: {n}')" >> "%LOG%" 2>&1

echo [%date% %time%] Step 4: scripts\universe_scheduler.py --run-now >> "%LOG%"
.venv\Scripts\python.exe scripts\universe_scheduler.py --run-now >> "%LOG%" 2>&1
if errorlevel 1 (
    echo [%date% %time%] ERROR: universe_scheduler.py --run-now failed >> "%LOG%"
    exit /b 1
)

echo [%date% %time%] === Weekly run complete === >> "%LOG%"

@echo off
:: Daily run: Mon-Fri at 10:00am via Windows Task Scheduler
:: Step 1 — refresh OHLCV, indicators, news, and FinBERT scores
:: Step 2 — re-score universe (Stage 3) so new members are active before training
:: Step 3 — train models for any symbols missing checkpoints (skips existing)
:: Step 3b — backfill SPY-relative returns for fresh walk-forward trade_log rows
:: Step 4 — signal runner (--no-dry-run); Phase 1 reconciles off-cycle live fills
:: Step 4b — backfill again so today's live-reconciled rows aren't NULL until tomorrow

:: Batch files live in batch_files/; cd to the PROJECT ROOT (parent) so every
:: relative path below (.venv, scripts, logs, db/trading.db) resolves correctly.
cd /d "%~dp0.."

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

:: signal_runner Phase 1 reconciles off-cycle live IBKR fills into trade_log
:: (source='live' rows) AFTER Step 3b ran -- so any round trip closed since the
:: last run lands here with a NULL benchmark_return_pct.  Re-run the backfill so
:: Page 10's benchmark-relative view picks up today's live exits same-day instead
:: of lagging a full day until tomorrow's Step 3b.  Idempotent (WHERE ... IS NULL).
echo [%date% %time%] Step 4b: scripts\backfill_benchmark_returns.py (live rows from Phase 1) >> "%LOG%"
.venv\Scripts\python.exe scripts\backfill_benchmark_returns.py >> "%LOG%" 2>&1
if errorlevel 1 (
    echo [%date% %time%] WARNING: post-runner backfill_benchmark_returns.py failed -- live rows reconciled today will have NULL benchmark_return_pct until tomorrow >> "%LOG%"
)

:: Verify — a noisy log line beats a silent data gap.
echo [%date% %time%] Step 4b verify: NULL benchmark_return_pct count >> "%LOG%"
.venv\Scripts\python.exe -c "from data.database import get_engine; from sqlalchemy import text; e=get_engine(); n=e.connect().execute(text('SELECT COUNT(*) FROM trade_log WHERE benchmark_return_pct IS NULL')).scalar(); print(f'NULL benchmark_return_pct after post-runner backfill: {n}')" >> "%LOG%" 2>&1

echo [%date% %time%] === Daily run complete === >> "%LOG%"

@echo off
:: Daily backup to external drive (db/trading.db, logs/, models/cache/, config/settings.yaml).
::
:: SQLite snapshot uses Python's sqlite3.Connection.backup() — atomic, safe against
:: concurrent writers (a raw file copy mid-write can corrupt the destination DB).
::
:: Schedule via Windows Task Scheduler at ~17:00 ET on weekdays, after run_eod.bat.

:: Batch files live in batch_files/; cd to the PROJECT ROOT (parent) so every
:: relative path below (.venv, db/trading.db, logs, models/cache, config) resolves correctly.
cd /d "%~dp0.."

set PYTHONUTF8=1

:: ---- Config (edit these to point at the actual external drive) ----
set "BACKUP_ROOT=D:\trading_app_backup"
set "RETENTION_DAYS=30"

:: Locale-safe datestamp via PowerShell (YYYY-MM-DD)
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set dt=%%I

:: ---- Log to the PROJECT logs/ dir (not the backup drive) so a skipped run is
::      still recorded locally when D: is unmounted. ----
if not exist "logs\backup" mkdir "logs\backup"
set "LOG=logs\backup\backup_%dt%.log"

echo. >> "%LOG%"
echo ============================================ >> "%LOG%"
echo [%date% %time%] === Backup starting === >> "%LOG%"
echo ============================================ >> "%LOG%"

:: ---- Bail out early if external drive isn't mounted ----
if not exist "D:\" (
    echo [%date% %time%] ERROR: external drive D: not mounted -- skipping backup >> "%LOG%"
    exit /b 1
)

if not exist "%BACKUP_ROOT%" mkdir "%BACKUP_ROOT%"

:: ---- 1. Atomic SQLite snapshot via Python's built-in sqlite3 ----
if not exist "%BACKUP_ROOT%\db_snapshots" mkdir "%BACKUP_ROOT%\db_snapshots"
set "DB_DEST=%BACKUP_ROOT%\db_snapshots\trading_%dt%.db"

echo [%date% %time%] Step 1: SQLite snapshot -> %DB_DEST% >> "%LOG%"
.venv\Scripts\python.exe -c "import sqlite3; src=sqlite3.connect(r'db\trading.db'); dst=sqlite3.connect(r'%DB_DEST%'); src.backup(dst); dst.close(); src.close(); print('OK')" >> "%LOG%" 2>&1
if errorlevel 1 (
    echo [%date% %time%] ERROR: SQLite backup failed >> "%LOG%"
    exit /b 1
)

:: ---- 2. Mirror logs/ ----
echo [%date% %time%] Step 2: mirroring logs/ >> "%LOG%"
robocopy "logs" "%BACKUP_ROOT%\logs\project" /MIR /R:1 /W:1 /NFL /NDL /NP >> "%LOG%" 2>&1
:: robocopy exit codes 0-7 are success; 8+ are real failures
if errorlevel 8 (
    echo [%date% %time%] WARNING: logs robocopy reported errors >> "%LOG%"
)

:: ---- 3. Mirror models/cache/ (trained checkpoints — slow to regenerate) ----
echo [%date% %time%] Step 3: mirroring models/cache/ >> "%LOG%"
robocopy "models\cache" "%BACKUP_ROOT%\models_cache" /MIR /R:1 /W:1 /NFL /NDL /NP >> "%LOG%" 2>&1
if errorlevel 8 (
    echo [%date% %time%] WARNING: models robocopy reported errors >> "%LOG%"
)

:: ---- 4. Copy config/settings.yaml (user overrides; secrets are env-var only) ----
if exist "config\settings.yaml" (
    if not exist "%BACKUP_ROOT%\config" mkdir "%BACKUP_ROOT%\config"
    echo [%date% %time%] Step 4: copying config/settings.yaml >> "%LOG%"
    copy /Y "config\settings.yaml" "%BACKUP_ROOT%\config\settings.yaml" >> "%LOG%" 2>&1
)

:: ---- 5. Prune DB snapshots older than RETENTION_DAYS ----
echo [%date% %time%] Step 5: pruning DB snapshots older than %RETENTION_DAYS% days >> "%LOG%"
forfiles /p "%BACKUP_ROOT%\db_snapshots" /m "trading_*.db" /d -%RETENTION_DAYS% /c "cmd /c del @path" >> "%LOG%" 2>nul

:: ---- 6. Prune local backup logs older than RETENTION_DAYS ----
forfiles /p "logs\backup" /m "backup_*.log" /d -%RETENTION_DAYS% /c "cmd /c del @path" >> "%LOG%" 2>nul

echo [%date% %time%] === Backup complete === >> "%LOG%"
exit /b 0

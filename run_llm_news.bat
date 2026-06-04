@echo off
:: LLM news analyst (shadow workflow — NOT consumed by signal_runner).
:: Step 1 — ingest full article bodies for stage-3 universe news (needs IB
::          Gateway up; fast, ~seconds).
:: Step 2 — score un-scored bodies with the local 8B model via Ollama (no
::          Gateway needed; slow, ~80s/article on this CPU).
::
:: Both steps no-op unless config.llm.enabled = True (Page 5 Settings).
:: Schedule via Windows Task Scheduler AFTER run_daily.bat (Gateway still up,
:: signal_runner already finished) — or split Step 2 to an overnight slot if the
:: machine stays awake.  Off the pre-market critical path on purpose: Step 2 can
:: take ~2h and must never delay signal_runner.

cd /d "%~dp0"

set PYTHONUTF8=1

if not exist "logs\llm" mkdir "logs\llm"

for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set dt=%%I
set LOG=logs\llm\llm_news_%dt%.log

echo [%date% %time%] === LLM news analyst starting === >> "%LOG%"

echo [%date% %time%] Step 1: scripts\ingest_news_bodies.py >> "%LOG%"
.venv\Scripts\python.exe scripts\ingest_news_bodies.py --days 1 >> "%LOG%" 2>&1

echo [%date% %time%] Step 2: scripts\score_news_llm.py >> "%LOG%"
.venv\Scripts\python.exe scripts\score_news_llm.py --days 1 >> "%LOG%" 2>&1

echo [%date% %time%] === LLM news analyst complete === >> "%LOG%"

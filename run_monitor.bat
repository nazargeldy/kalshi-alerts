@echo off
cd /d "c:\Users\geldy\kalshi_alerts"

if not exist logs mkdir logs

set PYTHONIOENCODING=utf-8

call .venv\Scripts\activate.bat

echo [%DATE% %TIME%] Starting Monitor... >> logs\monitor.out
python monitor.py >> logs\monitor.out 2>> logs\monitor.err

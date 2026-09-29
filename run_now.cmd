@echo off
rem Runs the job search immediately (same as the 09:00 scheduled task).
cd /d "%~dp0"
python job_searcher.py
pause

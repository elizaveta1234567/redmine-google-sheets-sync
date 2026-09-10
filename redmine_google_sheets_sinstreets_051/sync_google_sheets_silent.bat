@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
python -X utf8 redmine_export_google_sheets.py >> sync.log 2>&1

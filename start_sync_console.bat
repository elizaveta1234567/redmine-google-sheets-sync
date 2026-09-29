@echo off
chcp 65001 > nul
set PYTHONUTF8=1
cd /d "%~dp0"

python -X utf8 sync_unified.py

echo.
echo ==================================
echo Синхронизация завершена.
pause

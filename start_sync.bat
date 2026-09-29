@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
start "" pythonw -X utf8 gui_app.py


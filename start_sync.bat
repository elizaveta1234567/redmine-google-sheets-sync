@echo off
REM Включаем UTF-8 кодировку для правильного вывода русских букв
chcp 65001 > nul
set PYTHONUTF8=1
cd /d "%~dp0"

REM Запуск unified Redmine -> Google Sheets sync скрипта
REM После выбора проекта, версии и таблицы скрипт обновляет данные
REM каждые 5 минут. Для остановки нажми Ctrl+C.

cls
echo.
echo Redmine ^<-^> Google Sheets Sync
echo ==================================
echo.
echo Запуск синхронизации...
echo.

python -X utf8 sync_unified.py

echo.
echo ==================================
echo Синхронизация завершена.
pause


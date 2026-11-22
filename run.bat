@echo off
rem 더블클릭하면 책장 어드민이 열린다. 끄려면 이 창에서 Ctrl+C
cd /d "%~dp0"
python admin\server.py
pause

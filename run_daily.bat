@echo off
REM Wrapper para Programador de tareas de Windows.
cd /d "%~dp0"
python run_daily.py %*

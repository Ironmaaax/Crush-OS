@echo off
REM Double-cliquable : configure Crush (assistant web). Contourne l'Execution Policy.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0crush.ps1" setup
echo.
pause

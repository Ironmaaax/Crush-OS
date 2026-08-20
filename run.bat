@echo off
REM Double-cliquable : lance Crush (API + LiveKit + vocal). Contourne l'Execution Policy.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0crush.ps1" run
echo.
pause

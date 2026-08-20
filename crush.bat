@echo off
REM ============================================================
REM  Lanceur Crush pour Windows.
REM  Contourne la PowerShell Execution Policy : les .ps1 telecharges
REM  (mark of the web) sont bloques par defaut, ce qui fait echouer
REM  ".\crush.ps1 ..." avec "l'execution de scripts est desactivee".
REM  Un .bat n'est PAS soumis a cette politique : il appelle crush.ps1
REM  en -ExecutionPolicy Bypass et transmet les arguments.
REM
REM  Usage :  crush.bat setup   |   crush.bat run   |   crush.bat api
REM ============================================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0crush.ps1" %*

@echo off
rem Portable launcher.
rem   python\   the interpreter AND Textual, nothing installed on the machine
rem   src\      the client code (replace this folder to update)
rem   data\     config.json (your account, verifier ids and switches)
rem   engine\   wfsim-core.exe (+ prev) and its runtime DLL
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "PYTHONNOUSERSITE=1"
"%~dp0python\python.exe" -B "%~dp0src\app.py"
echo.
echo ---- exited with code %ERRORLEVEL% ----
pause

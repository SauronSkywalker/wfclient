@echo off
rem Build the native (best-for-this-CPU) engine and adopt it as the overlay.
rem Needs the self-build dependency pack unpacked into the portable folder, plus
rem Visual Studio 2026+ (2022 or newer is fine) with the C++ workload.
rem The generic release engine is NEVER overwritten -- it stays as the fallback,
rem and is what runs whenever the native core no longer matches the server.
setlocal
cd /d "%~dp0"
if not exist "python\python.exe" (
  echo [ERROR] put this file in the portable package root, next to run-portable.bat.
  pause
  exit /b 1
)
if not exist "build-deps.json" (
  echo [ERROR] self-build dependency pack missing: unpack it into this folder first.
  echo         it ships as wfclient-deps-*.zip; see build-deps\README-*.md
  pause
  exit /b 1
)
echo [1/2] building the MSVC + PGO engine (a few minutes) and checking it...
echo       computing can continue in the client while this runs.
python\python.exe src\sync_engine.py --build-native
if errorlevel 1 (
  echo.
  echo [ERROR] build or check failed. Is Visual Studio 2026 installed with the C++
  echo         workload?  The log above says exactly why.
  pause
  exit /b 1
)
echo.
echo [2/2] done -- engine\wfsim-core.native.exe is now the core this machine runs.
python\python.exe src\sync_engine.py --native-status
pause

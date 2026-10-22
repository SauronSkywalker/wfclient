@echo off
setlocal EnableDelayedExpansion
rem PORTABLE-DISPATCH
rem
rem THE FIXED ENTRY POINT.  Its content never changes: it reads line 4 of
rem current.txt (the current generation's run-portable-<stamp>.bat) and calls it.
rem
rem Without a pointer there are two kinds of folder:
rem   * an old layout -- run-portable.bat is a real launcher: call that one;
rem   * a new layout that has only been unpacked (nothing installed yet): call
rem     the lexicographically last run-portable-<stamp>.bat, i.e. the newest.
rem NEVER fall back to run-portable.bat unconditionally: this same file is
rem shipped under that name too, so that call would be self-recursion until
rem cmd runs out of stack.  Ask it first whether it carries this marker.
rem
rem ASCII ONLY.  cmd reads a .bat in the OEM codepage (936 here), so a utf8
rem line of chinese text is mis-split and its tail runs as a command -- seen
rem in the empty-folder case, where the message is the whole screen.
cd /d "%~dp0"
set "BAT="
if exist "%~dp0current.txt" (
  set "N=0"
  for /f "usebackq delims=" %%L in ("%~dp0current.txt") do (
    set /a N+=1
    set "L!N!=%%L"
  )
  set "BAT=!L4!"
)
if not "!BAT!"=="" if exist "%~dp0!BAT!" (
  call "%~dp0!BAT!"
  exit /b !ERRORLEVEL!
)
if exist "%~dp0run-portable.bat" (
  findstr /c:"PORTABLE-DISPATCH" "%~dp0run-portable.bat" >nul 2>&1
  if errorlevel 1 (
    call "%~dp0run-portable.bat"
    exit /b !ERRORLEVEL!
  )
)
set "NEWEST="
for /f "delims=" %%F in ('dir /b /o:n "%~dp0run-portable-*.bat" 2^>nul') do set "NEWEST=%%F"
if not "!NEWEST!"=="" if exist "%~dp0!NEWEST!" (
  call "%~dp0!NEWEST!"
  exit /b !ERRORLEVEL!
)
echo No usable launcher here: current.txt does not name an existing
echo run-portable-*.bat, and there is no old run-portable.bat either.
echo This folder is not a complete portable package.
pause
exit /b 1

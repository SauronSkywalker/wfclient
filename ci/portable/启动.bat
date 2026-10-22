@echo off
setlocal EnableDelayedExpansion
rem PORTABLE-DISPATCH
rem
rem 客户端的固定入口。内容永远不变：它只读同目录 current.txt 的第 4 行
rem （当前那一代的启动器 run-portable-<时间戳>.bat）并把它叫起来。
rem 没有指针就回退老启动器，于是"还没按新布局更新过"的目录也照常能开。
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
  call "%~dp0run-portable.bat"
  exit /b !ERRORLEVEL!
)
echo 找不到 current.txt 指向的启动器，也没有 run-portable.bat。
echo 这个目录不是完整的便携包。
pause
exit /b 1

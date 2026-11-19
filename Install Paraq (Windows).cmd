@echo off
rem Installs Paraq for the current Windows user. Double-click to run.
rem Options are passed on. From PowerShell:  & ".\Install Paraq (Windows).cmd" -Ai gemma-4b
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows\install.ps1" %*
echo.
pause

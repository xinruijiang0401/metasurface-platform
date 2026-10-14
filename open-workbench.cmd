@echo off
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-workbench.ps1"
set "WORKBENCH_EXIT_CODE=%ERRORLEVEL%"
if not "%WORKBENCH_EXIT_CODE%"=="0" pause
exit /b %WORKBENCH_EXIT_CODE%

@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
python -m nesting_studio
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" pause
endlocal & exit /b %EXIT_CODE%

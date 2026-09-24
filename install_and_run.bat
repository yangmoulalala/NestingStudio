@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
echo Installing NestingStudio dependencies...
python -m pip install -r requirements.txt
if errorlevel 1 goto :error
echo Starting NestingStudio...
python -m nesting_studio
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" pause
endlocal & exit /b %EXIT_CODE%
:error
pause
endlocal & exit /b 1

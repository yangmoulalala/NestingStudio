@echo off
setlocal
chcp 65001 >nul
set "SCRIPT=%~dp0step_thickness_classifier.py"
if "%~1"=="" (
    python "%SCRIPT%"
) else (
    python "%SCRIPT%" "%~1"
)
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" pause
endlocal & exit /b %EXIT_CODE%

@echo off
setlocal
chcp 65001 >nul
set "SCRIPT=%~dp0auto_nest.py"
if "%~1"=="" (
    echo Usage: drag DXF/SVG/STEP files or folders onto this file.
    echo Or run auto_nest.py with CLI parameters.
    python "%SCRIPT%" --help
) else (
    python "%SCRIPT%" %*
)
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" pause
endlocal & exit /b %EXIT_CODE%

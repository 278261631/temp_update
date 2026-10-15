@echo off
setlocal

cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found in PATH. Please install Python 3.8+ first.
    pause
    exit /b 1
)

echo Starting KATS Region Collect / Template Update GUI...
python main.py
set EXITCODE=%ERRORLEVEL%

if not "%EXITCODE%"=="0" (
    echo.
    echo [ERROR] Program exited with code %EXITCODE%.
    pause
)

endlocal
exit /b %EXITCODE%

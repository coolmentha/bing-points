@echo off
setlocal

cd /d "%~dp0"

if not exist ".venv\Scripts\activate.bat" (
    echo Missing virtual environment script: .venv\Scripts\activate.bat
    pause
    exit /b 1
)

call ".venv\Scripts\activate.bat"
python main.py %*
set "exit_code=%errorlevel%"

if not "%exit_code%"=="0" (
    echo.
    echo Program exited with code: %exit_code%
    pause
)

exit /b %exit_code%

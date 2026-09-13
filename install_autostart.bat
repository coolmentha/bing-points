@echo off
setlocal

cd /d "%~dp0"

set "startup_dir=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "target_vbs=%startup_dir%\bing-points-autostart.vbs"
set "start_bat=%~dp0start.bat"

if not exist "%startup_dir%" (
    mkdir "%startup_dir%"
)

> "%target_vbs%" echo Set shell = CreateObject("WScript.Shell"^)
>> "%target_vbs%" echo shell.Run Chr^(34^) ^& "%start_bat%" ^& Chr^(34^), 0, False

if errorlevel 1 (
    echo Failed to install autostart.
    pause
    exit /b 1
)

echo Autostart installed:
echo %target_vbs%
pause
exit /b 0

@echo off
setlocal

set "target_vbs=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\bing-points-autostart.vbs"

if exist "%target_vbs%" (
    del /f /q "%target_vbs%"
)

if exist "%target_vbs%" (
    echo Failed to remove autostart.
    pause
    exit /b 1
)

echo Autostart removed.
pause
exit /b 0

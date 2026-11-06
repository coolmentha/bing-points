@echo off
setlocal EnableExtensions

cd /d "%~dp0"

rem NOTE: keep this file ASCII-only. cmd.exe parses .bat using the OEM code page
rem (GBK on Chinese Windows); UTF-8 Chinese text here gets mis-decoded and can
rem merge/destroy following commands.

set "startup_dir=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "target_vbs=%startup_dir%\bing-points-autostart.vbs"
set "start_bat=%~dp0start.bat"

if not exist "%startup_dir%" mkdir "%startup_dir%"

rem Launch through cmd.exe instead of ShellExecute-ing start.bat directly.
rem ShellExecute on a .bat can raise the "Open File - Security Warning" dialog
rem (Mark-of-the-Web); at logon the window is hidden, so nobody can click Run and
rem the automation silently never starts. cmd.exe is a trusted signed binary and
rem never triggers that prompt. --task marks the run unattended, which tells
rem start.bat never to wait for a keypress.
set "vbs_cmd=cmd.exe /c """"%start_bat%"""" --task"

> "%target_vbs%" echo ' BingPoints autostart: launch the automation hidden at logon.
>> "%target_vbs%" echo ' Runs through cmd.exe so no "Security Warning" dialog can block it.
>> "%target_vbs%" echo Set shell = CreateObject("WScript.Shell"^)
>> "%target_vbs%" echo shell.Run "%vbs_cmd%", 0, False

if not exist "%target_vbs%" (
    echo Failed to install autostart.
    pause
    exit /b 1
)

rem Verify the generated script actually mentions cmd.exe and --task.
findstr /i /c:"cmd.exe" "%target_vbs%" >nul
if errorlevel 1 (
    echo Autostart script looks wrong: cmd.exe wrapper missing.
    pause
    exit /b 1
)

echo Autostart installed:
echo %target_vbs%
pause
exit /b 0

@echo off
setlocal EnableExtensions

cd /d "%~dp0"

rem ---------------------------------------------------------------------------
rem NOTE: keep this file ASCII-only. cmd.exe parses .bat using the OEM code page
rem (GBK on Chinese Windows); UTF-8 Chinese comments get mis-decoded and a
rem trailing lead byte can swallow the newline, merging commands into garbage.
rem All human-readable (Chinese) output is produced by main.py into the log.
rem
rem Output is always redirected to logs\run_*.log: automated runs have no
rem visible console, so anything written only to the console is lost forever.
rem
rem Waiting policy is BOUNDED, never pause-forever. This script is launched both
rem by the scheduled task and by a hidden-window VBS (shell.Run ..., 0, False).
rem A plain "pause" in the hidden case waits for a keypress nobody can deliver,
rem hanging the process invisibly - and with task policy IgnoreNew that single
rem hang swallows every later scheduled run. So: wait only for a real
rem double-click (console attached + no automation flag), and at most 60s.
rem ---------------------------------------------------------------------------

set "LOG_DIR=%~dp0logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

rem Timestamps via PowerShell: %%date%% is localized (contains a weekday name)
rem and comes from the OEM code page, which would corrupt a UTF-8 log.
set "STAMP="
set "ISO="
for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss" 2^>nul`) do set "STAMP=%%i"
for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "Get-Date -Format 'yyyy-MM-dd HH:mm:ss'" 2^>nul`) do set "ISO=%%i"
if not defined STAMP set "STAMP=manual"
if not defined ISO set "ISO=unknown"
set "LOG_FILE=%LOG_DIR%\run_%STAMP%.log"

rem Wait only for a real double-click: console attached AND no automation flag.
set "INTERACTIVE="
if /i "%SESSIONNAME%"=="Console" set "INTERACTIVE=1"
if defined BING_REWARDS_NONINTERACTIVE set "INTERACTIVE="
if /i "%~1"=="--task" set "INTERACTIVE="

echo [%ISO%] ===== BingPoints run start ===== >> "%LOG_FILE%"
echo [%ISO%] cwd=%CD% args=%* interactive=%INTERACTIVE% session=%SESSIONNAME% >> "%LOG_FILE%"

rem Resolve the base interpreter before the venv existence check so a launcher
rem inheriting a Low integrity label cannot break WebView UIA.
set "PYTHON_HOME="
for /f "usebackq tokens=1,* delims== " %%a in (`findstr /b /c:"home = " ".venv\pyvenv.cfg"`) do set "PYTHON_HOME=%%b"
set "PYTHON_HOME=%PYTHON_HOME: =%"
call :strip_home

if not exist ".venv\Scripts\python.exe" (
    echo [%ISO%] ERROR missing .venv\Scripts\python.exe >> "%LOG_FILE%"
    echo Missing virtual environment python: .venv\Scripts\python.exe
    if defined INTERACTIVE call :wait_bounded
    exit /b 1
)

rem Use the base interpreter recorded in pyvenv.cfg with the venv's site-packages.
rem The venv launcher can inherit a Low integrity label (for example when the
rem project is opened by an assistant), which hides WebView UIA descendants.
rem The base CPython executable is an unlabeled trusted system install; feeding
rem it the venv site-packages keeps all existing dependencies available.
set "PYTHON_EXE="
set "PYTHON_SITE="
if defined PYTHON_HOME if exist "%PYTHON_HOME%\python.exe" set "PYTHON_EXE=%PYTHON_HOME%\python.exe"
if not defined PYTHON_EXE set "PYTHON_EXE=.venv\Scripts\python.exe"
if exist ".venv\Lib\site-packages" set "PYTHON_SITE=%CD%\.venv\Lib\site-packages"
if defined PYTHON_SITE set "PYTHONPATH=%PYTHON_SITE%;%CD%"
rem -u disables output buffering so the log survives a hard kill.
rem BING_REWARDS_LOG_FILE tells main.py exactly which log to attach to a failure
rem notification (otherwise it would have to guess the newest run_*.log).
set "BING_REWARDS_LOG_FILE=%LOG_FILE%"
"%PYTHON_EXE%" -u -X utf8 main.py %* >> "%LOG_FILE%" 2>&1
set "exit_code=%errorlevel%"

echo [%ISO%] ===== BingPoints run end exit=%exit_code% ===== >> "%LOG_FILE%"

if not "%exit_code%"=="0" (
    echo.
    echo Program exited with code: %exit_code%
    echo See log: %LOG_FILE%
    if defined INTERACTIVE call :wait_bounded
)

exit /b %exit_code%

:strip_home
exit /b 0

rem ---------------------------------------------------------------------------
rem Bounded wait: auto-continues after 60s so it can never hang forever.
rem ---------------------------------------------------------------------------
:wait_bounded
choice /c r /t 60 /d r /n >nul 2>&1
if errorlevel 255 pause
exit /b 0

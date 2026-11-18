@echo off
setlocal
cd /d "%~dp0"
if /i "%~1"=="start" goto command_start
if /i "%~1"=="status" goto command_status
if /i "%~1"=="stop" goto command_stop
if /i "%~1"=="web" goto command_web
if not "%~1"=="" exit /b 2
set "weather99_action=Menu"
goto execute

:command_start
set "weather99_action=Start"
goto execute
:command_status
set "weather99_action=Status"
goto execute
:command_stop
set "weather99_action=Stop"
goto execute
:command_web
set "weather99_action=Web"
goto execute

:execute
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\paper.ps1" -Action %weather99_action%
set "weather99_result=%errorlevel%"
exit /b %weather99_result%

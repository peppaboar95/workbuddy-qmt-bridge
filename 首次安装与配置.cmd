@echo off
setlocal EnableExtensions
set "SETUP=%~dp0installer\setup.cmd"
if not exist "%SETUP%" set "SETUP=%~dp0setup.cmd"
if not exist "%SETUP%" (
  echo [ERROR] Internal setup.cmd is missing. Extract the complete Release ZIP and try again.
  pause
  exit /b 2
)
call "%SETUP%" %*
exit /b %ERRORLEVEL%

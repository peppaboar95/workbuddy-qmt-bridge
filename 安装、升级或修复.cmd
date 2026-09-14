@echo off
setlocal EnableExtensions
if not exist "%~dp0setup.cmd" (
  echo [ERROR] setup.cmd is missing. Extract the complete Release ZIP and try again.
  pause
  exit /b 2
)
call "%~dp0setup.cmd" %*
exit /b %ERRORLEVEL%

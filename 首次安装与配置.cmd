@echo off
setlocal
chcp 65001 >nul
call "%~dp0安装、升级或修复.cmd" %*
exit /b %ERRORLEVEL%

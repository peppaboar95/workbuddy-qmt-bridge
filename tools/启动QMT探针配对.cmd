@echo off
setlocal
cd /d "%~dp0.."
python "%~dp0qmt_probe_peer.py" %*
set "probe_exit=%errorlevel%"
if not "%probe_exit%"=="0" (
    echo Probe peer failed. Keep this output for diagnosis.
    pause
)
exit /b %probe_exit%

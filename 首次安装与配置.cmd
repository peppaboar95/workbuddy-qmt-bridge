@echo off
setlocal
title WorkBuddy QMT Bridge Setup
cd /d "%~dp0"

echo ============================================================
echo WorkBuddy-QMT Bridge - First Setup
echo ============================================================
echo Detailed Chinese guide: README-RELEASE.zh-CN.md
echo.
echo This script will:
echo   1. Check Python 3.10 or newer.
echo   2. Install the wheel stored beside this file, or this source tree.
echo   3. Start the guided account, QMT and WorkBuddy configuration.
echo.
echo Before the guided step, please prepare:
echo   - The QMT account ID for each account you want to enable.
echo   - Permission to update your WorkBuddy MCP configuration.
echo.
echo Safety defaults:
echo   - New runtimes and QMT adapters start in OBSERVE_ONLY.
echo   - Stock account is enabled by default; credit account is disabled.
echo   - This script never enables LIVE trading or starts QMT.
echo ============================================================
echo.

if /i "%~1"=="--syntax-check" (
  echo BATCH_SYNTAX_OK
  exit /b 0
)

echo [Step 1/4] Checking Python. No input is required...
set "PYTHON_CMD="
py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if not errorlevel 1 set "PYTHON_CMD=py -3"
if defined PYTHON_CMD goto python_ready
python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if not errorlevel 1 set "PYTHON_CMD=python"

:python_ready
if not defined PYTHON_CMD (
  echo.
  echo [ERROR] Python 3.10 or newer was not found.
  echo USER ACTION:
  echo   1. Install Python 3.10 or newer.
  echo   2. Enable the installer option "Add Python to PATH".
  echo   3. Close this window and run this file again.
  pause
  exit /b 2
)
echo Python check passed: %PYTHON_CMD%
echo.

echo [Step 2/4] Locating the installation source.
set "WHEEL="
set "WHEEL_COUNT=0"
for %%F in ("%~dp0workbuddy_qmt_bridge-*.whl") do if exist "%%~fF" (
  set /a WHEEL_COUNT+=1
  set "WHEEL=%%~fF"
)
if %WHEEL_COUNT% GTR 1 goto multiple_wheels
if defined WHEEL goto install_wheel
if exist "%~dp0pyproject.toml" goto install_source
echo [ERROR] Neither a release wheel nor pyproject.toml was found beside this file.
echo USER ACTION: Extract the complete Release ZIP, or run this file from the source repository root.
pause
exit /b 2

:multiple_wheels
echo [ERROR] More than one workbuddy_qmt_bridge wheel was found beside this file.
echo USER ACTION: Keep only the wheel from the current Release, then run this file again.
pause
exit /b 2

:install_wheel
echo Wheel: %WHEEL%
echo No input is required. Please wait...
%PYTHON_CMD% -m pip install --user --upgrade "%WHEEL%"
goto install_done

:install_source
echo Source repository: %~dp0
echo No input is required. Please wait...
%PYTHON_CMD% -m pip install --user --editable "%~dp0"

:install_done
if errorlevel 1 (
  echo.
  echo [ERROR] Package installation failed.
  echo USER ACTION:
  echo   - Review the pip error above.
  echo   - Confirm Python can write to your user package directory.
  echo   - Fix the reported Python/pip permission problem, then run this file again.
  pause
  exit /b 2
)
echo Package installation completed.
echo.

echo [Step 3/4] Starting the guided configuration.
echo The wizard will tell you when input is required.
echo Press Enter to accept a value shown in [brackets].
echo For a QMT account ID, leaving it empty skips QMT file generation for that account.
echo Existing bridge settings and QMT Profiles are preserved.
echo Changed Adapter/config files require confirmation; this installer never resets a Profile.
echo.
%PYTHON_CMD% -m workbuddy_qmt.manager setup
if errorlevel 1 (
  echo.
  echo [ERROR] Guided configuration did not complete.
  echo USER ACTION:
  echo   - Read the specific JSON error shown above.
  echo   - Correct the reported MCP JSON, path, account ID or file conflict.
  echo   - Run this file again. Existing trading configuration was not silently overwritten.
  pause
  exit /b 2
)

echo.
echo [Step 4/4] Setup completed. Manual actions are still required:
echo   1. Restart WorkBuddy so the qmt-bridge MCP entry is reloaded.
echo   2. Open the qmt_ready directory printed by the wizard above.
echo   3. For each generated account folder, put qmt_adapter.py into a separate QMT strategy instance.
echo   4. Keep qmt_adapter.json in OBSERVE_ONLY until the QMT mapping profile is verified and signed.
echo   5. Start the QMT strategy instance manually.
echo   6. Double-click the desktop Worker start script to start the Worker.
echo   7. Run the desktop bridge status script if WorkBuddy cannot see the bridge; it diagnoses abnormal status automatically.
echo.
echo Closing this installer does not start Worker, QMT or WorkBuddy.
echo For daily startup, runtime flow and troubleshooting, read README-RELEASE.zh-CN.md.
pause

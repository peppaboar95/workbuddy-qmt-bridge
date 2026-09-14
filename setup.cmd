@echo off
setlocal EnableExtensions
chcp 65001 >nul
set "PYTHONUTF8=1"
title WorkBuddy QMT Bridge Setup
for %%D in ("%~dp0.") do set "SOURCE_ROOT=%%~fD"
cd /d "%SOURCE_ROOT%"

if /i "%~1"=="--syntax-check" (
  echo BATCH_SYNTAX_OK
  exit /b 0
)

echo ============================================================
echo WorkBuddy-QMT Bridge - Install, Upgrade or Repair
echo ============================================================
echo This safe installer verifies the release wheel, installs it,
echo preserves existing configuration and signed Profiles, then runs
echo the guided setup. It never starts QMT or enables trading.
echo.

echo [Step 1/5] Checking Python 3.10 or newer...
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
  echo USER ACTION: Install Python from https://www.python.org/downloads/windows/
  echo Enable "Add Python to PATH", close this window, then run this file again.
  pause
  exit /b 2
)
for /f "usebackq delims=" %%V in (`%PYTHON_CMD% -c "import sys; print(sys.version.split()[0])"`) do set "PYTHON_VERSION=%%V"
echo Python: %PYTHON_VERSION% using %PYTHON_CMD%
echo.

if /i "%~1"=="--source-path-check" (
  %PYTHON_CMD% -c "import pathlib, sys; raise SystemExit(0 if pathlib.Path(sys.argv[1]).resolve() == pathlib.Path.cwd().resolve() else 1)" "%SOURCE_ROOT%"
  if errorlevel 1 (
    echo SOURCE_PATH_ERROR
    exit /b 1
  )
  echo SOURCE_PATH_OK
  exit /b 0
)

echo [Step 2/5] Locating the installation source...
set "WHEEL="
set "WHEEL_COUNT=0"
for %%F in ("%~dp0workbuddy_qmt_bridge-*.whl") do if exist "%%~fF" (
  set /a WHEEL_COUNT+=1
  set "WHEEL=%%~fF"
  set "WHEEL_NAME=%%~nxF"
)
if %WHEEL_COUNT% GTR 1 goto multiple_wheels
if defined WHEEL goto verify_wheel
if exist "%~dp0pyproject.toml" goto install_source
echo [ERROR] Neither a release wheel nor pyproject.toml was found beside this file.
echo USER ACTION: Extract the complete Release ZIP into a new folder and try again.
pause
exit /b 2

:multiple_wheels
echo [ERROR] More than one workbuddy_qmt_bridge wheel was found.
echo USER ACTION: Keep only the wheel from the current Release and try again.
pause
exit /b 2

:verify_wheel
echo [Step 3/5] Verifying the wheel SHA-256...
set "SUMS=%~dp0SHA256SUMS.txt"
if not exist "%SUMS%" (
  echo [ERROR] SHA256SUMS.txt is missing. The release package is incomplete.
  pause
  exit /b 2
)
set "EXPECTED_HASH="
for /f "tokens=1,2" %%H in ('findstr /i /c:"%WHEEL_NAME%" "%SUMS%"') do if /i "%%I"=="%WHEEL_NAME%" set "EXPECTED_HASH=%%H"
if not defined EXPECTED_HASH (
  echo [ERROR] SHA256SUMS.txt does not contain %WHEEL_NAME%.
  pause
  exit /b 2
)
set "WBQMT_WHEEL=%WHEEL%"
set "WBQMT_HASH_FILE=%TEMP%\workbuddy-qmt-hash-%RANDOM%-%RANDOM%.txt"
set "ACTUAL_HASH="
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $stream=[IO.File]::OpenRead($env:WBQMT_WHEEL); try { $hash=[BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($stream)).Replace('-','').ToLowerInvariant() } finally { $stream.Dispose() }; [IO.File]::WriteAllText($env:WBQMT_HASH_FILE, $hash, [Text.Encoding]::ASCII)" >nul 2>nul
if not errorlevel 1 if exist "%WBQMT_HASH_FILE%" set /p "ACTUAL_HASH="<"%WBQMT_HASH_FILE%"
if exist "%WBQMT_HASH_FILE%" del /q "%WBQMT_HASH_FILE%" >nul 2>nul
if not defined ACTUAL_HASH (
  echo [ERROR] Windows could not calculate the wheel SHA-256.
  pause
  exit /b 2
)
if /i not "%ACTUAL_HASH%"=="%EXPECTED_HASH%" (
  echo [ERROR] Wheel SHA-256 verification failed. Do not install this file.
  echo Expected: %EXPECTED_HASH%
  echo Actual:   %ACTUAL_HASH%
  pause
  exit /b 3
)
echo SHA-256 verified: %ACTUAL_HASH%
if /i "%~1"=="--verify-only" exit /b 0
goto install_wheel

:install_wheel
echo.
echo [Step 4/5] Installing or repairing %WHEEL_NAME%...
%PYTHON_CMD% -m pip --disable-pip-version-check install --user --upgrade --force-reinstall "%WHEEL%"
goto install_done

:install_source
if /i "%~1"=="--verify-only" (
  echo SOURCE_TREE_OK
  exit /b 0
)
echo [Step 3/5] Source tree selected; wheel verification is not applicable.
echo [Step 4/5] Installing the source tree in editable mode...
%PYTHON_CMD% -m pip --disable-pip-version-check install --user --editable "%SOURCE_ROOT%"

:install_done
if errorlevel 1 (
  echo.
  echo [ERROR] Package installation failed. Existing runtime data was not removed.
  echo USER ACTION: Review the pip error, fix the Python permission or path problem, then retry.
  pause
  exit /b 2
)
%PYTHON_CMD% -c "import workbuddy_qmt; print('Installed WorkBuddy-QMT Bridge ' + workbuddy_qmt.__version__)"
if errorlevel 1 (
  echo [ERROR] The installed package cannot be imported by the selected Python.
  pause
  exit /b 2
)
echo.

echo [Step 5/5] Starting guided configuration...
echo Existing bridge settings and signed Profiles will be preserved.
%PYTHON_CMD% -m workbuddy_qmt.manager setup
if errorlevel 1 (
  echo.
  echo [ERROR] Guided configuration did not complete. Read the specific error and retry.
  echo Existing trading configuration was not silently overwritten.
  pause
  exit /b 2
)

echo.
echo ============================================================
echo Setup completed. Finish the first read-only connection:
echo   1. Follow the per-account deployment guide opened by setup.
echo   2. Keep qmt_mode=OBSERVE_ONLY and start the QMT strategy.
echo   3. Start the desktop bridge, then run the desktop verification.
echo   4. Restart WorkBuddy and call qmt_health.
echo ============================================================
pause

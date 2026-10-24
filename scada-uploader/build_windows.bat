@echo off
setlocal
cd /d "%~dp0"

REM Build scada-uploader.exe on a 64-bit Windows PC. Do not run this on macOS.
REM Python is required only on the machine that builds the exe.

set "PY="
where python >nul 2>&1
if not errorlevel 1 set "PY=python"
if not defined PY (
    where py >nul 2>&1
    if not errorlevel 1 set "PY=py -3"
)
if not defined PY goto :missing_python

%PY% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)"
if errorlevel 1 goto :old_python

%PY% -c "import struct, sys; raise SystemExit(0 if struct.calcsize('P') == 8 else 1)"
if errorlevel 1 goto :not_64bit

if not exist "scada_uploader\__main__.py" (
    echo This script must be run from the scada-uploader folder that contains the scada_uploader package.
    exit /b 1
)

%PY% -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo Installing PyInstaller for this build...
    %PY% -m pip install pyinstaller
    if errorlevel 1 (
        echo Failed to install PyInstaller.
        exit /b 1
    )
)

echo Building dist\scada-uploader.exe ...
REM store and msvcrt are imported inside functions. fcntl is Unix-only.
%PY% -m PyInstaller --noconfirm --clean --onefile --noconsole --name scada-uploader --paths . --hidden-import scada_uploader.store --hidden-import msvcrt --exclude-module fcntl scada_uploader\__main__.py
if errorlevel 1 (
    echo PyInstaller failed.
    exit /b 1
)

if not exist "dist\scada-uploader.exe" (
    echo Build finished but dist\scada-uploader.exe was not created.
    exit /b 1
)

echo.
echo Build finished: %cd%\dist\scada-uploader.exe
echo The exe has no window. Status is written to scada_uploader.log and scada_uploader.sqlite.
echo.
echo Next steps:
echo   1. Copy dist\scada-uploader.exe, config.example.json and portal_ca.pem into one folder, for example C:\SCADA\uploader
echo      portal_ca.pem is the public GlobalSign Root R46 certificate, not a secret.
echo   2. In that folder, copy config.example.json to config.json. Set watch_folder to the full path of the SCADA CSV export folder.
echo      Write each backslash twice in paths, for example D:\\Plant\\SCADA\\Export.
echo      If HTTPS fails with CERTIFICATE_VERIFY_FAILED, set ca_bundle_file to portal_ca.pem.
echo      database_path and log_path can stay as scada_uploader.sqlite and scada_uploader.log.
echo      Those paths are resolved next to the config file, not from the process working directory.
echo   3. Copy register_scheduled_task.ps1 into that folder, or pass -InstallDir when you register.
echo   4. Optional dry run, which does not POST, run from that folder:
echo        cd /d C:\SCADA\uploader
echo        scada-uploader.exe --config config.json
echo   5. Register the 15-minute task:
echo        powershell -ExecutionPolicy Bypass -File .\register_scheduled_task.ps1 -InstallDir "C:\SCADA\uploader"
echo.
exit /b 0

:missing_python
echo Python was not found on PATH.
echo Install 64-bit Python 3.9 or newer from python.org and tick "Add python.exe to PATH".
echo Then open a new Command Prompt and run build_windows.bat again.
exit /b 1

:old_python
echo Python 3.9 or newer is required. The python command on PATH is older than 3.9.
exit /b 1

:not_64bit
echo 64-bit Python is required. The python command on PATH is 32-bit.
exit /b 1

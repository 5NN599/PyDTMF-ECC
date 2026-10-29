@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title PyDTMF-ECC - Windows Build
set "APP_NAME=PyDTMF-ECC"
set "EXE_NAME=PyDTMF-ECC.exe"
set "TEMP_BUILD=%TEMP%\PyDTMF_ECC_Build"
set "LOG=%TEMP%\PyDTMF_ECC_Build.log"
set "FAIL_LOG=%~dp0BUILD_FAILED.txt"
set "PY_CMD="

if exist "%FAIL_LOG%" del /q "%FAIL_LOG%" >nul 2>&1
if exist "%LOG%" del /q "%LOG%" >nul 2>&1
> "%LOG%" echo PyDTMF-ECC Windows build log
>> "%LOG%" echo Date: %DATE% %TIME%
>> "%LOG%" echo Folder: %CD%

echo.
echo  PyDTMF-ECC - Windows EXE Builder
echo  The finished EXE will be placed in this folder.
echo.

for %%F in (audio_file_transfer.py audio_file_transfer_live.py audio_file_transfer_gui.py pydtmf_ecc_entry.py requirements-build.txt pydtmf_ecc.ico version_info.txt) do (
  if not exist "%~dp0%%F" (
    >> "%LOG%" echo ERROR: Required file missing: %%F
    echo ERROR: Required file missing: %%F
    goto :failed
  )
)

rem Prefer Python 3.12, then 3.11, then 3.13; 64-bit is required.
for %%V in (3.12 3.11 3.13) do (
  py -%%V -c "import struct,sys; raise SystemExit(0 if struct.calcsize('P')*8==64 else 1)" >nul 2>&1
  if not errorlevel 1 if not defined PY_CMD set "PY_CMD=py -%%V"
)
if not defined PY_CMD (
  python -c "import struct,sys; raise SystemExit(0 if struct.calcsize('P')*8==64 and sys.version_info >= (3,11) and sys.version_info < (3,14) else 1)" >nul 2>&1
  if not errorlevel 1 set "PY_CMD=python"
)
if not defined PY_CMD (
  >> "%LOG%" echo ERROR: No supported 64-bit Python 3.11, 3.12, or 3.13 found.
  echo A supported 64-bit Python version was not found.
  echo Install Python 3.12 from https://www.python.org/downloads/windows/
  echo Enable "Add Python to PATH" during installation.
  goto :failed
)

echo [1/4] Preparing the isolated build environment...
>> "%LOG%" echo Python command: %PY_CMD%
call %PY_CMD% --version >> "%LOG%" 2>&1
if errorlevel 1 goto :failed
if not exist ".build_venv\Scripts\python.exe" (
  call %PY_CMD% -m venv .build_venv >> "%LOG%" 2>&1
  if errorlevel 1 goto :failed
)
call ".build_venv\Scripts\activate.bat"
if errorlevel 1 goto :failed

echo [2/4] Installing build dependencies (first build needs internet)...
python -m ensurepip --upgrade >> "%LOG%" 2>&1
if errorlevel 1 goto :failed
python -m pip install --disable-pip-version-check --upgrade pip >> "%LOG%" 2>&1
if errorlevel 1 goto :failed
python -m pip install --disable-pip-version-check -r requirements-build.txt >> "%LOG%" 2>&1
if errorlevel 1 goto :failed
python -c "import numpy,scipy,sounddevice,tkinter,_sounddevice_data; print('Dependencies OK')" >> "%LOG%" 2>&1
if errorlevel 1 goto :failed

echo [3/4] Building the single-file application...
if exist "%TEMP_BUILD%" rmdir /s /q "%TEMP_BUILD%" >nul 2>&1
mkdir "%TEMP_BUILD%" >nul 2>&1
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name "PyDTMF-ECC" ^
  --icon "%~dp0pydtmf_ecc.ico" ^
  --version-file "%~dp0version_info.txt" ^
  --add-data "%~dp0pydtmf_ecc.ico;." ^
  --distpath "%TEMP_BUILD%\dist" ^
  --workpath "%TEMP_BUILD%\work" ^
  --specpath "%TEMP_BUILD%\spec" ^
  --collect-all numpy ^
  --collect-all scipy ^
  --collect-all sounddevice ^
  --collect-all _sounddevice_data ^
  --copy-metadata numpy ^
  --copy-metadata scipy ^
  --copy-metadata sounddevice ^
  --hidden-import tkinter ^
  --hidden-import tkinter.ttk ^
  --hidden-import tkinter.filedialog ^
  --hidden-import tkinter.messagebox ^
  "%~dp0pydtmf_ecc_entry.py" >> "%LOG%" 2>&1
if errorlevel 1 goto :failed

if not exist "%TEMP_BUILD%\dist\%EXE_NAME%" (
  >> "%LOG%" echo ERROR: The EXE was not created in the temporary build folder.
  goto :failed
)

echo [4/4] Copying the EXE here and removing temporary build files...
copy /y "%TEMP_BUILD%\dist\%EXE_NAME%" "%~dp0%EXE_NAME%" >nul
if errorlevel 1 goto :failed
for %%Z in ("%~dp0%EXE_NAME%") do set "EXE_SIZE=%%~zZ"
if exist "%TEMP_BUILD%" rmdir /s /q "%TEMP_BUILD%" >nul 2>&1
if exist "%~dp0.build_venv" rmdir /s /q "%~dp0.build_venv" >nul 2>&1
if exist "%~dp0dist" rmdir /s /q "%~dp0dist" >nul 2>&1
if exist "%~dp0build" rmdir /s /q "%~dp0build" >nul 2>&1
if exist "%~dp0build_spec" rmdir /s /q "%~dp0build_spec" >nul 2>&1
for %%S in ("%~dp0*.spec") do if exist "%%~fS" del /q "%%~fS" >nul 2>&1
if exist "%FAIL_LOG%" del /q "%FAIL_LOG%" >nul 2>&1
if exist "%LOG%" del /q "%LOG%" >nul 2>&1

echo.
echo BUILD COMPLETE
echo File: %~dp0%EXE_NAME%
echo Size: %EXE_SIZE% bytes
echo Temporary build files have been removed.
echo.
echo Copy only "%EXE_NAME%" to another Windows PC for normal use.
echo The receiving PC does not need Python installed.
echo.
pause
exit /b 0

:failed
copy /y "%LOG%" "%FAIL_LOG%" >nul 2>&1
echo.
echo BUILD FAILED. Details were saved in BUILD_FAILED.txt.
echo Send that file if you need help troubleshooting the build.
echo.
if exist "%TEMP_BUILD%" rmdir /s /q "%TEMP_BUILD%" >nul 2>&1
if exist "%LOG%" del /q "%LOG%" >nul 2>&1
pause
exit /b 1

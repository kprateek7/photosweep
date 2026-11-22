@echo off
setlocal
set "VENV_DIR=%LOCALAPPDATA%\PhotoSweep\venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo Creating PhotoSweep Python environment in %VENV_DIR% ...
    python -m venv "%VENV_DIR%"
    "%VENV_PY%" -m pip install --upgrade pip
    "%VENV_PY%" -m pip install -r "%~dp0requirements.txt"
)

echo Launching PhotoSweep...
"%VENV_PY%" "%~dp0app.py" %*
pause

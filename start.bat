@echo off
setlocal
cd /d "%~dp0"
rem Developer/source launcher. End users should double-click dist\OCIBot\OCIBot.exe.
rem First-time setup may show this console; the GUI is then started detached so this window can close.

set "ROOT=%~dp0"
set "PY=%ROOT%.venv\Scripts\python.exe"
set "PYW=%ROOT%.venv\Scripts\pythonw.exe"
set "APP=%ROOT%main.py"

if not exist "%PY%" (
  echo Creating virtual environment...
  where python >nul 2>nul || (
    echo Python is required for source development. Use the packaged OCIBot.exe on another computer.
    pause
    exit /b 1
  )
  python -m venv "%ROOT%.venv" || (
    echo Failed to create .venv
    pause
    exit /b 1
  )
  "%PY%" -m pip install -U pip || (
    echo Failed to upgrade pip
    pause
    exit /b 1
  )
)

if not exist "%PYW%" (
  echo Missing pythonw.exe in .venv\Scripts - reinstall the virtual environment.
  pause
  exit /b 1
)

if not exist "%APP%" (
  echo Missing main.py next to start.bat
  pause
  exit /b 1
)

"%PY%" -c "import oci, cryptography, passlib, pyzipper" 2>nul
if errorlevel 1 (
  echo Installing dependencies...
  "%PY%" -m pip install -r "%ROOT%requirements.txt" || (
    echo Failed to install requirements
    pause
    exit /b 1
  )
)

rem Detach: start's first quoted token is the window TITLE.
rem /D sets the working directory. pythonw = no console attached to the GUI.
start "OCIBot" /D "%ROOT%." "%PYW%" "%APP%"
exit /b 0
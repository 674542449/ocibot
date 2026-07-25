@echo off
setlocal
cd /d "%~dp0"
if not exist ".build-venv\Scripts\python.exe" (
  echo Creating clean build environment...
  py -3.13 -m venv .build-venv || exit /b 1
)
".build-venv\Scripts\python.exe" -m pip install -U pip || exit /b 1
".build-venv\Scripts\python.exe" -m pip install -r requirements-windows-lock.txt -r requirements-build.txt || exit /b 1
".build-venv\Scripts\pyinstaller.exe" --noconfirm --clean --distpath dist --workpath build packaging\ocibot.spec || exit /b 1
echo.
echo Built dist\OCIBot\OCIBot.exe
endlocal

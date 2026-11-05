@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 用 pyw / pythonw 启动：不弹黑色命令行窗口。
where pyw >nul 2>nul
if %errorlevel%==0 (
  start "" pyw -3 "%~dp0ip_api_gui.py"
  exit /b
)
where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw "%~dp0ip_api_gui.py"
  exit /b
)
echo 没找到 Python。请先安装 Python 3：https://www.python.org/downloads/
echo 安装时勾选「Add python.exe to PATH」，装好后再双击本文件。
pause

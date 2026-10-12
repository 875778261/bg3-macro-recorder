@echo off
chcp 65001 >nul
cd /d "d:\workspace\mouse-script"
set PY="d:\workspace\mouse-script\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [错误] 找不到虚拟环境，请先安装依赖。
  pause
  exit /b 1
)
start "" /min "d:\workspace\mouse-script\.venv\Scripts\pythonw.exe" "d:\workspace\mouse-script\launcher.py"

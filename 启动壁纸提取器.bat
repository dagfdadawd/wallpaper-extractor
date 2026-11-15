@echo off
rem 壁纸提取器 App 启动器（无黑窗口）
cd /d "%~dp0"
where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw "%~dp0wall_app.py"
) else (
  start "" python "%~dp0wall_app.py"
)

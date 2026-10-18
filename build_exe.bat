@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [1/2] 安装 pyinstaller ...
py -3 -m pip install pyinstaller -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 (
  echo 安装失败，请检查网络或 Python 环境。
  pause
  exit /b 1
)
echo [2/2] 打包 exe ...
py -3 -m PyInstaller --onefile --windowed --name epub_tool --clean epub_tool.py
if errorlevel 1 (
  echo 打包失败。
  pause
  exit /b 1
)
echo.
echo 完成：dist\epub_tool.exe
pause
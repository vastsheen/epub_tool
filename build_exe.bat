@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [1/3] 安装依赖 ...
rem 功能 6（漫画→CBZ）需要 pillow/pypdf/cryptography；不装也能打包，但该标签页会禁用
py -3 -m pip install pyinstaller pillow pypdf cryptography -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 (
  echo 安装失败，请检查网络或 Python 环境。
  pause
  exit /b 1
)
echo [2/3] 自检 ...
py -3 epub_tool.py --selftest
if errorlevel 1 (
  echo 自检未通过，继续打包会产出坏 exe。
  pause
  exit /b 1
)
echo [3/3] 打包 exe ...
rem numpy/scipy/pandas 不在依赖里，排除掉可让 exe 小一大截
py -3 -m PyInstaller --onefile --windowed --name epub_tool --clean --noconfirm ^
  --exclude-module numpy --exclude-module scipy --exclude-module pandas ^
  --exclude-module matplotlib --exclude-module pytest epub_tool.py
if errorlevel 1 (
  echo 打包失败。
  pause
  exit /b 1
)
echo.
echo 完成：dist\epub_tool.exe
pause

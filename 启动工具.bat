@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PYCMD="
where py >nul 2>nul && set "PYCMD=py -3"
if not defined PYCMD where python >nul 2>nul && set "PYCMD=python"
if not defined PYCMD (
  echo [错误] 未找到 Python 3。
  echo 请到 https://www.python.org/downloads/ 安装 Python 3（安装时勾选 Add to PATH）。
  pause
  exit /b 1
)
echo 使用解释器：%PYCMD%
%PYCMD% epub_tool.py
if errorlevel 1 (
  echo.
  echo [提示] 程序异常退出（错误码 %errorlevel%）。
  echo 请确认安装的是 python.org 官方 Python 3（自带 tkinter），而不是 Microsoft Store 版本。
  echo 可先运行：%PYCMD% epub_tool.py --selftest 检查核心功能。
  pause
)

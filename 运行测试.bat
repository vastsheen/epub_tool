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

echo [1/2] 回归测试 ...
%PYCMD% -m unittest discover -s tests -v
if errorlevel 1 goto fail

echo.
echo [2/2] 端到端冒烟自检 ...
%PYCMD% epub_tool.py --selftest
if errorlevel 1 goto fail

echo.
echo 全部通过。
pause
exit /b 0

:fail
echo.
echo 有测试未通过，请查看上方输出。
pause
exit /b 1

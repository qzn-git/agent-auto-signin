@echo off
REM ============================================
REM  agent-auto-signin 一键启动（可见黑框）
REM  双击运行，会启动 Flask 服务在 http://127.0.0.1:8765
REM  关闭本窗口即停止服务
REM ============================================

cd /d "%~dp0"
set PYTHONUNBUFFERED=1

echo.
echo ============================================
echo   agent-auto-signin 启动中...
echo   前端: http://127.0.0.1:8765/
echo   状态: http://127.0.0.1:8765/api/status
echo   关闭本窗口可停止服务
echo ============================================
echo.

call ".venv\Scripts\python.exe" server.py
pause

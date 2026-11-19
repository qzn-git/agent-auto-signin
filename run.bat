@echo off
REM ============================================
REM  agent-auto-signin launcher (visible console)
REM  Start Flask at http://127.0.0.1:8765
REM  Close this window to stop
REM ============================================

cd /d "%~dp0"
set PYTHONUNBUFFERED=1

echo.
echo ============================================
echo   agent-auto-signin starting...
echo   UI  : http://127.0.0.1:8765/
echo   API : http://127.0.0.1:8765/api/status
echo   Close this window to stop
echo ============================================
echo.

call ".venv\Scripts\python.exe" server.py
pause

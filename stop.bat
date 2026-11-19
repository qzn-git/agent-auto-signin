@echo off
REM ============================================
REM  Stop agent-auto-signin (kill by port 8765)
REM ============================================
echo.
echo [agent-auto-signin] Stopping...

set FOUND=0

REM Method 1: kill by listening port 8765
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8765 " ^| findstr "LISTENING"') do (
    echo   PID %%a is using port 8765, killing...
    taskkill /F /PID %%a 2>nul
    if not errorlevel 1 (
        echo   [OK] PID %%a killed
        set FOUND=1
    )
)

REM Method 2: kill python processes whose cmdline contains server.py
wmic process where "name='python.exe'" get ProcessId,CommandLine 2>nul | findstr /i "server.py" | for /f "tokens=1" %%p in ('more') do (
    echo   Kill python PID %%p ^(server.py^)
    taskkill /F /PID %%p 2>nul
    if not errorlevel 1 set FOUND=1
)

if "%FOUND%"=="0" (
    echo   Nothing is running on port 8765.
) else (
    echo.
    echo   Done.
)

echo.
timeout /t 2 >nul

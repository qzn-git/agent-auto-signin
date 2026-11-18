@echo off
REM ============================================
REM  停止 agent-auto-signin 后台服务
REM ============================================

echo 正在停止 agent-auto-signin 服务...
taskkill /F /IM python.exe /FI "WINDOWTITLE eq agent-auto-signin*" 2>nul

REM 按端口兜底（如果上面没匹配到，直接杀 8765 端口的进程）
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :8765 ^| findstr LISTENING') do (
    echo 发现 PID %%a 占用 8765 端口，正在终止...
    taskkill /F /PID %%a 2>nul
)

echo 完成。
timeout /t 2 >nul

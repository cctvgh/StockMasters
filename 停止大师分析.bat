@echo off
chcp 936 >nul
title 停止 A股大师分析
echo ============================================
echo    A股大师分析系统 - 停止
echo ============================================
echo.
echo 正在停止服务...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8020" ^| findstr "LISTENING"') do (
    taskkill /PID %%a /T /F >nul 2>&1
)
timeout /t 2 >nul
echo.
echo ============================================
echo   A股大师分析服务已停止
echo ============================================
pause >nul

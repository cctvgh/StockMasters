@echo off
chcp 936 >nul
title 启动 A股大师分析
echo ============================================
echo    A股大师分析系统 - 启动
echo ============================================
echo.
echo 正在启动 Web 服务 端口 8020...
start "A股大师分析服务" "D:\StockMasters\venv\Scripts\python.exe" "D:\StockMasters\server.py"
timeout /t 5 >nul
echo.
echo 正在打开浏览器...
start "" "http://localhost:8020"
echo.
echo ============================================
echo   服务已启动 地址 http://localhost:8020
echo   关闭服务窗口或双击 停止大师分析.bat
echo ============================================
echo.
echo 此窗口可关闭 不影响服务运行
pause >nul

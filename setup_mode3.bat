@echo off
chcp 65001 >nul
title AI Bridge - Mode 3 launcher
echo ============================================================
echo   AI Bridge - Mode 3 (web drives your local opencode)
echo   double-click to install and start
echo ============================================================
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://ashbringerf.github.io/ai-bridge-web/setup_mode3.ps1 | iex"
echo.
pause

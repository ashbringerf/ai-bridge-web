@echo off
chcp 65001 >nul
title AI Bridge relay
echo ============================================================
echo   AI Bridge relay launcher
echo   double-click to install and start relay
echo ============================================================
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://ashbringerf.github.io/ai-bridge-web/relay.ps1 | iex"
echo.
pause

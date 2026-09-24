@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set HEADLESS=false
rem 工作目录必须是本文件所在目录（config.json / artifacts 都按它解析）。
rem 原先写死 C:\Users\Administrator\Desktop\watch-bridge，工程换到别的盘就拿不到 config.json。
cd /d "%~dp0"

rem python 解释器：优先用 WorkBuddy 托管环境（playwright/requests/openpyxl 都装在里面），
rem 找不到就退回 PATH 上的 python。
set "PY=C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
echo ==================================================
echo   Douyin Watch Bridge - Starting
echo ==================================================
echo  Keep this window AND the browser window open.
echo  Closing the Chromium window breaks the Douyin
echo  session and you will have to restart this service.
echo.
echo  On the watch app, fill in the "computer address"
echo  printed below as  IP:port  (same Wi-Fi).
echo  This is a raw TCP service, NOT a website - the
echo  watch app speaks a binary frame protocol.
echo.
echo  This window: the service turns OFF QuickEdit mode
echo  on it at startup, so clicking here will NOT freeze
echo  the service. Full log: artifacts\tcp_server.log
echo.
echo  First run only: a setup page opens in your browser.
echo  It shows the address + token to type on the watch,
echo  and lets you change the port / token / image sizes.
echo  Blocks: service settings / Douyin account / which
echo  friends show up on the watch (you can read them
echo  straight out of Douyin and just tick the ones you
echo  want - validated before writing, so a typo can
echo  never break your config).
echo  Later on it lives at  http://127.0.0.1:8788/  while
echo  the service is running (add --setup to reopen it).
echo ==================================================
echo.
"%PY%" scripts\clear_stale_lock.py
echo.
"%PY%" scripts\watch_server.py
echo.
echo =========== Service stopped. Press any key =========
pause

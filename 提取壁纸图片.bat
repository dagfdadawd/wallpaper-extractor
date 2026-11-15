@echo off
setlocal
title Wallpaper Engine Image Extractor
echo.
echo Extracting wallpaper images, please wait...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0extract-wallpaper.ps1" %*
echo.
echo.
echo Done. Press any key to close this window...
pause >nul

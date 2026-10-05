@echo off
echo Stopping all services...
taskkill /F /IM mediamtx.exe 2>nul
taskkill /F /IM ffmpeg.exe 2>nul
taskkill /F /IM python.exe 2>nul
echo Done.
timeout /t 2 /nobreak >nul
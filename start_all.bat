@echo off
setlocal

set APP_DIR=D:\ariessda\cctv_stream

cd /d "%APP_DIR%"

echo [1/2] Starting MediaMTX...
start "MediaMTX" cmd /k mediamtx.exe mediamtx.yml

timeout /t 3 /nobreak >nul

echo [2/2] Starting Flask App (auto-sync kamera)...
start "Flask" cmd /k python app.py

echo.
echo ============================================
echo  Sistem berjalan!
echo  - MediaMTX  : http://127.0.0.1:8888
echo  - API       : http://127.0.0.1:9997
echo  - Web UI    : http://localhost:5001
echo ============================================
pause
endlocal





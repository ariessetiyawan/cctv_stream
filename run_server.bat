@echo off
setlocal enabledelayedexpansion

set LOG_DIR=logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

set SERVER_LOG=%LOG_DIR%\server_wrapper.log
set RESTART_COUNT=0

:START
echo. >> "%SERVER_LOG%"
echo ============================================ >> "%SERVER_LOG%"
echo [%date% %time%] Starting server (restart #%RESTART_COUNT%) >> "%SERVER_LOG%"
echo ============================================ >> "%SERVER_LOG%"

python app.py >> "%SERVER_LOG%" 2>&1

set EXIT_CODE=%ERRORLEVEL%

echo. >> "%SERVER_LOG%"
echo [%date% %time%] SERVER DIED with exit code %EXIT_CODE% >> "%SERVER_LOG%"
echo ============================================ >> "%SERVER_LOG%"

set /a RESTART_COUNT+=1

if %RESTART_COUNT% GEQ 10 (
    echo [%date% %time%] Too many restarts, stopping. >> "%SERVER_LOG%"
    exit /b 1
)

echo [%date% %time%] Restarting in 5 seconds... >> "%SERVER_LOG%"
timeout /t 5 /nobreak >nul
goto START
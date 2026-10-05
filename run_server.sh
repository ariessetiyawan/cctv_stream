#!/bin/bash

LOG_DIR="logs"
mkdir -p "$LOG_DIR"

SERVER_LOG="$LOG_DIR/server_wrapper.log"
RESTART_COUNT=0
MAX_RESTARTS=10

while [ $RESTART_COUNT -lt $MAX_RESTARTS ]; do
    echo "" >> "$SERVER_LOG"
    echo "============================================" >> "$SERVER_LOG"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting server (restart #$RESTART_COUNT)" >> "$SERVER_LOG"
    echo "============================================" >> "$SERVER_LOG"

    python app.py >> "$SERVER_LOG" 2>&1
    EXIT_CODE=$?

    echo "" >> "$SERVER_LOG"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] SERVER DIED with exit code $EXIT_CODE" >> "$SERVER_LOG"
    echo "============================================" >> "$SERVER_LOG"

    RESTART_COUNT=$((RESTART_COUNT + 1))

    if [ $EXIT_CODE -eq 0 ]; then
        echo "[$(date)] Clean exit, stopping." >> "$SERVER_LOG"
        exit 0
    fi

    echo "[$(date)] Restarting in 5 seconds..." >> "$SERVER_LOG"
    sleep 5
done

echo "[$(date)] Too many restarts ($MAX_RESTARTS), stopping." >> "$SERVER_LOG"
exit 1
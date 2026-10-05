#!/bin/bash

# ============================================
#  Start Script - MediaMTX + Flask App
#  (Konversi dari start.bat Windows)
# ============================================

APP_DIR="/home/administrator/cctv"
VENV_DIR="/home/administrator/venv"

# ------------------------------------------------------------
# 1) Cek direktori penting
# ------------------------------------------------------------
if [ ! -d "$APP_DIR" ]; then
    echo "ERROR: Direktori $APP_DIR tidak ditemukan!"
    exit 1
fi

if [ ! -f "$VENV_DIR/bin/activate" ]; then
    echo "ERROR: Venv tidak ditemukan di $VENV_DIR"
    echo "       Pastikan venv sudah dibuat: python3 -m venv $VENV_DIR"
    exit 1
fi

# ------------------------------------------------------------
# 2) Aktifkan venv
# ------------------------------------------------------------
echo "Mengaktifkan virtual environment: $VENV_DIR"
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# Verifikasi python yang dipakai benar-benar dari venv
PYTHON_BIN=$(which python)
echo "Python aktif : $PYTHON_BIN"
echo "Python versi : $($PYTHON_BIN --version 2>&1)"
echo ""

# ------------------------------------------------------------
# 3) Masuk ke folder aplikasi
# ------------------------------------------------------------
cd "$APP_DIR" || {
    echo "ERROR: Gagal masuk ke $APP_DIR"
    exit 1
}

# ------------------------------------------------------------
# 4) Jalankan MediaMTX
# ------------------------------------------------------------
echo "[1/2] Starting MediaMTX..."
./mediamtx mediamtx.yml > mediamtx.log 2>&1 &
MEDIAMTX_PID=$!
echo "MediaMTX PID: $MEDIAMTX_PID"

sleep 3

# Cek MediaMTX masih hidup?
if ! kill -0 "$MEDIAMTX_PID" 2>/dev/null; then
    echo "ERROR: MediaMTX gagal start. Cek log:"
    tail -20 mediamtx.log
    deactivate 2>/dev/null
    exit 1
fi

# ------------------------------------------------------------
# 5) Jalankan Flask (pakai python dari venv)
# ------------------------------------------------------------
echo "[2/2] Starting Flask App (auto-sync kamera)..."
# Karena venv sudah aktif, 'python' = python dari venv
python app.py > flask.log 2>&1 &
FLASK_PID=$!
echo "Flask PID: $FLASK_PID"

# Cek Flask masih hidup setelah 2 detik
sleep 2
if ! kill -0 "$FLASK_PID" 2>/dev/null; then
    echo "ERROR: Flask gagal start. Cek log:"
    tail -20 flask.log
    kill "$MEDIAMTX_PID" 2>/dev/null
    deactivate 2>/dev/null
    exit 1
fi

# ------------------------------------------------------------
# 6) Info
# ------------------------------------------------------------
echo ""
echo "============================================"
echo " Sistem berjalan!"
echo "  - MediaMTX  : http://127.0.0.1:8888"
echo "  - API       : http://127.0.0.1:9997"
echo "  - Web UI    : http://localhost:5001"
echo "============================================"
echo ""
echo "Log MediaMTX : $APP_DIR/mediamtx.log"
echo "Log Flask    : $APP_DIR/flask.log"
echo ""
echo "Tekan CTRL+C untuk menghentikan kedua service..."

# ------------------------------------------------------------
# 7) Handle Ctrl+C -> matikan kedua proses
# ------------------------------------------------------------
cleanup() {
    echo ""
    echo "Menghentikan service..."
    kill "$MEDIAMTX_PID" 2>/dev/null
    kill "$FLASK_PID" 2>/dev/null

    # Tunggu graceful, paksa setelah 5 detik
    sleep 1
    kill -0 "$MEDIAMTX_PID" 2>/dev/null && kill -9 "$MEDIAMTX_PID" 2>/dev/null
    kill -0 "$FLASK_PID" 2>/dev/null    && kill -9 "$FLASK_PID" 2>/dev/null

    wait "$MEDIAMTX_PID" 2>/dev/null
    wait "$FLASK_PID" 2>/dev/null

    deactivate 2>/dev/null
    echo "Selesai."
    exit 0
}
trap cleanup SIGINT SIGTERM

# Tunggu sampai salah satu proses berhenti
wait
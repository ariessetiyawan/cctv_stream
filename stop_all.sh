#!/bin/bash
# ============================================
#  Stop All Services — via systemd
# ============================================
set -u

echo "Stopping all services..."
echo ""

STOPPED=0

# ------------------------------------------------------------
# Flask
# ------------------------------------------------------------
if systemctl is-active --quiet cctv-flask.service 2>/dev/null; then
    if sudo systemctl stop cctv-flask.service; then
        echo "  ? cctv-flask.service stopped"
        STOPPED=1
    else
        echo "  ? GAGAL stop cctv-flask.service"
        echo "    Coba jalankan: sudo ./stop_all.sh"
    fi
else
    echo "  · cctv-flask.service sudah tidak aktif"
fi

# ------------------------------------------------------------
# MediaMTX
# ------------------------------------------------------------
if systemctl is-active --quiet mediamtx.service 2>/dev/null; then
    if sudo systemctl stop mediamtx.service; then
        echo "  ? mediamtx.service stopped"
    else
        echo "  ? GAGAL stop mediamtx.service"
    fi
else
    echo "  · mediamtx.service sudah tidak aktif"
fi

# ------------------------------------------------------------
# Verifikasi
# ------------------------------------------------------------
echo ""
echo "Status akhir:"
sleep 2

FLASK_STATUS=$(systemctl is-active cctv-flask.service 2>/dev/null)
MTX_STATUS=$(systemctl is-active mediamtx.service 2>/dev/null)

echo "  Flask   : $FLASK_STATUS"
echo "  MediaMTX: $MTX_STATUS"

echo ""
echo "Proses Python/FFmpeg/MediaMTX terkait:"
REMAIN=$(pgrep -af "mediamtx|ffmpeg|cctv/app\.py" 2>/dev/null)
if [ -n "$REMAIN" ]; then
    echo "$REMAIN"
    echo ""
    echo "  ??  Masih ada proses tersisa"
else
    echo "  (bersih)"
fi

echo ""
if [ "$FLASK_STATUS" = "inactive" ]; then
    echo "? Semua service berhasil dihentikan."
else
    echo "? Flask masih aktif — pastikan script dijalankan dengan sudo"
fi

sleep 2
"""
Diagnostik channel CCTV — cek dari DVR, MediaMTX, sampai HLS.
Jalankan: python diagnose_ch21.py <CHANNEL>
"""
import sys
import subprocess
import requests
from requests.auth import HTTPDigestAuth

CH      = int(sys.argv[1]) if len(sys.argv) > 1 else 21
DVR_IP  = "192.168.22.5"
DVR_USER = "Rsudjbg"
DVR_PASS = "Simrs1038"
MTX_API  = "http://127.0.0.1:9997"
MTX_HLS  = "http://127.0.0.1:8888"
MTX_USER = "flask_proxy"
MTX_PASS = "Tr@nsdigi53$_proxy"

print(f"\n{'='*60}")
print(f"  DIAGNOSTIK CHANNEL {CH}")
print(f"{'='*60}\n")

# ------------------------------------------------------------
# 1. Test RTSP langsung ke DVR
# ------------------------------------------------------------
print("[1/5] Test RTSP ke DVR...")
url = f"rtsp://{DVR_USER}:{DVR_PASS}@{DVR_IP}:554/cam/realmonitor?channel={CH}&subtype=0"
try:
    p = subprocess.run(
        ["ffprobe", "-rtsp_transport", "tcp",
         "-timeout", "8000000", "-v", "error",
         "-select_streams", "v:0",
         "-show_entries", "stream=codec_name,width,height",
         "-of", "default=noprint_wrappers=1", "-i", url],
        capture_output=True, text=True, timeout=15,
    )
    if p.returncode == 0 and p.stdout.strip():
        print(f"  ✅ RTSP OK")
        for line in p.stdout.strip().splitlines():
            print(f"     {line}")
    else:
        print(f"  ❌ RTSP GAGAL")
        print(f"     {p.stderr.strip()[:200]}")
except Exception as e:
    print(f"  ❌ Error: {e}")

# ------------------------------------------------------------
# 2. Cek path di MediaMTX
# ------------------------------------------------------------
print(f"\n[2/5] Cek MediaMTX path...")
try:
    r = requests.get(f"{MTX_API}/v3/paths/list", timeout=5)
    items = r.json().get("items", [])
    matches = [p for p in items if f"21" in p.get("name", "") or f"ch{CH}" in p.get("name", "").lower()]
    if not matches:
        print(f"  ❌ Tidak ada path untuk channel {CH}")
        print(f"     Cek apakah sudah di-import via /api/camera/bulk-add")
    for p in matches:
        print(f"  Path: {p['name']}")
        print(f"     ready     : {p.get('ready')}")
        print(f"     tracks    : {p.get('tracks')}")
        print(f"     readers   : {p.get('readers', 0)}")
        src = p.get("source") or {}
        print(f"     source    : {src.get('type', '?')}")
except Exception as e:
    print(f"  ❌ Error akses MediaMTX API: {e}")

# ------------------------------------------------------------
# 3. Test manifest HLS langsung
# ------------------------------------------------------------
print(f"\n[3/5] Test manifest HLS langsung di MediaMTX...")
for p in matches if 'matches' in dir() else []:
    name = p["name"]
    try:
        r = requests.get(
            f"{MTX_HLS}/{name}/index.m3u8",
            auth=(MTX_USER, MTX_PASS),
            timeout=10,
        )
        print(f"  GET /{name}/index.m3u8 → HTTP {r.status_code}")
        if r.status_code == 200:
            lines = r.text.splitlines()[:8]
            print("     Preview manifest:")
            for line in lines:
                print(f"       {line}")
        else:
            print(f"     Response: {r.text[:200]}")
    except Exception as e:
        print(f"  ❌ {name}: {e}")

# ------------------------------------------------------------
# 4. Cek log MediaMTX (tail)
# ------------------------------------------------------------
print(f"\n[4/5] Cek log MediaMTX (10 baris terakhir)...")
try:
    with open("mediamtx.log", "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()
    for line in lines[-10:]:
        print(f"  {line.rstrip()}")
except FileNotFoundError:
    print(f"  ⚠️  File mediamtx.log tidak ditemukan")
except Exception as e:
    print(f"  ⚠️  {e}")

# ------------------------------------------------------------
# 5. Kesimpulan
# ------------------------------------------------------------
print(f"\n{'='*60}")
print("  KESIMPULAN")
print(f"{'='*60}\n")
print("Jika RTSP OK tapi HLS gagal:")
print("  → Cek log MediaMTX (Lapis 3)")
print("  → Pastikan hlsVariant: fmp4 di mediamtx.yml")
print("Jika HLS MediaMTX OK tapi via Flask gagal:")
print("  → Cek kredensial MediaMTX di config.py")
print("  → Cek stream_proxy di app.py")
print("Jika semua OK tapi browser tetap gagal:")
print("  → Buka F12 → Console → lihat [HLS ERROR]")
"""
Cek semua channel DVR: mana yang H.264, mana yang H.265,
dan mana yang gagal di MediaMTX.
"""
import subprocess
import concurrent.futures

DVR_IP   = "192.168.22.5"
DVR_PORT = 554
USER     = "Rsudjbg"
PASS     = "Simrs1038"
MAX_CH   = 32
SUBTYPE  = 1


def probe(ch):
    url = (f"rtsp://{USER}:{PASS}@{DVR_IP}:{DVR_PORT}"
           f"/cam/realmonitor?channel={ch}&subtype={SUBTYPE}")
    try:
        p = subprocess.run(
            ["ffprobe", "-rtsp_transport", "tcp",
             "-timeout", "5000000", "-v", "error",
             "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,width,height",
             "-of", "csv=p=0", "-i", url],
            capture_output=True, text=True, timeout=10,
        )
        if p.returncode == 0 and p.stdout.strip():
            parts = p.stdout.strip().split(",")
            return ch, parts[0], parts[1] if len(parts) > 1 else "?", parts[2] if len(parts) > 2 else "?"
    except Exception:
        pass
    return ch, None, None, None


print(f"{'CH':>3} | {'Codec':<6} | {'Resolusi':<12} | Status")
print("-" * 50)
h265 = []
failed = []

with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
    futures = {ex.submit(probe, ch): ch for ch in range(1, MAX_CH + 1)}
    for f in concurrent.futures.as_completed(futures):
        ch, codec, w, h = f.result()
        if codec is None:
            print(f"{ch:>3} | {'—':<6} | {'—':<12} | ❌ FAILED")
            failed.append(ch)
        else:
            flag = "⚠️  H.265" if codec == "hevc" else ""
            print(f"{ch:>3} | {codec:<6} | {w}x{h:<8} | ✅ {flag}")
            if codec == "hevc":
                h265.append(ch)

print()
print(f"Channel H.265 (perlu fmp4/lowLatency): {h265}")
print(f"Channel gagal probe: {failed}")
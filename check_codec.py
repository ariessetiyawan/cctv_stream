"""Cek codec semua channel DVR — jalankan sebelum/sesudah config change."""
import subprocess, concurrent.futures

DVR = "rtsp://Rsudjbg:Simrs1038@192.168.22.5:554"
MAX_CH = 32
SUBTYPE = 0  # main stream

def probe(ch):
    url = f"{DVR}/cam/realmonitor?channel={ch}&subtype={SUBTYPE}"
    try:
        p = subprocess.run(
            ["ffprobe", "-rtsp_transport", "tcp", "-timeout", "5000000",
             "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,width,height",
             "-of", "csv=p=0", "-i", url],
            capture_output=True, text=True, timeout=10,
        )
        if p.returncode == 0 and p.stdout.strip():
            parts = p.stdout.strip().split(",")
            return ch, parts[0], f"{parts[1]}x{parts[2]}" if len(parts) >= 3 else "?"
    except Exception:
        pass
    return ch, None, None

h264, h265, failed = [], [], []
with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
    for ch, codec, res in ex.map(probe, range(1, MAX_CH + 1)):
        if codec is None:
            failed.append(ch)
            print(f"CH{ch:>3} | ❌ FAILED")
        elif codec == "hevc":
            h265.append(ch)
            print(f"CH{ch:>3} | ⚠️  H.265  | {res}")
        else:
            h264.append(ch)
            print(f"CH{ch:>3} | ✅ {codec:<5} | {res}")

print()
print(f"H.264 : {len(h264)} channel → {h264}")
print(f"H.265 : {len(h265)} channel → {h265}")
print(f"Failed: {len(failed)} channel → {failed}")
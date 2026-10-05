"""
Deteksi otomatis channel DVR:
- Prioritas 1: Dahua CGI API (dapat nama channel + jumlah)
- Fallback   : RTSP probing paralel via ffprobe
"""
import subprocess
import concurrent.futures
import requests
from requests.auth import HTTPDigestAuth


# ============================================================
# Dahua CGI
# ============================================================
def probe_dahua_cgi(ip: str, user: str, passwd: str, http_port: int = 80,
                    timeout: int = 5):
    """
    Ambil nama channel dari Dahua CGI.
    Return dict {channel_index_1_based: nama} atau None jika gagal.
    """
    base = f"http://{ip}:{http_port}/cgi-bin/configManager.cgi"
    try:
        r = requests.get(
            f"{base}?action=getConfig&name=ChannelTitle",
            auth=HTTPDigestAuth(user, passwd),
            timeout=timeout,
        )
    except Exception as e:
        print(f"[probe] CGI {ip}:{http_port} gagal: {e}")
        return None

    if r.status_code != 200:
        print(f"[probe] CGI status {r.status_code}")
        return None

    titles = {}
    for line in r.text.splitlines():
        line = line.strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        if ".Name" in key and "[" in key and "]" in key:
            try:
                idx = int(key.split("[")[1].split("]")[0])
                val = val.strip()
                if val:
                    titles[idx + 1] = val  # 0-based di CGI → 1-based
            except (ValueError, IndexError):
                pass
    return titles or None


# ============================================================
# RTSP probing
# ============================================================
def _probe_single(ip, port, user, passwd, channel, subtype, timeout):
    url = (
        f"rtsp://{user}:{passwd}@{ip}:{port}"
        f"/cam/realmonitor?channel={channel}&subtype={subtype}"
    )
    try:
        p = subprocess.run(
            ["ffprobe",
             "-rtsp_transport", "tcp",
             "-timeout", str(int(timeout * 1_000_000)),
             "-v", "error",
             "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,width,height",
             "-of", "csv=p=0",
             "-i", url],
            capture_output=True, text=True, timeout=timeout + 4,
        )
        if p.returncode == 0 and p.stdout.strip():
            parts = p.stdout.strip().split(",")
            return {
                "channel": channel,
                "codec":   parts[0] if len(parts) > 0 else "",
                "width":   int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0,
                "height":  int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0,
            }
    except Exception:
        pass
    return None


def probe_channels_parallel(ip, port, user, passwd,
                            max_channels=32, subtype=1,
                            timeout=3, workers=8):
    """Cek banyak channel sekaligus."""
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(_probe_single, ip, port, user, passwd, ch, subtype, timeout): ch
            for ch in range(1, max_channels + 1)
        }
        for f in concurrent.futures.as_completed(futures):
            ch = futures[f]
            try:
                r = f.result()
                if r:
                    results[ch] = r
            except Exception:
                pass
    return results


# ============================================================
# High-level
# ============================================================
def detect_dvr_cameras(ip, rtsp_port, user, passwd, max_channels=32, subtype=1, use_cgi=True):
    """
    Return list of dict:
      {channel, name, codec, width, height, rtsp_url}
    """
    titles = None
    if use_cgi:
        # coba port 80 dulu, lalu 443
        for http_port in (80, 443):
            titles = probe_dahua_cgi(ip, user, passwd, http_port=http_port)
            if titles:
                print(f"[probe] CGI OK di port {http_port}, "
                      f"{len(titles)} judul channel")
                break
        if not titles:
            print("[probe] CGI tidak tersedia, fallback ke RTSP probing")

    reachable = probe_channels_parallel(ip, rtsp_port, user, passwd,max_channels=max_channels, subtype=subtype)
    cameras = []
    # jika CGI berhasil → iterasi semua judul channel
    if titles:
        for ch in sorted(titles.keys()):
            info = reachable.get(ch, {})
            cameras.append({
                "channel": ch,
                "name":    titles[ch],
                "codec":   info.get("codec", ""),
                "width":   info.get("width", 0),
                "height":  info.get("height", 0),
                "rtsp_url": (
                    f"rtsp://{user}:{passwd}@{ip}:{rtsp_port}"
                    f"/cam/realmonitor?channel={ch}&subtype={subtype}"
                ),
            })
    else:
        # murni dari RTSP probing
        for ch in sorted(reachable.keys()):
            info = reachable[ch]
            cameras.append({
                "channel": ch,
                "name":    f"Channel {ch}",
                "codec":   info.get("codec", ""),
                "width":   info.get("width", 0),
                "height":  info.get("height", 0),
                "rtsp_url": (
                    f"rtsp://{user}:{passwd}@{ip}:{rtsp_port}"
                    f"/cam/realmonitor?channel={ch}&subtype={subtype}"
                ),
            })
    return cameras, (titles is not None)
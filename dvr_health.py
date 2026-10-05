# dvr_health.py
"""
Modul untuk cek status DVR (online/offline).
Menggunakan TCP socket check + opsional RTSP probe.
"""
import socket
import time
import logging
import subprocess
from typing import Optional

logger = logging.getLogger("cctv_app")


def check_tcp_port(host: str, port: int, timeout: float = 3.0) -> bool:
    """
    Cek apakah TCP port di host bisa dibuka.
    Return: True jika port terbuka, False jika tidak.
    """
    if not host:
        return False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            result = sock.connect_ex((host, port))
            return result == 0
    except socket.gaierror:
        # DNS resolution failed
        return False
    except socket.timeout:
        return False
    except Exception as e:
        logger.debug(f"[TCP CHECK] {host}:{port} error: {e}")
        return False


def check_dvr_online(dvr: dict, timeout: float = 3.0) -> dict:
    """
    Cek status DVR secara komprehensif.

    Args:
        dvr: dict dengan field 'ip', 'port', 'name', 'id'
        timeout: timeout per pengecekan

    Return:
        dict: {
            'id': dvr_id,
            'online': bool,
            'reason': str,          # alasan status
            'rtsp_ok': bool,        # port 554 terbuka
            'http_ok': bool,        # port 80 terbuka (kalau dicek)
            'response_ms': float,   # waktu respons
        }
    """
    dvr_id = dvr.get("id")
    ip = (dvr.get("ip") or "").strip()
    port = int(dvr.get("port") or 554)

    result = {
        "id":          dvr_id,
        "name":        dvr.get("name"),
        "ip":          ip,
        "port":        port,
        "online":      False,
        "reason":      "",
        "rtsp_ok":     False,
        "http_ok":     False,
        "response_ms": 0,
    }

    if not ip:
        result["reason"] = "IP tidak diisi"
        return result

    # ===== Lapis 1: TCP check ke port RTSP =====
    start = time.time()
    rtsp_ok = check_tcp_port(ip, port, timeout=timeout)
    result["response_ms"] = round((time.time() - start) * 1000, 2)
    result["rtsp_ok"] = rtsp_ok

    if rtsp_ok:
        result["online"] = True
        result["reason"] = "RTSP port terbuka"
        return result

    # ===== Lapis 2: Fallback check ke port HTTP (80) =====
    # Beberapa DVR blokir port 554 tapi tetap bisa diakses via web
    http_ok = check_tcp_port(ip, 80, timeout=timeout)
    result["http_ok"] = http_ok

    if http_ok:
        result["online"] = True
        result["reason"] = "RTSP tertutup, tapi web (port 80) terbuka"
        return result

    result["reason"] = f"Tidak bisa konek ke port {port} atau 80"
    return result


def check_dvr_stream(dvr: dict, channel: int = 1, timeout: int = 10) -> dict:
    """
    Cek lebih dalam: apakah DVR bisa mengirim stream dari channel tertentu.
    Menggunakan ffprobe (harus terinstall di server).

    Return:
        {
            'stream_ok': bool,
            'codec': str,
            'resolution': str,
            'error': str,
        }
    """
    ip = (dvr.get("ip") or "").strip()
    port = int(dvr.get("port") or 554)
    user = dvr.get("user") or "admin"
    password = dvr.get("password") or ""

    if not ip:
        return {"stream_ok": False, "error": "IP kosong"}

    rtsp_url = (
        f"rtsp://{user}:{password}@{ip}:{port}"
        f"/cam/realmonitor?channel={channel}&subtype=1"
    )

    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-rtsp_transport", "tcp",
                "-timeout", str(timeout * 1_000_000),  # microseconds
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=codec_name,width,height",
                "-of", "default=noprint_wrappers=1",
                "-i", rtsp_url,
            ],
            capture_output=True,
            text=True,
            timeout=timeout + 5,
        )

        if proc.returncode == 0 and proc.stdout.strip():
            info = {}
            for line in proc.stdout.strip().splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    info[k.strip()] = v.strip()
            return {
                "stream_ok":  True,
                "codec":      info.get("codec_name", "?"),
                "resolution": f"{info.get('width', '?')}x{info.get('height', '?')}",
                "error":      "",
            }
        else:
            err = proc.stderr.strip().splitlines()[-1][:150] if proc.stderr else "unknown"
            return {"stream_ok": False, "error": err}

    except subprocess.TimeoutExpired:
        return {"stream_ok": False, "error": "ffprobe timeout"}
    except FileNotFoundError:
        return {"stream_ok": False, "error": "ffprobe tidak terinstall"}
    except Exception as e:
        return {"stream_ok": False, "error": str(e)[:150]}
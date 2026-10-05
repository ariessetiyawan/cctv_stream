# hikvision_probe.py
"""
Probe otomatis channel + nama channel dari NVR/DVR Hikvision.
Menggunakan ISAPI InputProxy (paling akurat) dengan fallback
ke probe RTSP brute-force.
"""
import logging
from hikvision_client import HikvisionClient

logger = logging.getLogger("hikvision_probe")


def detect_hikvision_cameras(ip, http_port=80, user="admin", password="",
                             rtsp_port=554, max_channels=32, stream=1,
                             use_isapi=True):
    """
    Deteksi kamera yang terhubung ke NVR/DVR Hikvision.

    Return:
      (cameras: list[dict], source: str)
      - cameras: [{channel, name, online, rtsp_url, ...}]
      - source : "isapi" | "rtsp_probe" | "fallback"
    """
    cameras = []
    source = "fallback"

    # ---------- 1) Coba ISAPI (paling akurat) ----------
    if use_isapi:
        try:
            client = HikvisionClient(ip, http_port, user, password, timeout=10)
            channels = client.get_input_channels()
            logger.info(f"[HIK PROBE] ISAPI mengembalikan {len(channels)} channel")

            for ch in channels:
                ch_id = ch.get("id")
                if ch_id is None:
                    continue
                ch_id = int(ch_id)

                cam = {
                    "channel":   ch_id,
                    "name":      ch.get("name") or f"Channel {ch_id}",
                    "online":    ch.get("online", True),
                    "ip":        ch.get("ipAddress"),
                    "protocol":  ch.get("protocol"),
                    "deviceType": ch.get("deviceType"),
                    "model":     ch.get("model"),
                    "rtsp_url":  client.build_rtsp_url(ch_id, stream=stream),
                    "rtsp_url_sub": client.build_rtsp_url(ch_id, stream=2),
                    "source":    "isapi",
                }
                cameras.append(cam)

            if cameras:
                return cameras, "isapi"
        except Exception as e:
            logger.warning(f"[HIK PROBE] ISAPI gagal: {e}")

    # ---------- 2) Fallback: brute-force RTSP ----------
    logger.info("[HIK PROBE] Fallback ke brute-force RTSP")
    try:
        cameras = _probe_rtsp_channels(
            ip, user, password, rtsp_port, max_channels, stream
        )
        if cameras:
            source = "rtsp_probe"
    except Exception as e:
        logger.warning(f"[HIK PROBE] RTSP probe gagal: {e}")

    return cameras, source


def _probe_rtsp_channels(ip, user, password, rtsp_port, max_channels, stream):
    """Coba RTSP URL satu per satu sampai tidak ada respons."""
    import subprocess

    cameras = []
    consecutive_fail = 0
    MAX_CONSECUTIVE_FAIL = 3   # stop setelah 3 channel berturut-turut gagal

    for ch in range(1, max_channels + 1):
        stream_id = ch * 100 + stream
        rtsp = (
            f"rtsp://{user}:{password}@{ip}:{rtsp_port}"
            f"/Streaming/Channels/{stream_id}"
        )

        ok = _test_rtsp(rtsp)
        if ok:
            consecutive_fail = 0
            cameras.append({
                "channel":   ch,
                "name":      f"Channel {ch}",
                "online":    True,
                "rtsp_url":  rtsp,
                "rtsp_url_sub": (
                    f"rtsp://{user}:{password}@{ip}:{rtsp_port}"
                    f"/Streaming/Channels/{ch * 100 + 2}"
                ),
                "source":    "rtsp_probe",
            })
        else:
            consecutive_fail += 1
            if consecutive_fail >= MAX_CONSECUTIVE_FAIL and ch > 4:
                break

    return cameras


def _test_rtsp(rtsp_url, timeout=6):
    """Tes RTSP dengan ffprobe."""
    import subprocess
    try:
        p = subprocess.run(
            ["ffprobe",
             "-rtsp_transport", "tcp",
             "-timeout", str(timeout * 1_000_000),
             "-v", "error",
             "-select_streams", "v:0",
             "-show_entries", "stream=codec_name",
             "-of", "default=noprint_wrappers=1",
             "-i", rtsp_url],
            capture_output=True, text=True, timeout=timeout + 2,
        )
        return p.returncode == 0 and "codec_name" in (p.stdout or "")
    except Exception:
        return False
# hikvision_client.py
"""
Client untuk NVR/DVR Hikvision via ISAPI (HTTP Digest Auth).
Mendukung: deteksi channel, cek status, build RTSP URL, snapshot.
"""
import re
import logging
import requests
import xml.etree.ElementTree as ET
from requests.auth import HTTPDigestAuth

logger = logging.getLogger("hikvision_client")


class HikvisionClient:
    def __init__(self, host, port=80, username="admin", password="", timeout=10):
        self.host = host
        self.port = int(port or 80)
        self.username = username
        self.password = password
        self.timeout = timeout
        self.base = f"http://{host}:{self.port}"
        self.auth = HTTPDigestAuth(username, password)

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------
    def _get(self, path, **kwargs):
        url = f"{self.base}{path}"
        kwargs.setdefault("auth", self.auth)
        kwargs.setdefault("timeout", self.timeout)
        kwargs.setdefault("headers", {"Accept": "application/xml"})
        return requests.get(url, **kwargs)

    def _put(self, path, data=None, **kwargs):
        url = f"{self.base}{path}"
        kwargs.setdefault("auth", self.auth)
        kwargs.setdefault("timeout", self.timeout)
        return requests.put(url, data=data, **kwargs)

    def _post(self, path, data=None, **kwargs):
        url = f"{self.base}{path}"
        kwargs.setdefault("auth", self.auth)
        kwargs.setdefault("timeout", self.timeout)
        return requests.post(url, data=data, **kwargs)

    # ------------------------------------------------------------------
    # Device info
    # ------------------------------------------------------------------
    def get_device_info(self):
        """Ambil info perangkat: model, serial, firmware."""
        r = self._get("/ISAPI/System/deviceInfo")
        if r.status_code != 200:
            raise RuntimeError(f"deviceInfo HTTP {r.status_code}: {r.text[:200]}")
        root = ET.fromstring(r.content)
        def _t(tag):
            el = root.find(tag)
            return el.text if el is not None else None
        return {
            "deviceName":  _t("deviceName"),
            "model":       _t("model"),
            "serialNumber": _t("serialNumber"),
            "macAddress":  _t("macAddress"),
            "firmwareVersion": _t("firmwareVersion"),
            "firmwareReleasedDate": _t("firmwareReleasedDate"),
        }

    # ------------------------------------------------------------------
    # Channel discovery
    # ------------------------------------------------------------------
    def get_input_channels(self):
        """
        Ambil semua channel input (kamera) via /ISAPI/ContentMgmt/InputProxy/channels.
        Return list of dict: {id, name, online, ip, protocol, ...}
        """
        r = self._get("/ISAPI/ContentMgmt/InputProxy/channels")
        if r.status_code != 200:
            raise RuntimeError(f"InputProxy HTTP {r.status_code}: {r.text[:200]}")

        root = ET.fromstring(r.content)
        channels = []

        # Hikvision bisa pakai namespace berbeda
        for ch in root.iter():
            tag = ch.tag.split("}")[-1]  # buang namespace
            if tag != "InputProxyChannel":
                continue
            info = self._parse_channel_element(ch)
            if info:
                channels.append(info)

        return channels

    def _parse_channel_element(self, el):
        """Parse satu <InputProxyChannel>."""
        def _find(tag):
            # Cari tanpa peduli namespace
            for c in el.iter():
                if c.tag.split("}")[-1] == tag:
                    return c
            return None

        info = {}
        for f in ("id", "name", "online", "ipAddress", "protocol",
                  "streamType", "deviceType", "model"):
            node = _find(f)
            if node is not None and node.text:
                info[f] = node.text.strip()

        if "id" not in info:
            return None

        # Konversi tipe
        if "online" in info:
            info["online"] = info["online"].lower() in ("true", "1", "yes")
        try:
            info["id"] = int(info["id"])
        except ValueError:
            pass

        return info

    def get_streaming_channels(self):
        """
        Ambil semua stream channel via /ISAPI/Streaming/channels.
        Return list of dict: {id, channel, streamType, enabled}
        """
        r = self._get("/ISAPI/Streaming/channels")
        if r.status_code != 200:
            raise RuntimeError(f"Streaming HTTP {r.status_code}: {r.text[:200]}")

        root = ET.fromstring(r.content)
        streams = []
        for ch in root.iter():
            if ch.tag.split("}")[-1] != "StreamingChannel":
                continue
            info = self._parse_stream_element(ch)
            if info:
                streams.append(info)
        return streams

    def _parse_stream_element(self, el):
        def _find(tag):
            for c in el.iter():
                if c.tag.split("}")[-1] == tag:
                    return c
            return None

        info = {}
        for f in ("id", "channelID", "streamType", "enabled"):
            node = _find(f)
            if node is not None and node.text:
                info[f] = node.text.strip()
        if "enabled" in info:
            info["enabled"] = info["enabled"].lower() in ("true", "1", "yes")
        return info if info else None

    # ------------------------------------------------------------------
    # RTSP URL builder
    # ------------------------------------------------------------------
    def build_rtsp_url(self, channel_id, stream=1):
        """
        Bangun URL RTSP Hikvision.

        channel_id: nomor channel fisik (1, 2, 3, ...)
        stream    : 1 = main stream, 2 = sub-stream, 3 = third stream

        Contoh: channel 17, main ? /Streaming/Channels/1701
                channel 1,  sub  ? /Streaming/Channels/102
        """
        channel_id = int(channel_id)
        stream = int(stream)
        # Rumus: channel * 100 + stream
        stream_id = channel_id * 100 + stream
        user = self.username
        pwd = self.password
        return (
            f"rtsp://{user}:{pwd}@{self.host}:554"
            f"/Streaming/Channels/{stream_id}"
        )

    @staticmethod
    def build_rtsp_url_static(host, username, password, channel_id, stream=1, port=554):
        """Versi statik tanpa instance client."""
        stream_id = int(channel_id) * 100 + int(stream)
        return (
            f"rtsp://{username}:{password}@{host}:{port}"
            f"/Streaming/Channels/{stream_id}"
        )

    # ------------------------------------------------------------------
    # Snapshot (untuk validasi cepat)
    # ------------------------------------------------------------------
    def get_snapshot(self, channel_id, timeout=8):
        """Ambil JPEG snapshot dari channel tertentu."""
        url = f"{self.base}/ISAPI/Streaming/channels/{int(channel_id)}01/picture"
        r = self._get(url, timeout=timeout)
        if r.status_code != 200:
            return None
        if r.headers.get("Content-Type", "").startswith("image/"):
            return r.content
        return None

    # ------------------------------------------------------------------
    # Cek online (ringan)
    # ------------------------------------------------------------------
    def ping(self, timeout=3):
        try:
            r = self._get("/ISAPI/System/status", timeout=timeout)
            return r.status_code == 200
        except Exception:
            return False
# dahua_client.py
import logging
import re
import requests
from requests.auth import HTTPDigestAuth

logger = logging.getLogger(__name__)


class DahuaClient:
    """Client untuk DVR/NVR Dahua via CGI API (HTTP Digest Auth)."""

    def __init__(self, host: str, port: int, username: str, password: str, timeout: int = 10):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.timeout = timeout
        self.base_url = f"http://{host}:{port}/cgi-bin"

    def _get(self, endpoint: str, params: dict) -> str:
        url = f"{self.base_url}/{endpoint}"
        logger.debug(f"[Dahua] GET {url} params={params}")
        try:
            resp = requests.get(
                url,
                params=params,
                auth=HTTPDigestAuth(self.username, self.password),
                timeout=self.timeout,
            )
            logger.debug(f"[Dahua] Response {resp.status_code}")
            resp.raise_for_status()
            return resp.text
        except requests.exceptions.RequestException as e:
            logger.error(f"[Dahua] Request gagal: {e}")
            raise

    def test_connection(self) -> bool:
        try:
            self._get("configManager.cgi", {"action": "getConfig", "name": "ChannelTitle"})
            return True
        except Exception as e:
            logger.error(f"Koneksi ke DVR gagal: {e}")
            return False

    def get_channel_count(self) -> int:
        try:
            text = self._get("configManager.cgi", {"action": "getConfig", "name": "Encode"})
            indices = set()
            for line in text.splitlines():
                m = re.match(r"table\.Encode\[(\d+)\]\.", line)
                if m:
                    indices.add(int(m.group(1)))
            return (max(indices) + 1) if indices else 0
        except Exception as e:
            logger.error(f"Gagal ambil jumlah channel: {e}")
            return 0

    def get_channels(self) -> list:
        channels = []
        try:
            text = self._get("configManager.cgi", {"action": "getConfig", "name": "ChannelTitle"})
            for line in text.splitlines():
                m = re.match(r"table\.ChannelTitle\[(\d+)\]\.Name=(.*)", line)
                if m:
                    idx = int(m.group(1))
                    name = m.group(2).strip()
                    channels.append({
                        "id": idx + 1,
                        "index": idx,
                        "name": name or f"Channel {idx + 1}",
                        "rtsp_url": self.build_rtsp_url(idx + 1),
                        "snapshot_url": self.build_snapshot_url(idx + 1),
                    })
        except Exception as e:
            logger.error(f"Gagal ambil channel titles: {e}")

        if not channels:
            count = self.get_channel_count()
            for i in range(count):
                channels.append({
                    "id": i + 1,
                    "index": i,
                    "name": f"Channel {i + 1}",
                    "rtsp_url": self.build_rtsp_url(i + 1),
                    "snapshot_url": self.build_snapshot_url(i + 1),
                })
        return channels

    def build_rtsp_url(self, channel: int, subtype: int = 0) -> str:
        return (
            f"rtsp://{self.username}:{self.password}@"
            f"{self.host}:554/cam/realmonitor?channel={channel}&subtype={subtype}"
        )

    def build_snapshot_url(self, channel: int) -> str:
        return (
            f"http://{self.host}:{self.port}/cgi-bin/snapshot.cgi"
            f"?channel={channel}"
        )
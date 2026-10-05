# hikvision_playback.py
"""
Playback rekaman Hikvision via ISAPI ContentMgmt/search.
Hikvision tidak punya loadfile.cgi seperti Dahua, tapi punya:
  - POST /ISAPI/ContentMgmt/search  ? cari rekaman (XML)
  - RTSP playback: /Streaming/tracks/<trackID>?starttime=...&endtime=...
"""
import logging
from datetime import datetime
import xml.etree.ElementTree as ET
import requests
from requests.auth import HTTPDigestAuth

logger = logging.getLogger("hikvision_playback")


class HikvisionPlayback:
    def __init__(self, host, port=80, username="admin", password="", timeout=15):
        self.host = host
        self.port = int(port or 80)
        self.username = username
        self.password = password
        self.timeout = timeout
        self.base = f"http://{host}:{self.port}"
        self.auth = HTTPDigestAuth(username, password)

    # ------------------------------------------------------------------
    # Search rekaman
    # ------------------------------------------------------------------
    def search_recordings(self, channel, start_dt, end_dt, max_results=100):
        """
        Cari rekaman via /ISAPI/ContentMgmt/search.
        Return list of dict: {start, end, playback_uri, ...}
        """
        track_id = int(channel) * 100 + 1  # track 101 = channel 1 main

        xml_body = f"""<?xml version="1.0" encoding="UTF-8"?>
<CMSearchDescription>
  <searchID>1</searchID>
  <trackList>
    <trackID>{track_id}</trackID>
  </trackList>
  <timeSpanList>
    <timeSpan>
      <startTime>{start_dt.strftime('%Y-%m-%dT%H:%M:%SZ')}</startTime>
      <endTime>{end_dt.strftime('%Y-%m-%dT%H:%M:%SZ')}</endTime>
    </timeSpan>
  </timeSpanList>
  <maxResults>{max_results}</maxResults>
  <searchResultPostion>0</searchResultPostion>
  <metadataList>
    <metadataDescriptor>//recordType.meta.std-cgi.com</metadataDescriptor>
  </metadataList>
</CMSearchDescription>
"""
        url = f"{self.base}/ISAPI/ContentMgmt/search"
        headers = {"Content-Type": "application/xml"}

        try:
            r = requests.post(
                url, data=xml_body.encode("utf-8"),
                auth=self.auth, headers=headers, timeout=self.timeout,
            )
        except requests.exceptions.RequestException as e:
            logger.warning(f"[HIK PB] Search error: {e}")
            return []

        if r.status_code != 200:
            logger.warning(f"[HIK PB] Search HTTP {r.status_code}: {r.text[:200]}")
            return []

        return self._parse_search_result(r.content)

    def _parse_search_result(self, xml_bytes):
        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError as e:
            logger.warning(f"[HIK PB] Parse error: {e}")
            return []

        items = []
        for match in root.iter():
            if match.tag.split("}")[-1] != "searchMatchItem":
                continue
            item = {}
            for child in match.iter():
                tag = child.tag.split("}")[-1]
                if child.text:
                    item[tag] = child.text.strip()
            if item:
                items.append(item)
        return items

    # ------------------------------------------------------------------
    # Build RTSP playback URL
    # ------------------------------------------------------------------
    def build_playback_rtsp(self, channel, start_dt, end_dt, stream=1):
        """
        URL RTSP playback Hikvision:
        rtsp://user:pass@host:554/Streaming/tracks/<trackID>?starttime=...&endtime=...
        trackID = channel * 100 + stream
        """
        track_id = int(channel) * 100 + int(stream)
        start = start_dt.strftime("%Y%m%dT%H%M%SZ")
        end   = end_dt.strftime("%Y%m%dT%H%M%SZ")
        return (
            f"rtsp://{self.username}:{self.password}@{self.host}:554"
            f"/Streaming/tracks/{track_id}"
            f"?starttime={start}&endtime={end}"
        )

    # ------------------------------------------------------------------
    # Cek ketersediaan rekaman per jam
    # ------------------------------------------------------------------
    def check_hourly_availability(self, channel, date_str):
        """
        Cek jam berapa saja yang ada rekaman pada tanggal tertentu.
        Return dict {hour: True/False}
        """
        from datetime import datetime, timedelta
        result = {}
        base = datetime.strptime(date_str, "%Y-%m-%d")

        for h in range(24):
            start = base.replace(hour=h, minute=0, second=0)
            end = start + timedelta(hours=1)
            items = self.search_recordings(channel, start, end, max_results=5)
            result[h] = len(items) > 0

        return result
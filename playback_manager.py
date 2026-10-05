"""
Manajer sesi playback NVR Dahua.
Spawn ffmpeg per-sesi untuk mengubah .DAV (loadfile.cgi) menjadi HLS
yang bisa diputar HLS.js.
"""
import os
import time
import uuid
import shutil
import tempfile
import subprocess
import threading
import logging
from datetime import datetime, timedelta
from requests.auth import HTTPDigestAuth

logger = logging.getLogger("cctv_app")

# Batas sesi playback serentak (hindari overload CPU/DVR)
MAX_SESSIONS = 4
SESSION_IDLE_TIMEOUT = 300        # 5 menit nganggur ? auto stop
PLAYBACK_ROOT = os.path.join(tempfile.gettempdir(), "cctv_playback")
os.makedirs(PLAYBACK_ROOT, exist_ok=True)


class PlaybackSession:
    def __init__(self, sid, camera_id, dvr_url, start_dt, end_dt):
        self.sid        = sid
        self.camera_id  = camera_id
        self.dvr_url    = dvr_url
        self.start_dt   = start_dt
        self.end_dt     = end_dt
        self.dir        = os.path.join(PLAYBACK_ROOT, sid)
        self.proc       = None
        self.created_at = time.time()
        self.last_seen  = time.time()
        self.finished   = False
        os.makedirs(self.dir, exist_ok=True)

    @property
    def playlist(self):
        return os.path.join(self.dir, "index.m3u8")

    def start(self):
        """Jalankan ffmpeg: tarik DAV dari DVR ? tulis HLS ke self.dir."""
        # Catatan: Dahua pakai HTTP Digest ? ffmpeg mendukung lewat user:pass@host
        # '-stimeout' cuma untuk RTSP; untuk HTTP gunakan '-rw_timeout'
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "warning",
            "-rw_timeout", "15000000",
            "-rtsp_transport tcp",
            "-i", self.dvr_url,
            
            # ---- Video: HEVC ? H.264 ----
            "-c:v", "libx264",
            "-preset", "ultrafast",     # paling cepat, CPU paling ringan
            "-tune", "zerolatency",
            "-crf", "26",               # 23=high quality, 28=kecil; 26 seimbang
            "-pix_fmt", "yuv420p",      # wajib untuk kompat browser
            "-g", "50",                 # keyframe tiap 2 detik (25fpsx2)
            "-sc_threshold", "0",       # tidak ada scene cut adaptif (biar stabil)

            # ---- Audio ----
            "-c:a", "aac",
            "-b:a", "64k",
            "-ac", "1",                 # mono, cukup untuk CCTV

            # ---- HLS ----
            "-f", "hls",
            "-hls_time", "2",
            "-hls_list_size", "8",
            "-hls_flags", "delete_segments+append_list+omit_endlist",
            "-hls_segment_type", "mpegts",
            "-hls_segment_filename",
                os.path.join(self.dir, "seg_%05d.ts"),
            self.playlist,
        ]    
        logger.info(f"[PLAYBACK {self.sid}] ffmpeg start: {self.dvr_url[:80]}...")
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            bufsize=1,
            universal_newlines=True,
        )
        # Thread pembaca stderr ? log
        t = threading.Thread(target=self._drain_stderr, daemon=True)
        t.start()

    def _drain_stderr(self):
        try:
            for line in self.proc.stderr:
                line = line.rstrip()
                if line:
                    logger.debug(f"[PLAYBACK {self.sid}] {line}")
        except Exception:
            pass

    def is_alive(self):
        return self.proc is not None and self.proc.poll() is None

    def touch(self):
        self.last_seen = time.time()

    def stop(self):
        # Kill ffmpeg
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            except Exception:
                pass
        # Hapus direktori
        try:
            shutil.rmtree(self.dir, ignore_errors=True)
        except Exception:
            pass


class PlaybackManager:
    def __init__(self):
        self.sessions = {}
        self.lock = threading.Lock()
        # Thread pembersih
        t = threading.Thread(target=self._janitor, daemon=True)
        t.start()

    def _janitor(self):
        while True:
            time.sleep(30)
            now = time.time()
            with self.lock:
                dead = []
                for sid, s in self.sessions.items():
                    if not s.is_alive() and s.finished:
                        dead.append(sid)
                    elif now - s.last_seen > SESSION_IDLE_TIMEOUT:
                        s.finished = True
                        dead.append(sid)
                for sid in dead:
                    s = self.sessions.pop(sid, None)
                    if s:
                        s.stop()
                        logger.info(f"[PLAYBACK] session {sid} dihapus (idle/dead)")

    def create(self, camera_id, dvr_url, start_dt, end_dt):
        with self.lock:
            # Batasi jumlah sesi
            alive = [s for s in self.sessions.values() if s.is_alive()]
            if len(alive) >= MAX_SESSIONS:
                # Matikan yang paling lama idle
                oldest = min(alive, key=lambda x: x.last_seen)
                oldest.finished = True
                self.sessions.pop(oldest.sid, None)
                oldest.stop()

            sid = uuid.uuid4().hex[:12]
            s = PlaybackSession(sid, camera_id, dvr_url, start_dt, end_dt)
            s.start()
            self.sessions[sid] = s
            return s

    def get(self, sid):
        with self.lock:
            return self.sessions.get(sid)

    def stop(self, sid):
        with self.lock:
            s = self.sessions.pop(sid, None)
        if s:
            s.stop()


# Singleton
manager = PlaybackManager()
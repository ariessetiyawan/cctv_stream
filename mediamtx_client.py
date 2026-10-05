"""
Client untuk MediaMTX Control API v3.

Endpoint yang dipakai:
  POST   /v3/config/paths/add/{name}   → tambah path runtime
  PATCH  /v3/config/paths/patch/{name} → update path runtime
  DELETE /v3/config/paths/delete/{name}→ hapus path runtime
  GET    /v3/config/paths/list         → list semua path
  GET    /v3/config/paths/get/{name}   → detail satu path
"""
import re
import logging
import requests
from urllib.parse import quote
from config import Config

log = logging.getLogger(__name__)

API_BASE = Config.MEDIAMTX_API.rstrip("/")
TIMEOUT  = 5  # detik


# ====================================================================
# Helpers
# ====================================================================
def _url(path: str) -> str:
    return f"{API_BASE}{path}"


def slugify(text: str) -> str:
    """Ubah nama kamera jadi slug aman untuk nama path MediaMTX."""
    text = (text or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-") or "cam"


def is_alive() -> bool:
    """Cek MediaMTX hidup atau tidak."""
    try:
        r = requests.get(_url("/v3/config/global/get"), timeout=2)
        return r.status_code == 200
    except Exception:
        return False


# ====================================================================
# Bangun URL RTSP Dahua
# ====================================================================
def build_dahua_rtsp(channel: int, subtype: int = None,
                     host: str = None, port: int = None,
                     user: str = None, password: str = None) -> str:
    """Format: rtsp://user:pass@IP:554/cam/realmonitor?channel=N&subtype=S"""
    host     = host     or Config.DAHUA_HOST
    port     = port     or Config.DAHUA_PORT
    user     = user     or Config.DAHUA_USER
    password = password or Config.DAHUA_PASSWORD
    subtype  = Config.DAHUA_SUBTYPE if subtype is None else subtype
    return (
        f"rtsp://{user}:{password}@{host}:{port}"
        f"/cam/realmonitor?channel={channel}&subtype={subtype}"
    )


# ====================================================================
# CRUD Path MediaMTX
# ====================================================================
def add_path(name: str, rtsp_url: str, on_demand: bool = True) -> tuple[bool, str]:
    """
    Tambah path baru.
    - on_demand=True  → MediaMTX baru connect ke DVR saat ada viewer (hemat bandwidth).
    - on_demand=False → MediaMTX langsung connect 24/7.

    Return (ok, message).
    """
    payload = {
        "source": rtsp_url,
        "sourceOnDemand": on_demand,
        # --- opsi tuning (opsional) ---
        "sourceOnDemandCloseAfter": "60s",
        # Gunakan TCP supaya stabil lewat WAN/NAT
        "rtspTransport": "tcp",
    }

    try:
        r = requests.post(
            _url(f"/v3/config/paths/add/{quote(name)}"),
            json=payload,
            timeout=TIMEOUT,
        )
    except requests.exceptions.ConnectionError:
        return False, "MediaMTX tidak dapat dihubungi"
    except requests.exceptions.Timeout:
        return False, "MediaMTX timeout"

    if r.status_code in (200, 201):
        return True, "Path ditambahkan ke MediaMTX"
    if r.status_code == 400 and "already exists" in r.text.lower():
        # Sudah ada → coba patch saja
        return patch_path(name, rtsp_url, on_demand)
    return False, f"MediaMTX error {r.status_code}: {r.text}"


def patch_path(name: str, rtsp_url: str, on_demand: bool = True) -> tuple[bool, str]:
    """Update path yang sudah ada."""
    payload = {
        "source": rtsp_url,
        "sourceOnDemand": on_demand,
        "sourceOnDemandCloseAfter": "60s",
        "rtspTransport": "tcp",
    }
    try:
        r = requests.patch(
            _url(f"/v3/config/paths/patch/{quote(name)}"),
            json=payload,
            timeout=TIMEOUT,
        )
    except Exception as e:
        return False, f"MediaMTX error: {e}"

    if r.status_code == 200:
        return True, "Path diperbarui"
    return False, f"MediaMTX error {r.status_code}: {r.text}"


def delete_path(name: str) -> tuple[bool, str]:
    """Hapus path dari MediaMTX."""
    try:
        r = requests.delete(
            _url(f"/v3/config/paths/delete/{quote(name)}"),
            timeout=TIMEOUT,
        )
    except Exception as e:
        return False, f"MediaMTX error: {e}"

    if r.status_code in (200, 404):   # 404 = memang sudah tidak ada
        return True, "Path dihapus"
    return False, f"MediaMTX error {r.status_code}: {r.text}"


def list_paths() -> list[dict]:
    """List semua path yang terdaftar di MediaMTX."""
    try:
        r = requests.get(_url("/v3/config/paths/list"), timeout=TIMEOUT)
        if r.status_code == 200:
            return r.json().get("items", [])
    except Exception:
        pass
    return []


def get_path(name: str) -> dict | None:
    try:
        r = requests.get(_url(f"/v3/config/paths/get/{quote(name)}"), timeout=TIMEOUT)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


# ====================================================================
# URL HLS untuk viewer
# ====================================================================
def hls_url(name: str) -> str:
    """URL HLS publik untuk viewer."""
    return f"{Config.MEDIAMTX_HLS.rstrip('/')}/{name}/index.m3u8"
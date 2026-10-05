# app.py
from flask import (
    Flask, render_template, request, jsonify, session, send_file,
    redirect, url_for, flash, stream_with_context, abort, Response
)
from functools import wraps
from datetime import datetime, timedelta
import os, re, json, logging
import time, requests, sys
from config import Config
import db as dbmod
from dotenv import load_dotenv
import pymysql
import pymysql.err
import mediamtx_client as mtx
import requests as _requests
from urllib.parse import urlparse
from dahua_client import DahuaClient
import dvr_probe
import dvr_health
import threading
import base64
import hmac as _hmac
import hashlib
import tempfile
import subprocess
import os as _os
from logger_config import setup_logger, setup_crash_handler
import time as _time
from flask_cors import CORS
from datetime import datetime, timedelta
from requests.auth import HTTPDigestAuth
from urllib.parse import quote as _url_quote
from playback_manager import manager as pb_manager

# === HIKVISION ===
from hikvision_client import HikvisionClient
import hikvision_probe
import hikvision_playback
# === END HIKVISION ===


# ====================================================================
# DVR HEALTH MONITOR (background thread)
# ====================================================================
DVR_STATUS_CACHE = {}          # { dvr_id: {...} }
DVR_STATUS_LOCK  = threading.Lock()
DVR_CHECK_INTERVAL = 60        # detik antar pengecekan
DVR_STREAM_CHECK_EVERY = 10    # cek stream mendalam tiap 10 putaran (~10 menit)

logger = setup_logger("cctv_app", level=logging.INFO)
setup_crash_handler(logger)

load_dotenv()

# Token util (opsional kalau masih dipakai untuk HLS)
try:
    from token_util import generate_token, verify_token
except Exception:
    generate_token = verify_token = None


app = Flask(__name__)
CORS(app,
     origins="*",
     allow_headers=["Content-Type", "Authorization"],
     methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])

app.config.from_object(Config)
app.secret_key = Config.SECRET_KEY

_APP_START_TIME = _time.time()

# ====================================================================
# Online users tracker (in-memory, per session)
# ====================================================================
ONLINE_USERS = {}          # { session_key: {"username":..., "role":..., "last_seen": ts} }
ONLINE_TIMEOUT = 300       # 5 menit dianggap idle → keluar dari daftar

# Path penyimpanan konfigurasi kamera
CAMERAS_FILE = "cameras.json"


def _resource_monitor():
    """Catat penggunaan RAM/CPU server setiap 60 detik."""
    try:
        import psutil
    except ImportError:
        logger.warning("[MONITOR] psutil tidak terinstall, skip resource monitor")
        return

    proc = psutil.Process(os.getpid())
    while True:
        try:
            mem_mb = proc.memory_info().rss / (1024 * 1024)
            cpu_pct = proc.cpu_percent(interval=1)
            total_mem = psutil.virtual_memory()
            logger.info(
                f"[RESOURCE] PID={os.getpid()} "
                f"RAM={mem_mb:.1f}MB "
                f"CPU={cpu_pct:.1f}% "
                f"SystemRAM={total_mem.percent}% "
                f"Threads={proc.num_threads()}"
            )
        except Exception as e:
            logger.warning(f"[RESOURCE] Monitor error: {e}")
        time.sleep(60)


def load_saved_cameras() -> list:
    if os.path.exists(CAMERAS_FILE):
        with open(CAMERAS_FILE, "r") as f:
            return json.load(f)
    return []


def save_cameras(cameras: list):
    with open(CAMERAS_FILE, "w") as f:
        json.dump(cameras, f, indent=2)


def build_dahua_rtsp(channel, subtype=1):
    return (
        f"rtsp://{Config.DAHUA_USER}:{Config.DAHUA_PASSWORD}"
        f"@{Config.DAHUA_HOST}:{Config.DAHUA_PORT}"
        f"/cam/realmonitor?channel={channel}&subtype={subtype}"
    )


# === HIKVISION ===
def build_hikvision_rtsp(channel, stream=1,
                          host=None, user=None, password=None, port=554):
    """
    Bangun URL RTSP Hikvision.
    stream: 1 = main, 2 = sub-stream, 3 = third stream
    """
    host = host or getattr(Config, "HIKVISION_HOST", "")
    user = user or getattr(Config, "HIKVISION_USER", "admin")
    password = password if password is not None else getattr(Config, "HIKVISION_PASSWORD", "")
    stream_id = int(channel) * 100 + int(stream)
    return (
        f"rtsp://{user}:{password}@{host}:{port}"
        f"/Streaming/Channels/{stream_id}"
    )


def _dvr_vendor(dvr_row):
    """Ambil vendor DVR dari DB row. Default 'dahua'."""
    if not dvr_row:
        return "dahua"
    v = (dvr_row.get("vendor") or "").strip().lower()
    return v if v else "dahua"


def _build_rtsp_for_camera(cam, dvr_row=None):
    """
    Bangun RTSP URL berdasarkan vendor DVR.
    Dipakai untuk camera yang field rtsp_url-nya kosong.
    """
    rtsp = (cam.get("rtsp_url") or "").strip()
    if rtsp:
        return rtsp

    channel = cam.get("channel") or 1
    subtype = 1

    if dvr_row:
        vendor = _dvr_vendor(dvr_row)
        ip   = dvr_row.get("ip")
        user = dvr_row.get("user") or ""
        pwd  = dvr_row.get("password") or ""

        if vendor == "hikvision":
            # Hikvision selalu pakai port RTSP 554
            if not user:
                user = getattr(Config, "HIKVISION_USER", "admin")
                pwd  = getattr(Config, "HIKVISION_PASSWORD", "")
            return build_hikvision_rtsp(
                channel=channel, stream=subtype,
                host=ip, user=user, password=pwd, port=554,
            )

    # Default: Dahua
    return build_dahua_rtsp(channel=channel, subtype=subtype)


def _guess_vendor(ip, http_port, user, password, timeout=5):
    """Tebak vendor DVR: coba ISAPI Hikvision dulu, kalau gagal → dahua."""
    try:
        client = HikvisionClient(ip, http_port, user, password, timeout=timeout)
        info = client.get_device_info()
        if info.get("model") or info.get("serialNumber"):
            logger.info(f"[GUESS] Vendor=Hikvision (model={info.get('model')})")
            return "hikvision"
    except Exception as e:
        logger.debug(f"[GUESS] Hikvision probe gagal: {e}")
    return "dahua"
# === END HIKVISION ===


def _session_key():
    """Kunci unik per user-session."""
    return session.get("user_id") or session.get("user") or "anon"


def touch_online_user():
    """Panggil di tiap request halaman ber-login untuk refresh last_seen."""
    if not session.get("logged_in"):
        return
    ONLINE_USERS[_session_key()] = {
        "username":  session.get("user", "Anonim"),
        "role":      session.get("role", "public"),
        "last_seen": time.time(),
    }


def _cleanup_online_users():
    now = time.time()
    for k in [k for k, v in ONLINE_USERS.items()
              if now - v["last_seen"] > ONLINE_TIMEOUT]:
        ONLINE_USERS.pop(k, None)


def get_online_users():
    _cleanup_online_users()
    return list(ONLINE_USERS.values())


# ====================================================================
# Auth helpers
# ====================================================================
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("landing"))
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("landing"))
        if session.get("role") != "admin":
            if request.path.startswith("/api/"):
                return jsonify({
                    "success": False,
                    "message": "Akses ditolak — hanya admin",
                }), 403
            flash("Hanya admin yang dapat mengakses halaman ini", "error")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return wrapper


def supervisor_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("landing"))
        if session.get("role") not in ("admin", "supervisor"):
            if request.path.startswith("/api/"):
                return jsonify({"success": False, "message": "Akses ditolak"}), 403
            flash("Hanya admin / supervisor yang dapat mengakses halaman ini", "error")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return wrapper


# ====================================================================
# SIKAWAN SSO
# ====================================================================
def _verify_sikawan(username: str, password: str):
    if not getattr(Config, "SIKAWAN_ENABLED", False):
        return False

    payload = {"username": username, "password": password}
    url     = Config.SIKAWAN_API_URL
    timeout = getattr(Config, "SIKAWAN_TIMEOUT", 10)

    try:
        r = _requests.post(
            url, json=payload, timeout=timeout,
            headers={"Accept": "application/json"},
        )
    except _requests.exceptions.Timeout:
        print(f"[SIKAWAN] Timeout {timeout}s connecting to {url}")
        return False
    except _requests.exceptions.ConnectionError as e:
        print(f"[SIKAWAN] Connection error: {e}")
        return False
    except Exception as e:
        print(f"[SIKAWAN] Unexpected error: {e}")
        return False

    print(f"[SIKAWAN] POST {url} → HTTP {r.status_code}")
    try:
        raw = r.json()
        print(f"[SIKAWAN] Response: {raw}")
    except Exception:
        print(f"[SIKAWAN] Non-JSON response: {r.text[:200]}")
        return False

    is_ok = (
        raw.get("success") is True
        or raw.get("status") in ("success", "ok", "OK", True)
        or raw.get("code") in (200, "200")
        or bool(raw.get("token"))
        or bool(raw.get("access_token"))
        or bool(raw.get("data"))
    )
    if not is_ok:
        return None

    user_data = (
        raw.get("data") if isinstance(raw.get("data"), dict) else None
        or raw.get("user") if isinstance(raw.get("user"), dict) else None
        or raw.get("data", {}).get("user") if isinstance(raw.get("data"), dict) else None
        or raw
    )

    return {
        "username": user_data.get("username")
                    or user_data.get("user_name")
                    or user_data.get("email")
                    or username,
        "full_name": user_data.get("name")
                     or user_data.get("full_name")
                     or user_data.get("nama")
                     or username,
        "email":     user_data.get("email") or "",
        "role_sikawan": user_data.get("role") or user_data.get("role_name") or "",
        "raw":       user_data,
    }


def _map_sikawan_role(sikawan_role: str, username: str) -> str:
    u_lower = (username or "").lower()
    r_lower = (sikawan_role or "").lower()
    admin_list = getattr(Config, "SIKAWAN_ADMIN_USERS", [])
    if u_lower in admin_list:
        return "admin"
    if "admin" in r_lower or "administrator" in r_lower:
        return "admin"
    return getattr(Config, "SIKAWAN_DEFAULT_ROLE", "public")


def _do_login(username: str, password: str):
    username = (username or "").strip()
    password = password or ""

    if not username or not password:
        return False, None

    if getattr(Config, "SIKAWAN_ENABLED", False):
        sikawan_result = _verify_sikawan(username, password)

        if sikawan_result is False:
            print("[LOGIN] SIKAWAN tidak dapat dihubungi")
            if not getattr(Config, "SIKAWAN_FALLBACK_LOCAL", True):
                return False, None
        elif sikawan_result is None:
            print(f"[LOGIN] SIKAWAN menolak kredensial untuk '{username}'")
            if not getattr(Config, "SIKAWAN_FALLBACK_LOCAL", True):
                return False, None
        else:
            sikawan_username = (sikawan_ok := sikawan_result).get("username", username)
            local_user = None
            if Config.DB_ENABLED:
                try:
                    local_user = dbmod.get_user_by_username(sikawan_username)
                except Exception as e:
                    print(f"[LOGIN] DB error saat lookup user: {e}")

            if local_user:
                print(
                    f"[LOGIN] User SIKAWAN '{sikawan_username}' sudah terdaftar "
                    f"di DB lokal dengan role='{local_user.get('role')}'"
                )
                return True, local_user

            if getattr(Config, "SIKAWAN_AUTO_CREATE", True) and Config.DB_ENABLED:
                try:
                    new_id = dbmod.create_user(
                        username=sikawan_username,
                        raw_password=password,
                        role="public",
                    )
                    print(
                        f"[LOGIN] Auto-create user SIKAWAN '{sikawan_username}' "
                        f"→ role='public' (default)"
                    )
                    return True, {
                        "id":        new_id,
                        "username":  sikawan_username,
                        "role":      "public",
                        "full_name": sikawan_ok.get("full_name", sikawan_username),
                        "email":     sikawan_ok.get("email", ""),
                    }
                except Exception as e:
                    print(f"[LOGIN] Gagal auto-create user SIKAWAN: {e}")
                    return True, {
                        "id":        None,
                        "username":  sikawan_username,
                        "role":      "public",
                        "full_name": sikawan_ok.get("full_name", sikawan_username),
                    }

            print(
                f"[LOGIN] User '{sikawan_username}' belum terdaftar di DB lokal "
                f"& SIKAWAN_AUTO_CREATE dimatikan → login ditolak"
            )
            return False, None

    if not Config.DB_ENABLED:
        if username == "admin" and password == "admin123":
            return True, {"username": "admin", "role": "admin", "id": 0}
        return False, None

    try:
        user = dbmod.verify_user(username, password)
    except Exception as e:
        print(f"[LOGIN] DB error: {e}")
        return False, None

    return (user is not None), user


def _set_session(user):
    session["logged_in"] = True
    session["user"]      = user["username"]
    session["role"]      = user.get("role", "admin")
    session["user_id"]   = user.get("id")


def _validate_dvr_name(dvr_name):
    if not dvr_name:
        return True
    try:
        names = {d["name"] for d in dbmod.fetch_all_dvr()}
        return dvr_name in names
    except Exception:
        return False


# ====================================================================
# Pages
# ====================================================================
@app.route("/health")
def health():
    return jsonify({
        "status":    "ok",
        "timestamp": datetime.now().isoformat(),
        "pid":       os.getpid(),
        "uptime":    _time.time() - _APP_START_TIME,
    })


@app.before_request
def _log_request_start():
    request._start_time = _time.time()


@app.after_request
def _log_request_end(response):
    if request.path.startswith("/static/") or request.path.startswith("/stream/"):
        return response
    duration = _time.time() - getattr(request, "_start_time", _time.time())
    logger.info(
        f"{request.method} {request.path} "
        f"→ {response.status_code} "
        f"({duration*1000:.0f}ms) "
        f"[{request.remote_addr}] "
        f"user={session.get('user', '-')}"
    )
    return response


@app.errorhandler(Exception)
def _handle_all_exceptions(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    logger.error(
        f"Unhandled exception di {request.method} {request.path}: {e}",
        exc_info=True,
    )
    return jsonify({
        "success": False,
        "message": "Internal server error",
        "error": str(e),
    }), 500


# ====================================================================
# BULK DETECT + ADD CAMERA (Dahua + Hikvision)
# ====================================================================
@app.route("/api/dvr/probe", methods=["POST"])
@admin_required
def api_dvr_probe():
    """
    Deteksi otomatis channel + nama channel dari DVR.
    Mendukung vendor: dahua (default), hikvision.
    """
    data         = request.get_json(silent=True) or {}
    ip           = (data.get("ip") or "").strip()
    rtsp_port    = int(data.get("port") or 554)
    http_port    = int(data.get("http_port") or 80)
    user         = (data.get("user") or "").strip()
    passwd       = data.get("password") or ""
    max_channels = min(int(data.get("max_channels") or 32), 64)
    subtype      = int(data.get("subtype") or 1)
    use_cgi      = bool(data.get("use_cgi", True))
    vendor       = (data.get("vendor") or "").strip().lower()

    if not ip:
        return jsonify({"success": False, "message": "IP DVR wajib diisi"}), 400
    if not user:
        return jsonify({"success": False, "message": "Username DVR wajib diisi"}), 400

    # === HIKVISION: auto-detect vendor ===
    if not vendor:
        vendor = _guess_vendor(ip, http_port, user, passwd)
    # === END HIKVISION ===

    try:
        if vendor == "hikvision":
            # === HIKVISION ===
            cameras, source = hikvision_probe.detect_hikvision_cameras(
                ip=ip, http_port=http_port, user=user, password=passwd,
                rtsp_port=rtsp_port, max_channels=max_channels,
                stream=subtype, use_isapi=use_cgi,
            )
            return jsonify({
                "success":        True,
                "vendor":         "hikvision",
                "detected_count": len(cameras),
                "source":         source,
                "cameras":        cameras,
            })
            # === END HIKVISION ===
        else:
            cameras, cgi_ok = dvr_probe.detect_dvr_cameras(
                ip=ip, rtsp_port=rtsp_port, user=user, passwd=passwd,
                max_channels=max_channels, subtype=subtype, use_cgi=use_cgi,
            )
            return jsonify({
                "success":        True,
                "vendor":         "dahua",
                "detected_count": len(cameras),
                "cgi_titles":     cgi_ok,
                "cameras":        cameras,
            })
    except Exception as e:
        logger.error(f"[PROBE] {vendor} gagal: {e}", exc_info=True)
        return jsonify({"success": False, "message": f"Probe gagal: {e}"}), 500


@app.route("/api/camera/bulk-adds", methods=["POST"])
@admin_required
def api_camera_bulk_add1():
    """Tambah banyak kamera sekaligus (legacy)."""
    data     = request.get_json(silent=True) or {}
    dvr_name = (data.get("dvr_name") or "").strip() or None
    location = (data.get("location") or "").strip() or None
    items    = data.get("cameras") or []

    if not items:
        return jsonify({"success": False, "message": "Tidak ada kamera untuk ditambahkan"}), 400
    if dvr_name and not _validate_dvr_name(dvr_name):
        return jsonify({"success": False, "message": f"DVR '{dvr_name}' tidak ditemukan"}), 400

    added, failed = [], []
    for it in items:
        try:
            rtsp = (it.get("rtsp_url") or "").strip()
            name = (it.get("name") or f"Channel {it.get('channel', '?')}").strip()
            if not rtsp:
                failed.append({"channel": it.get("channel"), "error": "RTSP kosong"})
                continue

            new_id = dbmod.add_camera(
                name          = name,
                rtsp_url      = rtsp,
                location      = (it.get("location") or location),
                nvr_dvr       = it.get("nvr_dvr") or "dvr",
                channel       = int(it.get("channel") or 1),
                codec         = it.get("codec") or "auto",
                is_public     = bool(it.get("is_public", True)),
                is_active     = bool(it.get("is_active", True)),
                dvr           = dvr_name,
            )
            try:
                dbmod.set_camera_roles(new_id, ["admin", "supervisor"])
            except Exception as e:
                app.logger.warning(f"[bulk-add1] set roles gagal: {e}")

            added.append({"channel": it.get("channel"), "id": new_id, "name": name})
        except Exception as e:
            failed.append({"channel": it.get("channel"), "error": str(e)})

    return jsonify({
        "success":     True,
        "added_count": len(added),
        "failed_count": len(failed),
        "added":       added,
        "failed":      failed,
    })


@app.route("/api/camera/bulk-add", methods=["POST"])
@admin_required
def api_camera_bulk_add():
    """
    Tambah / update banyak kamera sekaligus (upsert).
    Bekerja untuk Dahua & Hikvision (vendor terdeteksi dari DVR row).
    """
    data     = request.get_json(silent=True) or {}
    dvr_name = (data.get("dvr_name") or "").strip() or None
    location = (data.get("location") or "").strip() or None
    items    = data.get("cameras") or []

    if not items:
        return jsonify({"success": False, "message": "Tidak ada kamera untuk ditambahkan"}), 400
    if dvr_name and not _validate_dvr_name(dvr_name):
        return jsonify({"success": False, "message": f"DVR '{dvr_name}' tidak ditemukan"}), 400

    # ---- Dapatkan dvr_row untuk bangun RTSP kalau kosong ----
    dvr_row = None
    if dvr_name:
        try:
            dvr_row = next(
                (d for d in dbmod.fetch_all_dvr() if d.get("name") == dvr_name),
                None,
            )
        except Exception:
            dvr_row = None

    try:
        existing_cams = dbmod.fetch_all_cameras()
    except Exception as e:
        return jsonify({"success": False, "message": f"Gagal membaca data kamera: {e}"}), 500

    by_dvr_channel = {}
    by_name_dvr    = {}

    for c in existing_cams:
        c_dvr   = (c.get("dvr") or "").strip().lower()
        c_ch    = int(c.get("channel") or 0)
        c_name  = (c.get("name") or "").strip().lower()
        if c_dvr and c_ch:
            by_dvr_channel[(c_dvr, c_ch)] = c
        if c_name:
            by_name_dvr[(c_name, c_dvr)] = c

    added, updated, failed = [], [], []

    for it in items:
        try:
            name  = (it.get("name") or f"Channel {it.get('channel', '?')}").strip()
            chan  = int(it.get("channel") or 1)

            # === HIKVISION: bangun RTSP kalau kosong, sesuai vendor DVR ===
            rtsp = (it.get("rtsp_url") or "").strip()
            if not rtsp and dvr_row:
                rtsp = _build_rtsp_for_camera(
                    {"channel": chan, "rtsp_url": None}, dvr_row
                )
            if not rtsp:
                failed.append({"channel": chan, "error": "RTSP kosong & tidak bisa dibangun"})
                continue
            # === END HIKVISION ===

            match = None
            if dvr_name and chan:
                match = by_dvr_channel.get((dvr_name.strip().lower(), chan))

            common_fields = dict(
                name          = name,
                rtsp_url      = rtsp,
                location      = (it.get("location") or location),
                nvr_dvr       = it.get("nvr_dvr") or "dvr",
                channel       = chan,
                codec         = it.get("codec") or "auto",
                is_public     = bool(it.get("is_public", True)),
                is_active     = bool(it.get("is_active", True)),
                dvr           = dvr_name,
            )

            if match:
                ok = dbmod.update_camera(match["id"], **common_fields)
                if ok:
                    updated.append({"channel": chan, "id": match["id"], "name": name})
                else:
                    updated.append({
                        "channel": chan, "id": match["id"], "name": name,
                        "note":    "tidak ada perubahan",
                    })

                mtx_path = match.get("mtx_path") or _camera_slug(name, match["id"])
                try:
                    mtx.patch_path(mtx_path, rtsp, on_demand=True)
                    if not match.get("mtx_path"):
                        dbmod.set_camera_mtx_path(match["id"], mtx_path)
                except Exception as e:
                    app.logger.warning(f"[bulk-add] MediaMTX update gagal: {e}")

            else:
                new_id = dbmod.add_camera(**common_fields)
                added.append({"channel": chan, "id": new_id, "name": name})

                try:
                    dbmod.set_camera_roles(new_id, ["admin", "supervisor"])
                except Exception as e:
                    app.logger.warning(f"[bulk-add] set roles gagal: {e}")

                try:
                    slug = _camera_slug(name, new_id)
                    ok_mtx, msg_mtx = mtx.add_path(slug, rtsp, on_demand=True)
                    if ok_mtx:
                        dbmod.set_camera_mtx_path(new_id, slug)
                except Exception as e:
                    app.logger.warning(f"[bulk-add] MediaMTX add gagal: {e}")

                if dvr_name:
                    by_dvr_channel[(dvr_name.strip().lower(), chan)] = {
                        "id": new_id, "name": name, "channel": chan, "dvr": dvr_name,
                        "mtx_path": None,
                    }
                by_name_dvr[(name.lower(), (dvr_name or "").lower())] = {
                    "id": new_id, "name": name, "dvr": dvr_name, "mtx_path": None,
                }

        except Exception as e:
            failed.append({"channel": it.get("channel"), "error": str(e)})

    return jsonify({
        "success":       True,
        "added_count":   len(added),
        "updated_count": len(updated),
        "failed_count":  len(failed),
        "added":         added,
        "updated":       updated,
        "failed":        failed,
        "message": (
            f"{len(added)} ditambahkan, "
            f"{len(updated)} diperbarui"
            + (f", {len(failed)} gagal" if failed else "")
        ),
    })


@app.route("/")
def landing():
    if session.get("logged_in"):
        return redirect(url_for("dashboard"))
    stats = dbmod.compute_stats() if Config.DB_ENABLED else dbmod.FALLBACK_STATS
    return render_template("landing.html", stats=stats)


@app.route("/dashboard")
@login_required
def dashboard():
    touch_online_user()
    role = session.get("role", "public")

    if Config.DB_ENABLED:
        stats    = dbmod.compute_stats()
        dvr_list = dbmod.fetch_all_dvr() if role in ("admin", "supervisor") else []
        cameras  = dbmod.fetch_cameras_for_role(role)
        dvr_with_cameras, orphan_cameras = dbmod.fetch_dvr_with_cameras(
            cameras_override=cameras
        )
    online_users = get_online_users()
    stats["users_online"]    = len(online_users)
    stats["users_usernames"] = [u["username"] for u in online_users]

    return render_template(
        "dashboard.html",
        stats=stats,
        dvr_list=dvr_list,
        cameras=cameras,
        dvr_with_cameras=dvr_with_cameras,
        orphan_cameras=orphan_cameras,
        online_users=online_users,
        user=session.get("user", "Admin"),
        role=role,
        all_roles=dbmod.ALL_ROLES,
    )


@app.route("/konfigurasi")
@admin_required
def konfigurasi():
    if Config.DB_ENABLED:
        stats    = dbmod.compute_stats()
        dvr_list = dbmod.fetch_all_dvr()
    else:
        stats, dvr_list = dbmod.FALLBACK_STATS, []

    return render_template(
        "konfigurasi.html",
        stats=stats,
        dvr_list=dvr_list,
        user=session.get("user", "Admin"),
        last_updated=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )


# ====================================================================
# DVR page
# ====================================================================
@app.route("/dvr")
@admin_required
def dvr_page():
    dvrs = dbmod.fetch_all_dvr() if Config.DB_ENABLED else []
    return render_template("dvr.html", dvrs=dvrs,
                           year=datetime.now().year,
                           current_user={"full_name": session.get("user")})


@app.route("/dvr/add", methods=["POST"])
@admin_required
def dvr_add():
    try:
        vendor = (request.form.get("vendor") or "dahua").strip().lower()
        dbmod.add_dvr(
            name    = request.form.get("name", "").strip(),
            lokasi  = request.form.get("lokasi"),
            channel = int(request.form.get("channel") or 0),
            ip      = request.form.get("ip"),
            port    = int(request.form.get("port") or 554),
            status  = 0,
            vendor  = vendor,
        )
        flash("DVR berhasil ditambahkan", "success")
    except Exception as e:
        flash(f"Gagal menambah DVR: {e}", "error")
    return redirect(url_for("dvr_page"))


@app.route("/dvr/delete/<int:dvr_id>", methods=["POST"])
@admin_required
def dvr_delete(dvr_id):
    try:
        dbmod.delete_dvr(dvr_id)
        flash("DVR berhasil dihapus", "success")
    except Exception as e:
        flash(f"Gagal menghapus DVR: {e}", "error")
    return redirect(url_for("dvr_page"))


# ====================================================================
# Camera pages
# ====================================================================
@app.route("/camera")
@supervisor_required
def camera_page():
    role = session.get("role", "public")
    cameras = dbmod.fetch_cameras_for_role(role) if Config.DB_ENABLED else []
    return render_template(
        "camera.html",
        cameras=cameras,
        year=datetime.now().year,
        user=session.get("user", "Admin"),
        role=role,
    )


@app.route("/camera/add", methods=["POST"])
@admin_required
def camera_add():
    try:
        new_id = dbmod.add_camera(
            name        = request.form.get("name", "").strip(),
            rtsp_url    = request.form.get("rtsp_url", "").strip(),
            location    = request.form.get("location"),
            nvr_dvr     = request.form.get("nvr_dvr", "ipcam"),
            channel     = int(request.form.get("channel") or 1),
            codec       = request.form.get("codec", "auto"),
            is_public   = bool(request.form.get("is_public")),
            is_active   = bool(request.form.get("is_active")),
            youtube_embed = request.form.get("youtube_embed"),
        )
        roles = request.form.getlist("roles") or ["admin", "supervisor"]
        dbmod.set_camera_roles(new_id, roles)
        flash("Kamera berhasil ditambahkan", "success")
    except Exception as e:
        flash(f"Gagal menambah kamera: {e}", "error")
    return redirect(url_for("camera_page"))


@app.route("/camera/delete/<int:camera_id>", methods=["POST"])
@admin_required
def camera_delete(camera_id):
    try:
        dbmod.delete_camera(camera_id)
        flash("Kamera berhasil dihapus", "success")
    except Exception as e:
        flash(f"Gagal menghapus kamera: {e}", "error")
    return redirect(url_for("camera_page"))


@app.route("/camera/<int:camera_id>")
@login_required
def camera_view(camera_id):
    """Viewer kamera — semua role yang punya akses boleh lihat."""
    role = session.get("role", "public")

    cam = dbmod.fetch_camera(camera_id)
    if not cam:
        return "Kamera tidak ditemukan", 404

    if not dbmod.has_camera_access(camera_id, role):
        flash("Anda tidak memiliki akses ke kamera ini", "error")
        return redirect(url_for("dashboard"))

    slug = cam.get("mtx_path") or _camera_slug(cam["name"], camera_id)

    # === HIKVISION: bangun RTSP sesuai vendor ===
    dvr_row = None
    if cam.get("dvr"):
        try:
            dvr_row = next(
                (d for d in dbmod.fetch_all_dvr() if d.get("name") == cam["dvr"]),
                None,
            )
        except Exception:
            dvr_row = None
    rtsp = _build_rtsp_for_camera(cam, dvr_row)
    # === END HIKVISION ===

    try:
        existing = mtx.get_path(slug)
        if existing:
            mtx.patch_path(slug, rtsp, on_demand=True)
        else:
            app.logger.info(f"[camera_view] Path {slug} tidak ada, re-register")
            mtx.add_path(slug, rtsp, on_demand=True)

        if not cam.get("mtx_path"):
            dbmod.set_camera_mtx_path(camera_id, slug)
            cam["mtx_path"] = slug
    except Exception as e:
        app.logger.warning(f"[camera_view] Gagal register mtx: {e}")

    hls_url = f"/stream/{slug}/index.m3u8"
    return render_template("viewer.html", cam=cam, hls_url=hls_url)


@app.route("/api/mediamtx/sync", methods=["POST"])
@admin_required
def api_mtx_sync():
    if Config.DB_ENABLED is False:
        return jsonify({"success": False, "message": "DB tidak aktif"}), 400

    cameras = dbmod.fetch_all_cameras()
    dvrs = {d["name"]: d for d in dbmod.fetch_all_dvr()}
    synced, failed = 0, []

    for cam in cameras:
        # === HIKVISION: bangun RTSP by vendor ===
        dvr_row = dvrs.get(cam.get("dvr")) if cam.get("dvr") else None
        rtsp = _build_rtsp_for_camera(cam, dvr_row)
        # === END HIKVISION ===

        slug = cam.get("mtx_path") or _camera_slug(cam["name"], cam["id"])

        ok, msg = mtx.add_path(slug, rtsp, on_demand=True)
        if ok:
            synced += 1
            if not cam.get("mtx_path"):
                try:
                    with dbmod.get_cursor(commit=True) as cur:
                        cur.execute(
                            "UPDATE cameras SET mtx_path = %s WHERE id = %s",
                            (slug, cam["id"]),
                        )
                except Exception:
                    pass
        else:
            failed.append({"id": cam["id"], "name": cam["name"], "error": msg})

    return jsonify({
        "success": True,
        "synced": synced,
        "failed": failed,
        "message": f"{synced}/{len(cameras)} kamera disinkronkan",
    })


@app.route("/api/dvr/discover", methods=["POST"])
@admin_required
def dvr_discover():
    """
    Discovery channel dari DVR — kompatibel untuk Dahua & Hikvision.
    """
    data = request.get_json(silent=True) or {}
    host = (data.get("host") or "").strip()
    port = int(data.get("port", 80))
    rtsp_port = int(data.get("rtsp_port", 554))
    username = (data.get("username") or "").strip()
    password = data.get("password", "")
    vendor   = (data.get("vendor") or "").strip().lower()

    if not host or not username:
        return jsonify({"success": False, "message": "Host dan username wajib diisi"}), 400

    # === HIKVISION ===
    if not vendor:
        vendor = _guess_vendor(host, port, username, password)

    if vendor == "hikvision":
        try:
            cameras, source = hikvision_probe.detect_hikvision_cameras(
                ip=host, http_port=port, user=username, password=password,
                rtsp_port=rtsp_port, max_channels=64, stream=1, use_isapi=True,
            )
        except requests.exceptions.ConnectTimeout:
            return jsonify({
                "success": False,
                "message": f"Timeout: DVR {host}:{port} tidak merespons."
            }), 502
        except requests.exceptions.ConnectionError as e:
            return jsonify({
                "success": False,
                "message": f"Tidak bisa konek ke {host}:{port}. Detail: {e}"
            }), 502
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 401:
                return jsonify({
                    "success": False,
                    "message": "Username atau password DVR salah (401)."
                }), 502
            return jsonify({
                "success": False,
                "message": f"DVR balas HTTP {e.response.status_code}."
            }), 502
        except Exception as e:
            return jsonify({
                "success": False,
                "message": f"Error: {type(e).__name__}: {e}"
            }), 500

        if not cameras:
            return jsonify({"success": False, "message": "Tidak ada channel ditemukan."}), 404

        return jsonify({
            "success": True,
            "vendor":  "hikvision",
            "host":    host,
            "port":    port,
            "source":  source,
            "total":   len(cameras),
            "channels": cameras,
        })
    # === END HIKVISION ===

    # -------- Dahua (existing) --------
    client = DahuaClient(host, port, username, password)
    try:
        channels = client.get_channels()
    except requests.exceptions.ConnectTimeout:
        return jsonify({
            "success": False,
            "message": f"Timeout: DVR {host}:{port} tidak merespons. Cek IP/port."
        }), 502
    except requests.exceptions.ConnectionError as e:
        return jsonify({
            "success": False,
            "message": f"Tidak bisa konek ke {host}:{port}. Detail: {e}"
        }), 502
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 401:
            return jsonify({
                "success": False,
                "message": "Username atau password DVR salah (401 Unauthorized)."
            }), 502
        return jsonify({
            "success": False,
            "message": f"DVR balas HTTP {e.response.status_code}. Cek kredensial/URL."
        }), 502
    except Exception as e:
        return jsonify({
            "success": False,
            "message": f"Error: {type(e).__name__}: {e}"
        }), 500

    if not channels:
        return jsonify({"success": False, "message": "Tidak ada channel ditemukan di DVR."}), 404

    return jsonify({
        "success": True,
        "vendor":  "dahua",
        "host": host,
        "port": port,
        "total": len(channels),
        "channels": channels,
    })


@app.route("/api/cameras/bulk-add", methods=["POST"])
@admin_required
def bulk_add_cameras():
    try:
        data = request.get_json()
        new_cameras = data.get("cameras", [])
        if not isinstance(new_cameras, list) or not new_cameras:
            return jsonify({"success": False, "message": "Data kamera tidak valid"}), 400

        existing = load_saved_cameras()
        existing_keys = {(c.get("host"), c.get("channel")) for c in existing}

        added = []
        for cam in new_cameras:
            key = (cam.get("host"), cam.get("channel"))
            if key in existing_keys:
                continue
            cam["id"] = f"cam_{len(existing) + len(added) + 1}"
            cam.setdefault("status", "online")
            existing.append(cam)
            existing_keys.add(key)
            added.append(cam)
        save_cameras(existing)

        return jsonify({
            "success": True,
            "added": len(added),
            "total": len(existing),
            "cameras": existing,
        })
    except Exception as e:
        print(str(e))
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/cameras", methods=["GET"])
@admin_required
def list_cameras():
    return jsonify({"success": True, "cameras": load_saved_cameras()})


@app.route("/api/cameras/<cam_id>", methods=["DELETE"])
@admin_required
def delete_camera(cam_id):
    cameras = load_saved_cameras()
    cameras = [c for c in cameras if c.get("id") != cam_id]
    save_cameras(cameras)
    return jsonify({"success": True, "cameras": cameras})


# ====================================================================
# Users
# ====================================================================
@app.route("/users")
@admin_required
def users_page():
    users = dbmod.fetch_all_users() if Config.DB_ENABLED else []
    for u in users:
        u.pop("password", None)

    return render_template(
        "users.html",
        users=users,
        user=session.get("user", "Admin"),
        current_user_id=session.get("user_id"),
        stats=dbmod.compute_stats() if Config.DB_ENABLED else dbmod.FALLBACK_STATS,
    )


@app.route("/camera-health")
@supervisor_required
def camera_health_page():
    return render_template(
        "camera_health.html",
        user=session.get("user", "Admin"),
    )


# ====================================================================
# CAMERA HEALTH CHECK
# ====================================================================
@app.route("/api/camera/health")
@supervisor_required
def api_camera_health():
    import concurrent.futures

    channel = request.args.get("channel", type=int)
    only_failed = request.args.get("only_failed", "0") == "1"
    check_frame = request.args.get("check_frame", "0") == "1"

    try:
        cameras = dbmod.fetch_all_cameras() if Config.DB_ENABLED else []
    except Exception as e:
        return jsonify({"success": False, "message": f"DB error: {e}"}), 500

    if channel:
        cameras = [c for c in cameras if c.get("channel") == channel]

    mtx_paths = _get_mtx_paths_dict()
    started = time.time()

    result = {
        "success":     True,
        "timestamp":   datetime.now().isoformat(timespec="seconds"),
        "total":       len(cameras),
        "ok":          0,
        "failed":      0,
        "cameras":     [],
        "failed_list": [],
    }

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(_analyze_camera_health, c, mtx_paths, check_frame): c
                   for c in cameras}
        for f in concurrent.futures.as_completed(futures):
            try:
                r = f.result()
                result["cameras"].append(r)
                if r["issues"]:
                    result["failed"] += 1
                    result["failed_list"].append(r)
                else:
                    result["ok"] += 1
            except Exception as e:
                cam = futures[f]
                result["failed"] += 1
                result["failed_list"].append({
                    "id": cam.get("id"),
                    "name": cam.get("name"),
                    "channel": cam.get("channel"),
                    "dvr": cam.get("dvr"),
                    "mtx_path": cam.get("mtx_path"),
                    "issues": [f"Exception: {e}"],
                    "layer_status": {},
                })

    result["cameras"].sort(key=lambda c: c.get("channel") or 0)
    result["failed_list"].sort(key=lambda c: c.get("channel") or 0)
    result["elapsed_sec"] = round(time.time() - started, 2)

    if only_failed:
        result["cameras"] = result["failed_list"]

    return jsonify(result)


# ---------------- Helpers ----------------
def _get_mtx_paths_dict():
    try:
        r = _requests.get(f"{Config.MEDIAMTX_API}/v3/paths/list", timeout=5)
        if r.status_code != 200:
            return {}
        items = r.json().get("items", [])
        return {p["name"]: p for p in items}
    except Exception:
        return {}


def _probe_rtsp_quick(rtsp_url: str) -> dict:
    import subprocess
    try:
        p = subprocess.run(
            ["ffprobe",
             "-rtsp_transport", "tcp",
             "-timeout", "5000000",
             "-v", "error",
             "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,width,height",
             "-of", "default=noprint_wrappers=1",
             "-i", rtsp_url],
            capture_output=True, text=True, timeout=10,
        )
        if p.returncode == 0 and p.stdout.strip():
            info = {}
            for line in p.stdout.strip().splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    info[k.strip()] = v.strip()
            return {
                "ok":     True,
                "codec":  info.get("codec_name", "?"),
                "width":  int(info.get("width", 0)),
                "height": int(info.get("height", 0)),
            }
        err = p.stderr.strip().splitlines()[-1][:120] if p.stderr else "unknown"
        return {"ok": False, "error": err}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "ffprobe timeout"}
    except FileNotFoundError:
        return {"ok": False, "error": "ffprobe tidak ditemukan"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


def _check_hls_quick(mtx_path: str) -> dict:
    url = f"{Config.MEDIAMTX_HLS.rstrip('/')}/{mtx_path}/index.m3u8"
    auth = (
        getattr(Config, "MEDIAMTX_USER", None),
        getattr(Config, "MEDIAMTX_PASS", None),
    )
    try:
        r = _requests.get(url, auth=auth if auth[0] else None, timeout=8)
        if r.status_code == 200 and r.text.strip().startswith("#EXTM3U"):
            return {"ok": True}
        return {"ok": False, "status": r.status_code, "error": f"HTTP {r.status_code}"}
    except _requests.exceptions.ConnectionError:
        return {"ok": False, "error": "MediaMTX tidak dapat dihubungi"}
    except _requests.exceptions.Timeout:
        return {"ok": False, "error": "HLS timeout"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


def _grab_frame_stats(rtsp_url: str, timeout_sec: int = 8) -> dict:
    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    tmp.close()
    try:
        p = subprocess.run(
            [
                "ffmpeg",
                "-rtsp_transport", "tcp",
                "-timeout", "5000000",
                "-i", rtsp_url,
                "-frames:v", "1",
                "-q:v", "5",
                "-y", tmp.name,
            ],
            capture_output=True, text=True, timeout=timeout_sec,
        )
        if p.returncode != 0 or not _os.path.exists(tmp.name):
            err = (p.stderr or "").strip().splitlines()[-1:] or ["unknown"]
            return {"ok": False, "error": err[0][:120]}

        size = _os.path.getsize(tmp.name)
        if size < 1024:
            return {
                "ok": True, "bytes": size,
                "avg_brightness": 0, "is_black": True, "is_frozen_hint": False,
            }

        p2 = subprocess.run(
            ["ffmpeg", "-i", tmp.name,
             "-vf", "signalstats,metadata=print:file=-",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=5,
        )
        avg_y = 0
        for line in (p2.stdout or "").splitlines():
            if "lavfi.signalstats.YAVG=" in line:
                try:
                    avg_y = float(line.split("=")[1])
                except Exception:
                    pass
                break

        return {
            "ok": True, "bytes": size,
            "avg_brightness": round(avg_y, 1),
            "is_black": avg_y < 5.0, "is_frozen_hint": False,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "ffmpeg timeout (grab frame)"}
    except FileNotFoundError:
        return {"ok": False, "error": "ffmpeg tidak ditemukan"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}
    finally:
        try:
            _os.unlink(tmp.name)
        except Exception:
            pass


def _is_frozen(rtsp_url: str, timeout_sec: int = 10) -> dict:
    import hashlib
    hashes = []
    for _ in range(2):
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        tmp.close()
        try:
            p = subprocess.run(
                ["ffmpeg", "-rtsp_transport", "tcp", "-timeout", "5000000",
                 "-i", rtsp_url, "-frames:v", "1", "-q:v", "5", "-y", tmp.name],
                capture_output=True, text=True, timeout=timeout_sec,
            )
            if p.returncode != 0 or not _os.path.exists(tmp.name):
                return {"ok": False, "error": "gagal grab frame"}
            with open(tmp.name, "rb") as f:
                hashes.append(hashlib.md5(f.read()).hexdigest())
        finally:
            try: _os.unlink(tmp.name)
            except Exception: pass
        time.sleep(1.2)

    return {"ok": True, "is_frozen": hashes[0] == hashes[1], "hashes": hashes}


def _analyze_camera_health(cam: dict, mtx_paths: dict, check_frame: bool = False) -> dict:
    result = {
        "id":          cam.get("id"),
        "name":        cam.get("name"),
        "channel":     cam.get("channel"),
        "dvr":         cam.get("dvr"),
        "mtx_path":    cam.get("mtx_path"),
        "codec":       None,
        "resolution":  None,
        "issues":      [],
        "layer_status": {"db": "ok", "rtsp": "skip", "mediamtx": "skip",
                         "hls": "skip", "frame": "skip"},
    }

    if not cam.get("is_active"):
        result["issues"].append("Kamera dinonaktifkan (is_active=0)")
        result["layer_status"]["db"] = "fail"
        return result

    if not cam.get("rtsp_url"):
        result["issues"].append("RTSP URL kosong di database")
        result["layer_status"]["db"] = "fail"
        return result

    rtsp = _probe_rtsp_quick(cam["rtsp_url"])
    if not rtsp["ok"]:
        result["issues"].append(f"RTSP gagal: {rtsp.get('error','?')}")
        result["layer_status"]["rtsp"] = "fail"
        return result

    result["layer_status"]["rtsp"] = "ok"
    result["codec"]      = rtsp["codec"]
    result["resolution"] = f"{rtsp['width']}x{rtsp['height']}"

    mtx_path = cam.get("mtx_path")
    if not mtx_path:
        result["issues"].append("Belum punya mtx_path (belum di-sync)")
        return result

    mtx = mtx_paths.get(mtx_path)
    if not mtx:
        result["issues"].append(f"Path '{mtx_path}' belum terdaftar di MediaMTX")
        result["layer_status"]["mediamtx"] = "fail"
        return result

    if not mtx.get("ready"):
        result["issues"].append("Path belum ready di MediaMTX")
        result["layer_status"]["mediamtx"] = "fail"
        return result

    result["layer_status"]["mediamtx"] = "ok"

    hls = _check_hls_quick(mtx_path)
    if not hls["ok"]:
        result["issues"].append(f"HLS gagal: {hls.get('error','?')}")
        if hls.get("status") == 500:
            tracks = mtx.get("tracks") or []
            if any("H265" in t or "HEVC" in t for t in tracks):
                result["issues"].append(
                    "H.265 terdeteksi — cek hlsVariant harus 'fmp4' "
                    "atau upgrade MediaMTX ke v1.15.5+"
                )
            else:
                result["issues"].append(
                    "Muxer error — cek log MediaMTX (kemungkinan bug DTS)"
                )
        elif hls.get("status") == 401:
            result["issues"].append("401 Unauthorized — cek 'action: read' di mediamtx.yml")
        result["layer_status"]["hls"] = "fail"
        return result

    result["layer_status"]["hls"] = "ok"

    if check_frame:
        fr = _grab_frame_stats(cam["rtsp_url"])
        if not fr.get("ok"):
            result["issues"].append(f"Frame tidak bisa diambil: {fr.get('error','?')}")
            result["layer_status"]["frame"] = "fail"
        elif fr.get("is_black"):
            result["issues"].append(
                f"Frame hitam total (brightness={fr.get('avg_brightness')})"
            )
            result["layer_status"]["frame"] = "warn"
        else:
            result["layer_status"]["frame"] = "ok"
            result["avg_brightness"] = fr.get("avg_brightness")

    return result


# ====================================================================
# CRUD USER (JSON API)
# ====================================================================
@app.route("/api/users", methods=["GET"])
@admin_required
def api_users_list():
    try:
        users = dbmod.fetch_all_users()
        for u in users:
            u.pop("password", None)
        return jsonify({"success": True, "data": users})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/users", methods=["POST"])
@admin_required
def api_users_create():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    role     = data.get("role") or "public"

    if not username:
        return jsonify({"success": False, "message": "Username wajib diisi"}), 400
    if len(password) < 6:
        return jsonify({"success": False, "message": "Password minimal 6 karakter"}), 400
    if role not in ("admin", "supervisor", "public"):
        return jsonify({"success": False, "message": "Role tidak valid"}), 400

    try:
        if dbmod.get_user_by_username(username):
            return jsonify({"success": False, "message": "Username sudah digunakan"}), 409

        new_id = dbmod.create_user(username, password, role=role)
        return jsonify({"success": True, "id": new_id,
                        "message": f"User '{username}' berhasil dibuat"})
    except pymysql.err.IntegrityError:
        return jsonify({"success": False, "message": "Username sudah digunakan"}), 409
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/users/<int:user_id>", methods=["PUT"])
@admin_required
def api_users_update(user_id):
    target = dbmod.get_user_by_id(user_id)
    if not target:
        return jsonify({"success": False, "message": "User tidak ditemukan"}), 404

    data     = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip() or None
    password = data.get("password") or None
    role     = data.get("role") or None

    if role and role != "admin" and target["role"] == "admin":
        if dbmod.count_admins() <= 1:
            return jsonify({
                "success": False,
                "message": "Tidak dapat mengubah role admin terakhir",
            }), 400

    if username and username != target["username"]:
        if dbmod.get_user_by_username(username):
            return jsonify({"success": False, "message": "Username sudah digunakan"}), 409

    if password is not None and password != "" and len(password) < 6:
        return jsonify({"success": False, "message": "Password minimal 6 karakter"}), 400

    try:
        ok = dbmod.update_user(user_id, username=username,
                               password=password, role=role)
        if not ok:
            return jsonify({"success": False, "message": "Tidak ada perubahan"}), 400
        return jsonify({"success": True, "message": "User berhasil diperbarui"})
    except pymysql.err.IntegrityError:
        return jsonify({"success": False, "message": "Username sudah digunakan"}), 409
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/users/<int:user_id>", methods=["DELETE"])
@admin_required
def api_users_delete(user_id):
    if user_id == session.get("user_id"):
        return jsonify({"success": False,
                        "message": "Tidak dapat menghapus akun sendiri"}), 400

    target = dbmod.get_user_by_id(user_id)
    if not target:
        return jsonify({"success": False, "message": "User tidak ditemukan"}), 404

    if target["role"] == "admin" and dbmod.count_admins() <= 1:
        return jsonify({"success": False,
                        "message": "Tidak dapat menghapus admin terakhir"}), 400

    try:
        n = dbmod.delete_user(user_id)
        if n == 0:
            return jsonify({"success": False, "message": "Gagal menghapus"}), 500
        return jsonify({"success": True, "message": "User berhasil dihapus"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


# ====================================================================
# Auth API
# ====================================================================
@app.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "GET":
        if session.get("logged_in"):
            return redirect(url_for("dashboard"))
        return render_template("login.html", year=datetime.now().year)

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    ok, user = _do_login(username, password)
    if ok:
        _set_session(user)
        nxt = request.form.get("next") or url_for("dashboard")
        return redirect(nxt)

    flash("Username atau password salah", "error")
    return redirect(url_for("login_page"))


@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.get_json(silent=True) or {}
    username = (data.get('email') or data.get('username') or '').strip()
    password = data.get('password') or ''

    if not username or not password:
        return jsonify({'success': False, 'message': 'Username & password wajib diisi'}), 400

    ok, user = _do_login(username, password)
    if ok:
        _set_session(user)
        return jsonify({'success': True, 'message': 'Login berhasil'})

    return jsonify({'success': False, 'message': 'Username atau password salah'}), 401


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"success": True, "message": "Logout berhasil"})


@app.route("/logout")
def logout_page():
    session.clear()
    return redirect(url_for("landing"))


# ====================================================================
# CRUD DVR (JSON API)
# ====================================================================
@app.route("/api/dvr", methods=["GET"])
@admin_required
def api_dvr_list():
    try:
        dvrs = dbmod.fetch_all_dvr() if Config.DB_ENABLED else []
        return jsonify({"success": True, "data": dvrs})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/dvr", methods=["POST"])
@admin_required
def api_dvr_create():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"success": False, "message": "Nama DVR wajib diisi"}), 400

    # === HIKVISION: validasi vendor ===
    vendor = (data.get("vendor") or "dahua").strip().lower()
    if vendor not in ("dahua", "hikvision"):
        return jsonify({
            "success": False,
            "message": "Vendor harus 'dahua' atau 'hikvision'",
        }), 400
    # === END HIKVISION ===

    try:
        new_id = dbmod.add_dvr(
            name    = name,
            lokasi  = data.get("lokasi") or None,
            channel = int(data.get("channel") or 0),
            ip      = data.get("ip") or None,
            port    = int(data.get("port") or 554),
            status  = int(data.get("status") or 0),
            vendor  = vendor,
        )
        return jsonify({"success": True, "id": new_id,
                        "message": "DVR berhasil ditambahkan"})
    except pymysql.err.IntegrityError:
        return jsonify({"success": False, "message": "Nama DVR sudah digunakan"}), 409
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/dvr/<int:dvr_id>", methods=["PUT"])
@admin_required
def api_dvr_update(dvr_id):
    data = request.get_json(silent=True) or {}
    try:
        vendor = data.get("vendor")
        if vendor is not None:
            vendor = vendor.strip().lower()
            if vendor not in ("dahua", "hikvision"):
                return jsonify({
                    "success": False,
                    "message": "Vendor harus 'dahua' atau 'hikvision'",
                }), 400

        ok = dbmod.update_dvr(
            dvr_id,
            name    = (data.get("name") or "").strip() or None,
            lokasi  = data.get("lokasi"),
            channel = data.get("channel"),
            ip      = data.get("ip"),
            port    = data.get("port"),
            status  = data.get("status"),
            vendor  = vendor,
        )
        if not ok:
            return jsonify({"success": False,
                            "message": "DVR tidak ditemukan atau tidak ada perubahan"}), 404
        return jsonify({"success": True, "message": "DVR berhasil diperbarui"})
    except pymysql.err.IntegrityError:
        return jsonify({"success": False, "message": "Nama DVR sudah digunakan"}), 409
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/dvr/<int:dvr_id>", methods=["DELETE"])
@admin_required
def api_dvr_delete(dvr_id):
    try:
        n = dbmod.delete_dvr(dvr_id) if Config.DB_ENABLED else 0
        if n == 0:
            return jsonify({"success": False, "message": "DVR tidak ditemukan"}), 404
        return jsonify({"success": True, "message": "DVR berhasil dihapus"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


# ====================================================================
# API: stats
# ====================================================================
@app.route("/api/stats")
@login_required
def api_stats():
    if not Config.DB_ENABLED:
        return jsonify(dbmod.FALLBACK_STATS)
    return jsonify(dbmod.compute_stats())


@app.route("/api/save-settings", methods=["POST"])
@admin_required
def save_settings():
    return jsonify({"success": True, "message": "Pengaturan disimpan"})


@app.route("/api/online-users")
@login_required
def api_online_users():
    touch_online_user()
    users = get_online_users()
    return jsonify({
        "count": len(users),
        "users": [{"username": u["username"], "role": u["role"]} for u in users],
    })


# ====================================================================
# CRUD CAMERA (JSON API)
# ====================================================================
@app.route("/api/camera", methods=["GET"])
@admin_required
def api_camera_list():
    try:
        cams = dbmod.fetch_all_cameras() if Config.DB_ENABLED else []
        if cams:
            dbmod._attach_roles(cams)
        return jsonify({"success": True, "data": cams})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/camera", methods=["POST"])
@admin_required
def api_camera_create():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    rtsp = (data.get("rtsp_url") or "").strip()

    RTSP_RE = re.compile(r"^rtsp://[^:]+:[^@]+@[^:/]+(:\d+)?/.+$")

    if not RTSP_RE.match(rtsp):
        return jsonify({
            "success": False,
            "message": "Format RTSP URL salah. Contoh: rtsp://user:pass@192.168.1.10:554/path"
        }), 400

    if not name:
        return jsonify({"success": False, "message": "Nama kamera wajib diisi"}), 400

    roles = data.get("roles")
    if not isinstance(roles, list) or not roles:
        roles = ["admin", "supervisor"]

    on_demand = bool(data.get("on_demand", True))

    try:
        new_id = dbmod.add_camera(
            name          = name,
            rtsp_url      = rtsp,
            location      = data.get("location") or None,
            nvr_dvr       = data.get("nvr_dvr") or "ipcam",
            channel       = int(data.get("channel") or 1),
            codec         = data.get("codec") or "auto",
            is_public     = bool(data.get("is_public", True)),
            is_active     = bool(data.get("is_active", True)),
            lat           = _to_float_or_none(data.get("lat")),
            lng           = _to_float_or_none(data.get("lng")),
            youtube_embed = data.get("youtube_embed") or None,
            dvr           = data.get("dvr") or None,
        )

        try:
            dbmod.set_camera_roles(new_id, roles)
        except Exception as e:
            print(f"[WARN] Gagal set camera roles: {e}")

        slug = _camera_slug(name, new_id)
        ok, msg = mtx.add_path(slug, rtsp, on_demand=on_demand)

        if ok:
            try:
                with dbmod.get_cursor(commit=True) as cur:
                    cur.execute(
                        "UPDATE cameras SET mtx_path = %s WHERE id = %s",
                        (slug, new_id),
                    )
            except Exception as e:
                print(f"[WARN] Gagal simpan mtx_path: {e}")

        return jsonify({
            "success": True,
            "id": new_id,
            "mtx_path": slug,
            "mtx_ok": ok,
            "mtx_message": msg,
            "roles": dbmod.get_camera_roles(new_id),
            "hls_url": mtx.hls_url(slug) if ok else None,
            "message": (
                f"Kamera dibuat. MediaMTX: {msg}" if ok
                else f"Kamera dibuat, tapi MediaMTX gagal: {msg}"
            ),
        })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/camera/<int:camera_id>", methods=["PUT"])
@admin_required
def api_camera_update(camera_id):
    data = request.get_json(silent=True) or {}
    old = dbmod.fetch_camera(camera_id, attach_roles=False)
    if not old:
        return jsonify({"success": False, "message": "Kamera tidak ditemukan"}), 404

    try:
        ok = dbmod.update_camera(
            camera_id,
            name          = (data.get("name") or "").strip() or None,
            rtsp_url      = (data.get("rtsp_url") or "").strip() or None,
            location      = data.get("location"),
            nvr_dvr       = data.get("nvr_dvr"),
            channel       = data.get("channel"),
            codec         = data.get("codec"),
            is_public     = data.get("is_public"),
            is_active     = data.get("is_active"),
            lat           = _to_float_or_none(data.get("lat")),
            lng           = _to_float_or_none(data.get("lng")),
            youtube_embed = data.get("youtube_embed"),
            dvr           = data.get("dvr"),
        )

        if isinstance(data.get("roles"), list):
            try:
                dbmod.set_camera_roles(camera_id, data["roles"])
            except Exception as e:
                print(f"[WARN] Gagal update camera roles: {e}")

        if not ok:
            return jsonify({
                "success": True,
                "message": "Tidak ada perubahan field",
                "roles": dbmod.get_camera_roles(camera_id),
            })

        # === HIKVISION: sync ke MediaMTX dengan vendor-aware RTSP ===
        new = dbmod.fetch_camera(camera_id, attach_roles=False)
        dvr_row = None
        if new.get("dvr"):
            try:
                dvr_row = next(
                    (d for d in dbmod.fetch_all_dvr() if d.get("name") == new["dvr"]),
                    None,
                )
            except Exception:
                dvr_row = None
        rtsp_new = _build_rtsp_for_camera(new, dvr_row)
        # === END HIKVISION ===

        slug = new.get("mtx_path") or _camera_slug(new["name"], camera_id)

        mtx_ok, mtx_msg = mtx.patch_path(slug, rtsp_new, on_demand=True)

        if not new.get("mtx_path"):
            try:
                with dbmod.get_cursor(commit=True) as cur:
                    cur.execute("UPDATE cameras SET mtx_path = %s WHERE id = %s",
                                (slug, camera_id))
            except Exception:
                pass

        return jsonify({
            "success": True,
            "mtx_ok": mtx_ok,
            "mtx_message": mtx_msg,
            "roles": dbmod.get_camera_roles(camera_id),
            "message": "Kamera diperbarui",
        })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/camera/<int:camera_id>", methods=["DELETE"])
@admin_required
def api_camera_delete(camera_id):
    cam = dbmod.fetch_camera(camera_id, attach_roles=False)
    if not cam:
        return jsonify({"success": False, "message": "Kamera tidak ditemukan"}), 404

    mtx_cleanup_ok = True
    mtx_msg = ""
    mtx_path = cam.get("mtx_path")

    if mtx_path:
        try:
            mtx.delete_path(mtx_path)
        except Exception as e:
            mtx_cleanup_ok = False
            mtx_msg = str(e)
            app.logger.warning(
                f"[camera_delete] MediaMTX cleanup gagal untuk '{mtx_path}': {e}"
            )

    try:
        n = dbmod.delete_camera(camera_id)
        if n == 0:
            return jsonify({"success": False, "message": "Gagal menghapus kamera"}), 500
    except Exception as e:
        return jsonify({"success": False, "message": f"Gagal hapus dari DB: {e}"}), 500

    msg = "Kamera & stream berhasil dihapus"
    if not mtx_cleanup_ok and mtx_msg:
        msg += f" (peringatan MediaMTX: {mtx_msg})"

    return jsonify({
        "success": True,
        "mtx_cleanup": mtx_cleanup_ok,
        "mtx_message": mtx_msg,
        "message": msg,
    })


@app.route("/api/camera/bulk-delete", methods=["POST"])
@admin_required
def api_camera_bulk_delete():
    data = request.get_json(silent=True) or {}
    raw_ids = data.get("ids") or []

    if not isinstance(raw_ids, list) or not raw_ids:
        return jsonify({"success": False, "message": "Tidak ada ID kamera yang dipilih"}), 400

    ids_clean = []
    for i in raw_ids:
        try:
            n = int(i)
            if n > 0:
                ids_clean.append(n)
        except (ValueError, TypeError):
            continue

    if not ids_clean:
        return jsonify({"success": False, "message": "ID kamera tidak valid"}), 400

    deleted, failed, mtx_warn = [], [], []

    for cam_id in ids_clean:
        cam = dbmod.fetch_camera(cam_id, attach_roles=False)
        if not cam:
            failed.append({"id": cam_id, "error": "Kamera tidak ditemukan"})
            continue

        mtx_path = cam.get("mtx_path")
        if mtx_path:
            try:
                mtx.delete_path(mtx_path)
            except Exception as e:
                mtx_warn.append({"id": cam_id, "mtx_path": mtx_path, "error": str(e)})

        try:
            n = dbmod.delete_camera(cam_id)
            if n > 0:
                deleted.append({"id": cam_id, "name": cam.get("name")})
            else:
                failed.append({"id": cam_id, "error": "Tidak ada baris terhapus"})
        except Exception as e:
            failed.append({"id": cam_id, "error": str(e)})

    return jsonify({
        "success":         True,
        "deleted_count":   len(deleted),
        "failed_count":    len(failed),
        "mtx_warning_count": len(mtx_warn),
        "deleted":         deleted,
        "failed":          failed,
        "mtx_warnings":    mtx_warn,
        "message": (
            f"{len(deleted)} kamera dihapus"
            + (f", {len(failed)} gagal" if failed else "")
            + (f", {len(mtx_warn)} warning MediaMTX" if mtx_warn else "")
        ),
    })


@app.route("/api/camera/<int:camera_id>/roles", methods=["GET", "PUT"])
@admin_required
def api_camera_roles(camera_id):
    if not dbmod.fetch_camera(camera_id, attach_roles=False):
        return jsonify({"success": False, "message": "Kamera tidak ditemukan"}), 404

    if request.method == "GET":
        return jsonify({"success": True, "roles": dbmod.get_camera_roles(camera_id)})

    data = request.get_json(silent=True) or {}
    roles = data.get("roles")
    if not isinstance(roles, list):
        return jsonify({"success": False, "message": "Format roles tidak valid"}), 400

    try:
        dbmod.set_camera_roles(camera_id, roles)
        return jsonify({
            "success": True,
            "roles": dbmod.get_camera_roles(camera_id),
            "message": "Hak akses kamera diperbarui",
        })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/mediamtx/status")
@login_required
def api_mtx_status():
    return jsonify({
        "alive": mtx.is_alive(),
        "api_url": Config.MEDIAMTX_API,
        "path_count": len(mtx.list_paths()),
    })


def _to_float_or_none(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _camera_slug(cam_name: str, cam_id: int | None = None) -> str:
    base = mtx.slugify(cam_name) or "cam"
    if cam_id is not None:
        return f"{base}-{cam_id}"
    return base


def _camera_rtsp_url(cam: dict) -> str:
    """Legacy — default Dahua. Gunakan _build_rtsp_for_camera untuk vendor-aware."""
    rtsp = (cam.get("rtsp_url") or "").strip()
    if rtsp:
        return rtsp
    return mtx.build_dahua_rtsp(channel=cam.get("channel") or 1)


# ====================================================================
# HLS Reverse Proxy
# ====================================================================
@app.route("/stream/<path:subpath>")
@login_required
def stream_proxy(subpath):
    app.logger.info(f"[stream_proxy] {subpath} qs={request.query_string.decode()}")

    if not (subpath.endswith(".m3u8")
            or subpath.endswith(".ts")
            or subpath.endswith(".m4s")
            or subpath.endswith(".mp4")):
        abort(400, "Invalid stream path")

    base = Config.MEDIAMTX_HLS.rstrip("/")
    qs = request.query_string.decode("utf-8")
    mtx_url = f"{base}/{subpath}"
    if qs:
        mtx_url += f"?{qs}"

    fwd_headers = {}
    for h in ("Range", "If-Modified-Since", "If-None-Match", "Accept"):
        if h in request.headers:
            fwd_headers[h] = request.headers[h]

    auth = (
        getattr(Config, "MEDIAMTX_USER", None),
        getattr(Config, "MEDIAMTX_PASS", None),
    )

    try:
        upstream = _requests.get(
            mtx_url,
            headers=fwd_headers,
            cookies=dict(request.cookies),
            auth=auth if auth[0] else None,
            stream=True,
            timeout=15,
            allow_redirects=False,
        )
    except _requests.exceptions.RequestException as e:
        app.logger.error(f"[stream_proxy] upstream error: {e}")
        return Response(f"MediaMTX tidak dapat dihubungi: {e}", status=502)

    pass_headers = []
    for k, v in upstream.raw.headers.items():
        lk = k.lower()
        if lk in ("content-encoding", "transfer-encoding",
                  "connection", "content-length"):
            continue

        if lk == "location":
            parsed = urlparse(v)
            if not parsed.netloc:
                new_path = parsed.path
                if not new_path.startswith("/stream/"):
                    new_path = "/stream" + new_path
                new_loc = new_path
                if parsed.query:
                    new_loc += "?" + parsed.query
                v = new_loc
            pass_headers.append(("Location", v))
            continue

        if lk == "set-cookie":
            import re as _re
            v = _re.sub(r'Path=/([^;]*)', r'Path=/stream/\1', v, flags=_re.IGNORECASE)
            pass_headers.append(("Set-Cookie", v))
            continue

        pass_headers.append((k, v))

    pass_headers.append(("Access-Control-Allow-Origin", "*"))
    pass_headers.append(("Access-Control-Expose-Headers", "Content-Length, Content-Range"))
    pass_headers.append(("Cache-Control", "no-cache"))

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return Response(
        stream_with_context(generate()),
        status=upstream.status_code,
        headers=pass_headers,
    )


def _startup_sync_cameras():
    """Sync kamera dari DB ke MediaMTX saat Flask start."""
    import time as _t
    for i in range(10):
        if mtx.is_alive():
            break
        print(f"[STARTUP] Menunggu MediaMTX... ({i+1}/10)")
        logger.info(f"[STARTUP] Menunggu MediaMTX... ({i+1}/10)")
        _t.sleep(1)

    if not mtx.is_alive():
        print("[STARTUP] MediaMTX tidak hidup, skip sync")
        logger.warning("[STARTUP] MediaMTX tidak hidup, skip sync")
        return

    try:
        cameras = dbmod.fetch_all_cameras()
        # === HIKVISION: peta nama → row DVR untuk RTSP vendor-aware ===
        dvrs = {d["name"]: d for d in dbmod.fetch_all_dvr()}
        # === END HIKVISION ===

        synced = 0
        for cam in cameras:
            # === HIKVISION: bangun RTSP by vendor ===
            dvr_row = dvrs.get(cam.get("dvr")) if cam.get("dvr") else None
            rtsp = _build_rtsp_for_camera(cam, dvr_row)
            # === END HIKVISION ===

            slug = cam.get("mtx_path") or _camera_slug(cam["name"], cam["id"])
            ok, msg = mtx.add_path(slug, rtsp, on_demand=True)
            if ok:
                synced += 1
                if not cam.get("mtx_path"):
                    try:
                        with dbmod.get_cursor(commit=True) as cur:
                            cur.execute("UPDATE cameras SET mtx_path = %s WHERE id = %s",
                                        (slug, cam["id"]))
                    except Exception:
                        pass
        print(f"[STARTUP] {synced}/{len(cameras)} kamera disinkronkan ke MediaMTX")
        logger.info(f"[STARTUP] {synced}/{len(cameras)} kamera disinkronkan ke MediaMTX")
    except Exception as e:
        print(f"[STARTUP] Sync gagal: {e}")
        logger.error(f"[STARTUP] Sync gagal: {e}", exc_info=True)


# ====================================================================
# MOBILE API
# ====================================================================
MOBILE_TOKEN_TTL = 24 * 3600


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _mobile_secret() -> bytes:
    s = app.secret_key
    return s.encode() if isinstance(s, str) else s


def _mobile_generate_token(user: dict) -> str:
    payload = {
        "uid": user.get("id"),
        "u":   user["username"],
        "r":   user.get("role", "public"),
        "exp": int(time.time()) + MOBILE_TOKEN_TTL,
    }
    body = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    sig  = _hmac.new(_mobile_secret(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64url_encode(sig)}"


def _mobile_verify_token(token: str):
    if not token:
        logger.warning("[MOBILE AUTH] Token kosong")
        return None

    try:
        body, sig_b64 = token.split(".", 1)
    except ValueError:
        logger.warning("[MOBILE AUTH] Format token salah (tidak ada '.')")
        return None

    try:
        expected_sig = _hmac.new(
            _mobile_secret(), body.encode(), hashlib.sha256
        ).digest()
        received_sig = _b64url_decode(sig_b64)

        if not _hmac.compare_digest(expected_sig, received_sig):
            logger.warning("[MOBILE AUTH] Signature mismatch!")
            return None
    except Exception as e:
        logger.warning(f"[MOBILE AUTH] Gagal decode signature: {e}")
        return None

    try:
        payload = json.loads(_b64url_decode(body))
    except Exception as e:
        logger.warning(f"[MOBILE AUTH] Gagal decode payload: {e}")
        return None

    exp = int(payload.get("exp", 0))
    if exp < int(time.time()):
        logger.warning(f"[MOBILE AUTH] Token expired")
        return None

    return payload


def mobile_auth_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        token = ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        if not token:
            token = (request.args.get("token") or "").strip()

        payload = _mobile_verify_token(token) if token else None
        if not payload:
            return jsonify({
                "success": False,
                "code":    "INVALID_TOKEN",
                "message": "Token tidak valid atau kedaluwarsa",
            }), 401

        request.mobile_user = payload

        try:
            ONLINE_USERS[f"mobile:{payload.get('uid') or payload['u']}"] = {
                "username":  payload["u"],
                "role":      payload.get("r", "public"),
                "last_seen": time.time(),
            }
        except Exception:
            pass

        return f(*args, **kwargs)
    return wrapper


def _mobile_user_from_request():
    return getattr(request, "mobile_user", {}) or {}


@app.route("/api/mobile/login", methods=["POST"])
def api_mobile_login():
    data     = request.get_json(silent=True) or {}
    username = (data.get("username") or data.get("email") or "").strip()
    password = data.get("password") or ""

    if not username or not password:
        return jsonify({
            "success": False,
            "message": "Username & password wajib diisi",
        }), 400

    ok, user = _do_login(username, password)
    if not ok or not user:
        return jsonify({
            "success": False,
            "message": "Username atau password salah",
        }), 401

    token = _mobile_generate_token(user)

    ONLINE_USERS[f"mobile:{user.get('id') or user['username']}"] = {
        "username":  user["username"],
        "role":      user.get("role", "public"),
        "last_seen": time.time(),
    }

    return jsonify({
        "success":    True,
        "message":    "Login berhasil",
        "token":      token,
        "expires_in": MOBILE_TOKEN_TTL,
        "user": {
            "id":       user.get("id"),
            "username": user["username"],
            "role":     user.get("role", "public"),
        },
    })


@app.route("/api/mobile/me", methods=["GET"])
@mobile_auth_required
def api_mobile_me():
    me = _mobile_user_from_request()
    return jsonify({
        "success": True,
        "user": {
            "id":       me.get("uid"),
            "username": me.get("u"),
            "role":     me.get("r", "public"),
            "exp":      me.get("exp"),
        },
    })


@app.route("/api/mobile/ping", methods=["GET", "POST"])
@mobile_auth_required
def api_mobile_ping():
    return jsonify({"success": True, "ts": int(time.time())})


def _camera_to_mobile_dict(cam: dict, base_url: str, token: str) -> dict:
    mtx_path = cam.get("mtx_path")
    hls_url  = None
    rtsp_url = cam.get("rtsp_url")

    if mtx_path:
        hls_url = f"{base_url}/api/mobile/stream/{mtx_path}/index.m3u8?token={token}"

    return {
        "id":            cam.get("id"),
        "name":          cam.get("name"),
        "location":      cam.get("location"),
        "dvr":           cam.get("dvr"),
        "nvr_dvr":       cam.get("nvr_dvr"),
        "channel":       cam.get("channel"),
        "codec":         cam.get("codec"),
        "is_active":     bool(cam.get("is_active")),
        "is_public":     bool(cam.get("is_public")),
        "mtx_path":      mtx_path,
        "hls_url":       hls_url,
        "rtsp_url":      rtsp_url,
        "youtube_embed": cam.get("youtube_embed"),
        "lat":           cam.get("lat"),
        "lng":           cam.get("lng"),
    }


@app.route("/api/mobile/cameras", methods=["GET"])
@mobile_auth_required
def api_mobile_cameras():
    me   = _mobile_user_from_request()
    role = me.get("r", "public")

    if not Config.DB_ENABLED:
        return jsonify({"success": True, "count": 0, "cameras": []})

    try:
        cams = dbmod.fetch_cameras_for_role(role)
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

    dvr_q    = (request.args.get("dvr") or "").strip().lower()
    codec_q  = (request.args.get("codec") or "").strip().lower()
    active_q = request.args.get("active")
    kw       = (request.args.get("q") or "").strip().lower()

    def _match(c):
        if dvr_q and (c.get("dvr") or "").lower() != dvr_q:
            return False
        if codec_q and (c.get("codec") or "").lower() != codec_q:
            return False
        if active_q in ("1", "true", "yes") and not c.get("is_active"):
            return False
        if kw:
            hay = f"{c.get('name','')} {c.get('location','')} {c.get('dvr','')}".lower()
            if kw not in hay:
                return False
        return True

    cams = [c for c in cams if _match(c)]

    base_url = request.host_url.rstrip("/")
    token    = request.headers.get("Authorization", "")[7:].strip() or request.args.get("token", "")

    payload = [_camera_to_mobile_dict(c, base_url, token) for c in cams]

    return jsonify({
        "success": True,
        "count":   len(payload),
        "cameras": payload,
    })


@app.route("/api/dvr/status")
@supervisor_required
def api_dvr_status():
    with DVR_STATUS_LOCK:
        cache = dict(DVR_STATUS_CACHE)

    if not cache:
        if Config.DB_ENABLED:
            try:
                dvrs = dbmod.fetch_all_dvr()
                for dvr in dvrs:
                    r = dvr_health.check_dvr_online(dvr, timeout=3.0)
                    cache[dvr["id"]] = {**r, "last_check": time.time()}
            except Exception as e:
                return jsonify({"success": False, "message": str(e)}), 500

    total   = len(cache)
    online  = sum(1 for v in cache.values() if v.get("online"))
    offline = total - online

    return jsonify({
        "success": True,
        "total":   total,
        "online":  online,
        "offline": offline,
        "dvrs":    list(cache.values()),
    })


@app.route("/api/dvr/<int:dvr_id>/check", methods=["POST"])
@admin_required
def api_dvr_check_one(dvr_id):
    dvr = dbmod.fetch_dvr(dvr_id)
    if not dvr:
        return jsonify({"success": False, "message": "DVR tidak ditemukan"}), 404

    r = dvr_health.check_dvr_online(dvr, timeout=5.0)

    if r["online"]:
        stream_r = dvr_health.check_dvr_stream(dvr, channel=1, timeout=10)
        r["stream_ok"]     = stream_r.get("stream_ok")
        r["codec"]         = stream_r.get("codec")
        r["resolution"]    = stream_r.get("resolution")
        r["stream_error"]  = stream_r.get("error")

    try:
        dbmod.update_dvr_status(dvr_id, 1 if r["online"] else 0)
    except Exception as e:
        logger.warning(f"[DVR CHECK] Update DB gagal: {e}")

    with DVR_STATUS_LOCK:
        DVR_STATUS_CACHE[dvr_id] = {**r, "last_check": time.time()}

    return jsonify({"success": True, "status": r})


@app.route("/api/mobile/cameras/<int:camera_id>", methods=["GET"])
@mobile_auth_required
def api_mobile_camera_detail(camera_id):
    me   = _mobile_user_from_request()
    role = me.get("r", "public")

    if not Config.DB_ENABLED:
        return jsonify({"success": False, "message": "DB tidak aktif"}), 400

    cam = dbmod.fetch_camera(camera_id)
    if not cam:
        return jsonify({"success": False, "message": "Kamera tidak ditemukan"}), 404

    try:
        if not dbmod.has_camera_access(camera_id, role):
            return jsonify({"success": False, "message": "Akses ditolak"}), 403
    except Exception:
        pass

    # === HIKVISION: bangun RTSP by vendor ===
    slug = cam.get("mtx_path") or _camera_slug(cam["name"], camera_id)
    dvr_row = None
    if cam.get("dvr"):
        try:
            dvr_row = next(
                (d for d in dbmod.fetch_all_dvr() if d.get("name") == cam["dvr"]),
                None,
            )
        except Exception:
            dvr_row = None
    rtsp = _build_rtsp_for_camera(cam, dvr_row)
    # === END HIKVISION ===

    try:
        existing = mtx.get_path(slug)
        if existing:
            mtx.patch_path(slug, rtsp, on_demand=True)
        else:
            mtx.add_path(slug, rtsp, on_demand=True)
        if not cam.get("mtx_path"):
            dbmod.set_camera_mtx_path(camera_id, slug)
            cam["mtx_path"] = slug
    except Exception as e:
        app.logger.warning(f"[mobile] Gagal register mtx: {e}")

    base_url = request.host_url.rstrip("/")
    token    = request.headers.get("Authorization", "")[7:].strip() or request.args.get("token", "")

    return jsonify({
        "success": True,
        "camera":  _camera_to_mobile_dict(cam, base_url, token),
    })


@app.route("/api/camera/status")
@login_required
def api_camera_status():
    cam_id = request.args.get("id", type=int)
    with CAMERA_STATUS_LOCK:
        if cam_id:
            data = CAMERA_STATUS.get(cam_id)
            if not data:
                return jsonify({"success": False, "message": "Belum ada data"}), 404
            return jsonify({"success": True, "status": data})

        total  = len(CAMERA_STATUS)
        errors = sum(1 for v in CAMERA_STATUS.values() if v["status"] == "error")
        return jsonify({
            "success": True,
            "total":   total,
            "errors":  errors,
            "ok":      total - errors,
            "last_tick_age": round(
                time.time() - max((v.get("last_check", 0)
                                   for v in CAMERA_STATUS.values()), default=time.time()),
                1,
            ),
            "cameras": CAMERA_STATUS,
        })


@app.route("/api/camera/status/refresh", methods=["POST"])
@admin_required
def api_camera_status_refresh():
    threading.Thread(target=_monitor_once, daemon=True).start()
    return jsonify({"success": True, "message": "Refresh dijadwalkan"})


def _monitor_once():
    """Versi sekali-jalan dari _monitor_loop, dipanggil manual."""
    ...


@app.route("/api/mobile/stream/<path:subpath>")
@mobile_auth_required
def api_mobile_stream(subpath):
    if not (subpath.endswith(".m3u8")
            or subpath.endswith(".ts")
            or subpath.endswith(".m4s")
            or subpath.endswith(".mp4")):
        abort(400, "Invalid stream path")

    base   = Config.MEDIAMTX_HLS.rstrip("/")
    qs     = request.query_string.decode("utf-8")
    qs_clean = "&".join(
        p for p in qs.split("&") if p and not p.startswith("token=")
    )
    mtx_url = f"{base}/{subpath}"
    if qs_clean:
        mtx_url += f"?{qs_clean}"

    fwd_headers = {}
    for h in ("Range", "If-Modified-Since", "If-None-Match", "Accept"):
        if h in request.headers:
            fwd_headers[h] = request.headers[h]

    auth = (
        getattr(Config, "MEDIAMTX_USER", None),
        getattr(Config, "MEDIAMTX_PASS", None),
    )

    try:
        upstream = _requests.get(
            mtx_url,
            headers=fwd_headers,
            auth=auth if auth[0] else None,
            stream=True,
            timeout=15,
            allow_redirects=False,
        )
    except _requests.exceptions.RequestException as e:
        return Response(f"MediaMTX tidak dapat dihubungi: {e}", status=502)

    pass_headers = []
    for k, v in upstream.raw.headers.items():
        lk = k.lower()
        if lk in ("content-encoding", "transfer-encoding",
                  "connection", "content-length", "set-cookie"):
            continue
        pass_headers.append((k, v))

    pass_headers.append(("Access-Control-Allow-Origin", "*"))
    pass_headers.append(("Cache-Control", "no-cache"))

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return Response(
        stream_with_context(generate()),
        status=upstream.status_code,
        headers=pass_headers,
    )


@app.route("/api/mobile/logout", methods=["POST"])
@mobile_auth_required
def api_mobile_logout():
    me = _mobile_user_from_request()
    key = f"mobile:{me.get('uid') or me.get('u')}"
    ONLINE_USERS.pop(key, None)
    return jsonify({"success": True, "message": "Logout berhasil"})


@app.route("/api/mobile/refresh", methods=["POST"])
@mobile_auth_required
def api_mobile_refresh():
    me = _mobile_user_from_request()

    user = None
    if Config.DB_ENABLED:
        try:
            user = dbmod.get_user_by_id(me["uid"]) if me.get("uid") else \
                   dbmod.get_user_by_username(me["u"])
        except Exception:
            user = None

    if not user:
        user = {
            "id":       me.get("uid"),
            "username": me.get("u"),
            "role":     me.get("r", "public"),
        }

    new_token = _mobile_generate_token(user)
    return jsonify({
        "success":    True,
        "token":      new_token,
        "expires_in": MOBILE_TOKEN_TTL,
    })


# ====================================================================
# BACKGROUND CAMERA MONITOR
# ====================================================================
CAMERA_STATUS = {}
CAMERA_STATUS_LOCK = threading.Lock()
MONITOR_INTERVAL = 30
MONITOR_WORKERS  = 6
MONITOR_CHECK_FRAME_EVERY = 4


def _monitor_loop():
    import concurrent.futures
    tick = 0
    while True:
        tick += 1
        check_frame = (tick % MONITOR_CHECK_FRAME_EVERY == 0)

        try:
            if not Config.DB_ENABLED:
                time.sleep(MONITOR_INTERVAL)
                continue

            cameras = dbmod.fetch_all_cameras()
            cameras = [c for c in cameras if c.get("is_active")]

            mtx_paths = _get_mtx_paths_dict()
            new_status = {}

            with concurrent.futures.ThreadPoolExecutor(max_workers=MONITOR_WORKERS) as ex:
                futures = {
                    ex.submit(_analyze_camera_health, c, mtx_paths, check_frame): c
                    for c in cameras
                }
                for f in concurrent.futures.as_completed(futures):
                    try:
                        r = f.result()
                        new_status[r["id"]] = {
                            "status":      "ok" if not r["issues"] else "error",
                            "issues":      r["issues"],
                            "layer":       r["layer_status"],
                            "codec":       r.get("codec"),
                            "resolution":  r.get("resolution"),
                            "brightness":  r.get("avg_brightness"),
                            "last_check":  time.time(),
                        }
                    except Exception as e:
                        cam = futures[f]
                        new_status[cam["id"]] = {
                            "status":     "error",
                            "issues":     [f"Monitor exception: {e}"],
                            "layer":      {},
                            "last_check": time.time(),
                        }

            with CAMERA_STATUS_LOCK:
                CAMERA_STATUS.clear()
                CAMERA_STATUS.update(new_status)

            n_err = sum(1 for v in new_status.values() if v["status"] == "error")
            print(f"[MONITOR] tick={tick} check_frame={check_frame} "
                  f"checked={len(new_status)} errors={n_err}")

        except Exception as e:
            print(f"[MONITOR] loop error: {e}")

        time.sleep(MONITOR_INTERVAL)


def _dvr_monitor_loop():
    tick = 0
    while True:
        tick += 1
        check_stream = (tick % DVR_STREAM_CHECK_EVERY == 0)

        try:
            if not Config.DB_ENABLED:
                time.sleep(DVR_CHECK_INTERVAL)
                continue

            dvrs = dbmod.fetch_all_dvr()
            if not dvrs:
                logger.debug("[DVR MONITOR] Tidak ada DVR terdaftar")
                time.sleep(DVR_CHECK_INTERVAL)
                continue

            new_status = {}
            for dvr in dvrs:
                try:
                    r = dvr_health.check_dvr_online(dvr, timeout=3.0)

                    if check_stream and r["online"]:
                        stream_result = dvr_health.check_dvr_stream(
                            dvr, channel=1, timeout=10
                        )
                        r["stream_ok"] = stream_result.get("stream_ok", False)
                        r["codec"]     = stream_result.get("codec")
                        r["resolution"] = stream_result.get("resolution")
                        r["stream_error"] = stream_result.get("error", "")
                    else:
                        r["stream_ok"] = None

                    new_status[dvr["id"]] = {**r, "last_check": time.time()}

                    new_status_int = 1 if r["online"] else 0
                    if dvr.get("status_int") != new_status_int:
                        try:
                            dbmod.update_dvr_status(dvr["id"], new_status_int)
                            logger.info(
                                f"[DVR MONITOR] {dvr['name']} "
                                f"({dvr.get('ip')}) status → "
                                f"{'online' if r['online'] else 'offline'} "
                                f"({r['reason']})"
                            )
                        except Exception as e:
                            logger.warning(f"[DVR MONITOR] Update DB gagal: {e}")

                except Exception as e:
                    logger.error(
                        f"[DVR MONITOR] Error cek DVR '{dvr.get('name')}': {e}",
                        exc_info=True,
                    )
                    new_status[dvr["id"]] = {
                        "id":         dvr["id"],
                        "name":       dvr.get("name"),
                        "online":     False,
                        "reason":     f"Exception: {e}",
                        "last_check": time.time(),
                    }

            with DVR_STATUS_LOCK:
                DVR_STATUS_CACHE.clear()
                DVR_STATUS_CACHE.update(new_status)

            online_count = sum(1 for v in new_status.values() if v["online"])
            logger.info(
                f"[DVR MONITOR] tick={tick} "
                f"checked={len(new_status)} "
                f"online={online_count} "
                f"offline={len(new_status) - online_count} "
                f"{'(deep check)' if check_stream else ''}"
            )

        except Exception as e:
            logger.error(f"[DVR MONITOR] Loop error: {e}", exc_info=True)

        time.sleep(DVR_CHECK_INTERVAL)


# ====================================================================
# PLAYBACK (Dahua + Hikvision)
# ====================================================================
PLAYBACK_CACHE = {}
PLAYBACK_CACHE_LOCK = threading.Lock()
PLAYBACK_CACHE_TTL = 60


def _dahua_loadfile_url(dvr_row, channel, start_dt, end_dt):
    ip   = (dvr_row.get("ip") or "").strip()
    port = int(dvr_row.get("port") or 80)

    def fmt(dt):
        return f"{dt.year}-{dt.month}-{dt.day} {dt.hour}:{dt.minute}:{dt.second}"

    start = _url_quote(fmt(start_dt), safe="")
    end   = _url_quote(fmt(end_dt),   safe="")

    return (
        f"http://{ip}:{port}/cgi-bin/loadfile.cgi"
        f"?action=startLoad&channel={channel}"
        f"&startTime={start}&endTime={end}"
    )


@app.route("/api/playback/segments")
@login_required
def api_playback_segments():
    camera_id = request.args.get("camera_id", type=int)
    date_str  = (request.args.get("date") or "").strip()
    start_hour = request.args.get("start_hour", default=0, type=int)
    end_hour   = request.args.get("end_hour", default=23, type=int)

    if not camera_id:
        return jsonify({"success": False, "message": "camera_id wajib"}), 400
    if not date_str:
        return jsonify({"success": False, "message": "date wajib (YYYY-MM-DD)"}), 400

    cam = dbmod.fetch_camera(camera_id, attach_roles=False)
    if not cam:
        return jsonify({"success": False, "message": "Kamera tidak ditemukan"}), 404

    role = session.get("role", "public")
    if not dbmod.has_camera_access(camera_id, role):
        return jsonify({"success": False, "message": "Akses ditolak"}), 403

    dvr_name = cam.get("dvr")
    if not dvr_name:
        return jsonify({
            "success": False,
            "message": "Kamera ini tidak terhubung ke DVR",
        }), 400

    try:
        dvrs = dbmod.fetch_all_dvr()
        dvr_row = next((d for d in dvrs if d.get("name") == dvr_name), None)
    except Exception as e:
        return jsonify({"success": False, "message": f"Gagal baca DVR: {e}"}), 500

    if not dvr_row:
        return jsonify({
            "success": False,
            "message": f"DVR '{dvr_name}' tidak ditemukan di database",
        }), 404

    vendor = _dvr_vendor(dvr_row)

    segments = []
    for h in range(start_hour, end_hour + 1):
        start_dt = datetime.strptime(f"{date_str} {h:02d}:00:00", "%Y-%m-%d %H:%M:%S")
        end_dt = start_dt + timedelta(hours=1)
        segments.append({
            "hour": h,
            "start": start_dt.isoformat(),
            "end":   end_dt.isoformat(),
            "available": None,
        })

    # === HIKVISION: cek ketersediaan via ISAPI search ===
    try:
        if vendor == "hikvision":
            user = dvr_row.get("user") or getattr(Config, "HIKVISION_USER", "admin")
            pwd  = dvr_row.get("password") or getattr(Config, "HIKVISION_PASSWORD", "")
            pb = hikvision_playback.HikvisionPlayback(
                dvr_row["ip"], dvr_row.get("port") or 80, user, pwd
            )
            avail = pb.check_hourly_availability(cam["channel"], date_str)
            for seg in segments:
                seg["available"] = bool(avail.get(seg["hour"]))
        else:
            found = _dahua_find_recordings(
                dvr_row, cam["channel"], date_str, start_hour, end_hour
            )
            if found:
                for seg in segments:
                    seg["available"] = bool(found.get(seg["hour"]))
    except Exception as e:
        app.logger.warning(f"[playback] find_recordings gagal: {e}")
    # === END HIKVISION ===

    return jsonify({
        "success":  True,
        "vendor":   vendor,
        "camera":   {
            "id":       cam["id"],
            "name":     cam["name"],
            "channel":  cam["channel"],
            "dvr":      dvr_name,
            "nvr_dvr":  cam.get("nvr_dvr"),
        },
        "date":     date_str,
        "segments": segments,
    })


def _dahua_find_recordings(dvr_row, channel, date_str, start_hour, end_hour):
    ip   = (dvr_row.get("ip") or "").strip()
    port = int(dvr_row.get("port") or 80)
    user = getattr(Config, "DAHUA_USER", None) or dvr_row.get("user")
    passwd = getattr(Config, "DAHUA_PASSWORD", None) or dvr_row.get("password")

    if not ip or not user:
        return None

    try:
        start_dt = datetime.strptime(f"{date_str} {start_hour:02d}:00:00",
                                     "%Y-%m-%d %H:%M:%S")
        end_dt   = datetime.strptime(f"{date_str} {end_hour:02d}:59:59",
                                     "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None

    def fmt(dt):
        return f"{dt.year}-{dt.month}-{dt.day} {dt.hour}:{dt.minute}:{dt.second}"

    url = (
        f"http://{ip}:{port}/cgi-bin/mediaFileFind.cgi"
        f"?action=findFile"
        f"&channel={channel}"
        f"&startTime={_url_quote(fmt(start_dt), safe='')}"
        f"&endTime={_url_quote(fmt(end_dt), safe='')}"
    )

    try:
        r = requests.get(
            url,
            auth=HTTPDigestAuth(user, passwd),
            timeout=8,
        )
    except Exception:
        return None

    if r.status_code != 200:
        return None

    import re
    text = r.text or ""

    hours = {}
    for m in re.finditer(
        r"<file>.*?"
        r"<StartTime>(\d{4}-\d{1,2}-\d{1,2})\s+(\d{1,2}):\d{2}:\d{2}</StartTime>"
        r".*?</file>",
        text, re.DOTALL
    ):
        try:
            hours[int(m.group(2))] = True
        except Exception:
            pass

    if not hours:
        for line in text.splitlines():
            m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):", line)
            if m:
                try: hours[int(m.group(4))] = True
                except: pass

    return hours or None


@app.route("/api/playback/stream")
@login_required
def api_playback_stream():
    """Legacy — untuk Dahua saja. Untuk Hikvision, gunakan /api/playback/session."""
    camera_id = request.args.get("camera_id", type=int)
    start_s   = (request.args.get("start") or "").strip()
    end_s     = (request.args.get("end") or "").strip()

    if not camera_id or not start_s or not end_s:
        return jsonify({"success": False,
                        "message": "camera_id, start, end wajib"}), 400

    cam = dbmod.fetch_camera(camera_id, attach_roles=False)
    if not cam:
        return jsonify({"success": False, "message": "Kamera tidak ditemukan"}), 404

    role = session.get("role", "public")
    if not dbmod.has_camera_access(camera_id, role):
        return jsonify({"success": False, "message": "Akses ditolak"}), 403

    dvr_name = cam.get("dvr")
    if not dvr_name:
        return jsonify({"success": False,
                        "message": "Kamera bukan tipe NVR/DVR"}), 400

    try:
        dvrs = dbmod.fetch_all_dvr()
        dvr_row = next((d for d in dvrs if d.get("name") == dvr_name), None)
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

    if not dvr_row:
        return jsonify({"success": False,
                        "message": f"DVR '{dvr_name}' tidak ditemukan"}), 404

    # Hikvision: tidak support loadfile.cgi (pakai session-based playback).
    vendor = _dvr_vendor(dvr_row)
    if vendor == "hikvision":
        return jsonify({
            "success": False,
            "message": "Gunakan /api/playback/session untuk Hikvision",
        }), 400

    try:
        start_dt = datetime.fromisoformat(start_s.replace("Z", ""))
        end_dt   = datetime.fromisoformat(end_s.replace("Z", ""))
    except ValueError:
        return jsonify({"success": False,
                        "message": "Format datetime salah (ISO 8601)"}), 400

    if end_dt <= start_dt:
        return jsonify({"success": False,
                        "message": "end harus lebih besar dari start"}), 400

    max_duration = timedelta(hours=1)
    if end_dt - start_dt > max_duration:
        end_dt = start_dt + max_duration

    url = _dahua_loadfile_url(dvr_row, cam["channel"], start_dt, end_dt)

    user   = getattr(Config, "DAHUA_USER", None) or dvr_row.get("user")
    passwd = getattr(Config, "DAHUA_PASSWORD", None) or dvr_row.get("password")

    if not user:
        return jsonify({"success": False,
                        "message": "Kredensial DVR belum diset"}), 500

    try:
        upstream = requests.get(
            url,
            auth=HTTPDigestAuth(user, passwd),
            stream=True,
            timeout=30,
        )
    except requests.exceptions.RequestException as e:
        return Response(f"DVR tidak dapat dihubungi: {e}", status=502)

    if upstream.status_code != 200:
        return Response(
            f"DVR menolak request: HTTP {upstream.status_code}",
            status=upstream.status_code,
        )

    headers = {
        "Content-Type": upstream.headers.get("Content-Type", "video/x-dav"),
        "Cache-Control": "no-cache",
        "Access-Control-Allow-Origin": "*",
    }
    if "Content-Length" in upstream.headers:
        headers["Content-Length"] = upstream.headers["Content-Length"]

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=128 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return Response(
        stream_with_context(generate()),
        status=200,
        headers=headers,
    )


@app.route("/api/playback/download")
@login_required
def api_playback_download():
    camera_id = request.args.get("camera_id", type=int)
    start_s   = (request.args.get("start") or "").strip()
    end_s     = (request.args.get("end") or "").strip()

    if not camera_id or not start_s or not end_s:
        return jsonify({"success": False,
                        "message": "camera_id, start, end wajib"}), 400

    cam = dbmod.fetch_camera(camera_id, attach_roles=False)
    if not cam:
        return jsonify({"success": False, "message": "Kamera tidak ditemukan"}), 404

    role = session.get("role", "public")
    if not dbmod.has_camera_access(camera_id, role):
        return jsonify({"success": False, "message": "Akses ditolak"}), 403

    dvr_name = cam.get("dvr")
    if not dvr_name:
        return jsonify({"success": False,
                        "message": "Kamera bukan tipe NVR/DVR"}), 400

    try:
        dvrs = dbmod.fetch_all_dvr()
        dvr_row = next((d for d in dvrs if d.get("name") == dvr_name), None)
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

    if not dvr_row:
        return jsonify({"success": False,
                        "message": f"DVR '{dvr_name}' tidak ditemukan"}), 404

    # Hikvision: tidak support loadfile.cgi
    vendor = _dvr_vendor(dvr_row)
    if vendor == "hikvision":
        return jsonify({
            "success": False,
            "message": "Download langsung tidak tersedia untuk Hikvision; gunakan playback session.",
        }), 400

    try:
        start_dt = datetime.fromisoformat(start_s.replace("Z", ""))
        end_dt   = datetime.fromisoformat(end_s.replace("Z", ""))
    except ValueError:
        return jsonify({"success": False, "message": "Format datetime salah"}), 400

    if end_dt <= start_dt:
        return jsonify({"success": False,
                        "message": "end harus > start"}), 400

    max_duration = timedelta(hours=2)
    if end_dt - start_dt > max_duration:
        end_dt = start_dt + max_duration

    url = _dahua_loadfile_url(dvr_row, cam["channel"], start_dt, end_dt)

    user   = getattr(Config, "DAHUA_USER", None) or dvr_row.get("user")
    passwd = getattr(Config, "DAHUA_PASSWORD", None) or dvr_row.get("password")

    try:
        upstream = requests.get(
            url,
            auth=HTTPDigestAuth(user, passwd),
            stream=True,
            timeout=30,
        )
    except requests.exceptions.RequestException as e:
        return Response(f"DVR tidak dapat dihubungi: {e}", status=502)

    if upstream.status_code != 200:
        return Response(f"DVR HTTP {upstream.status_code}",
                        status=upstream.status_code)

    fname = f"{cam['name']}_{start_dt:%Y%m%d_%H%M%S}.dav".replace(" ", "_")

    headers = {
        "Content-Type": "video/x-dav",
        "Content-Disposition": f'attachment; filename="{fname}"',
        "Cache-Control": "no-cache",
    }
    if "Content-Length" in upstream.headers:
        headers["Content-Length"] = upstream.headers["Content-Length"]

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=128 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return Response(stream_with_context(generate()), status=200, headers=headers)


# ====================================================================
# PLAYBACK SESSION (FFmpeg — Dahua + Hikvision)
# ====================================================================
@app.route("/api/playback/session", methods=["POST"])
@login_required
def api_playback_session():
    data      = request.get_json(silent=True) or {}
    camera_id = data.get("camera_id")
    start_s   = data.get("start")
    end_s     = data.get("end")

    if not camera_id or not start_s or not end_s:
        return jsonify({"success": False,
                        "message": "camera_id, start, end wajib"}), 400

    cam = dbmod.fetch_camera(camera_id, attach_roles=False)
    if not cam:
        return jsonify({"success": False, "message": "Kamera tidak ditemukan"}), 404

    role = session.get("role", "public")
    if not dbmod.has_camera_access(camera_id, role):
        return jsonify({"success": False, "message": "Akses ditolak"}), 403

    dvr_name = cam.get("dvr")
    if not dvr_name:
        return jsonify({"success": False,
                        "message": "Kamera bukan tipe NVR/DVR"}), 400

    try:
        dvrs = dbmod.fetch_all_dvr()
        dvr_row = next((d for d in dvrs if d.get("name") == dvr_name), None)
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500
    if not dvr_row:
        return jsonify({"success": False,
                        "message": f"DVR '{dvr_name}' tidak ada di DB"}), 404

    try:
        start_dt = datetime.fromisoformat(start_s.replace("Z", ""))
        end_dt   = datetime.fromisoformat(end_s.replace("Z", ""))
    except ValueError:
        return jsonify({"success": False,
                        "message": "Format datetime salah"}), 400
    if end_dt <= start_dt:
        return jsonify({"success": False, "message": "end harus > start"}), 400

    if end_dt - start_dt > timedelta(hours=2):
        end_dt = start_dt + timedelta(hours=2)

    vendor = _dvr_vendor(dvr_row)

    # === HIKVISION: RTSP playback URL ===
    if vendor == "hikvision":
        user = dvr_row.get("user") or getattr(Config, "HIKVISION_USER", "admin")
        pwd  = dvr_row.get("password") or getattr(Config, "HIKVISION_PASSWORD", "")
        pb = hikvision_playback.HikvisionPlayback(
            dvr_row["ip"], dvr_row.get("port") or 80, user, pwd
        )
        loadfile_url = pb.build_playback_rtsp(
            cam["channel"], start_dt, end_dt, stream=1
        )
    else:
        # Dahua: loadfile.cgi dengan embedded credentials
        user   = getattr(Config, "DAHUA_USER", None) or dvr_row.get("user") or ""
        passwd = getattr(Config, "DAHUA_PASSWORD", None) or dvr_row.get("password") or ""

        ip   = (dvr_row.get("ip") or "").strip()
        port = int(dvr_row.get("port") or 80)

        def fmt(dt):
            return f"{dt.year}-{dt.month}-{dt.day} {dt.hour}:{dt.minute}:{dt.second}"

        loadfile_url = (
            f"http://{_url_quote(user, safe='')}:{_url_quote(passwd, safe='')}"
            f"@{ip}:{port}/cgi-bin/loadfile.cgi"
            f"?action=startLoad"
            f"&channel={cam['channel']}"
            f"&startTime={_url_quote(fmt(start_dt), safe='')}"
            f"&endTime={_url_quote(fmt(end_dt), safe='')}"
        )
    # === END HIKVISION ===

    try:
        s = pb_manager.create(camera_id, loadfile_url, start_dt, end_dt)
    except FileNotFoundError:
        return jsonify({"success": False,
                        "message": "ffmpeg tidak ditemukan di server"}), 500
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

    return jsonify({
        "success":     True,
        "vendor":      vendor,
        "session_id":  s.sid,
        "hls_url":     f"/api/playback/hls/{s.sid}/index.m3u8",
        "start":       start_dt.isoformat(),
        "end":         end_dt.isoformat(),
    })


@app.route("/api/playback/session/<sid>", methods=["DELETE"])
@login_required
def api_playback_session_stop(sid):
    pb_manager.stop(sid)
    return jsonify({"success": True})


@app.route("/api/playback/hls/<sid>/<path:fname>")
@login_required
def api_playback_hls(sid, fname):
    from playback_manager import manager as _pm
    all_sids = list(_pm.sessions.keys())
    app.logger.warning(
        f"[PB HLS] MASUK sid={sid} fname={fname} | sesi_aktif={all_sids}"
    )

    s = pb_manager.get(sid)
    if not s:
        app.logger.warning(f"[PB HLS] Session '{sid}' TIDAK ADA.")
        abort(404)
    s.touch()

    safe = os.path.normpath(fname).replace("\\", "/").lstrip("/")
    if ".." in safe.split("/"):
        abort(400)

    fullpath = os.path.join(s.dir, safe)
    exists = os.path.isfile(fullpath)
    dir_files = os.listdir(s.dir) if os.path.isdir(s.dir) else "DIR_TIDAK_ADA"
    app.logger.warning(
        f"[PB HLS] dir={s.dir} exists={exists} "
        f"alive={s.is_alive()} files={dir_files}"
    )

    if not exists:
        abort(404)

    if safe.endswith(".m3u8"):
        mime = "application/vnd.apple.mpegurl"
    elif safe.endswith(".ts"):
        mime = "video/mp2t"
    else:
        mime = "application/octet-stream"

    resp = send_file(fullpath, mimetype=mime, conditional=True)
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


def _make_playback_proxy_token(camera_id, start_dt, end_dt):
    msg = f"{camera_id}:{start_dt.isoformat()}:{end_dt.isoformat()}".encode()
    return _hmac.new(_mobile_secret(), msg, hashlib.sha256).hexdigest()


@app.route("/internal/playback/<int:camera_id>")
def internal_playback(camera_id):
    token    = request.args.get("token", "")
    start_s  = request.args.get("start", "")
    end_s    = request.args.get("end", "")

    if not token or not start_s or not end_s:
        abort(400)

    try:
        start_dt = datetime.fromisoformat(start_s)
        end_dt   = datetime.fromisoformat(end_s)
    except ValueError:
        abort(400)

    expected = _make_playback_proxy_token(camera_id, start_dt, end_dt)
    if not _hmac.compare_digest(expected, token):
        abort(403)

    cam = dbmod.fetch_camera(camera_id, attach_roles=False)
    if not cam:
        abort(404)

    dvr_name = cam.get("dvr")
    if not dvr_name:
        abort(400)

    try:
        dvrs = dbmod.fetch_all_dvr()
        dvr_row = next((d for d in dvrs if d.get("name") == dvr_name), None)
    except Exception:
        abort(500)

    if not dvr_row:
        abort(404)

    url = _dahua_loadfile_url(dvr_row, cam["channel"], start_dt, end_dt)
    user   = getattr(Config, "DAHUA_USER", None) or dvr_row.get("user")
    passwd = getattr(Config, "DAHUA_PASSWORD", None) or dvr_row.get("password")

    try:
        upstream = requests.get(
            url,
            auth=HTTPDigestAuth(user, passwd),
            stream=True,
            timeout=30,
        )
    except requests.exceptions.RequestException as e:
        return Response(f"DVR error: {e}", status=502)

    if upstream.status_code != 200:
        return Response(f"DVR HTTP {upstream.status_code}",
                        status=upstream.status_code)

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=128 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return Response(
        stream_with_context(generate()),
        status=200,
        headers={
            "Content-Type": "video/x-dav",
            "Cache-Control": "no-cache",
        },
    )


if __name__ == "__main__":
    with open("logs/server.pid", "w") as f:
        f.write(str(os.getpid()))

    logger.info("=" * 60)
    logger.info("SERVER STARTING")
    logger.info(f"PID: {os.getpid()}")
    logger.info(f"Python: {sys.version}")
    logger.info("=" * 60)

    if Config.DB_ENABLED:
        try:
            dbmod.ensure_default_admin()
        except Exception as e:
            print(f"[WARN] Bootstrap admin: {e}")

    threading.Thread(target=_startup_sync_cameras, daemon=True).start()
    threading.Thread(target=_monitor_loop, daemon=True).start()
    threading.Thread(target=_resource_monitor, daemon=True).start()
    logger.info(f"[MONITOR] Background monitor aktif (interval {MONITOR_INTERVAL}s)")

    threading.Thread(target=_dvr_monitor_loop, daemon=True).start()
    logger.info(f"[DVR MONITOR] Thread aktif (interval {DVR_CHECK_INTERVAL}s)")

    try:
        from waitress import serve
        serve(
            app,
            host="0.0.0.0",
            port=5001,
            threads=8,
            channel_timeout=120,
            cleanup_interval=30,
            connection_limit=1000,
            log_socket_errors=True,
        )
    except Exception as e:
        logger.critical(f"FATAL: Server gagal start: {e}", exc_info=True)
        raise
    finally:
        logger.info("SERVER STOPPED")
"""
Database layer - PyMySQL
Semua query ke tabel `dvr`, `cameras`, `users`, `records` ada di sini.
Row di-map ke dict yang field-nya kompatibel dengan template Jinja
(dashboard.html, dvr.html, camera.html, landing.html).
"""
import pymysql
from pymysql.cursors import DictCursor
from contextlib import contextmanager
from werkzeug.security import generate_password_hash, check_password_hash

from config import Config


# ====================================================================
# Koneksi
# ====================================================================
def get_connection():
    return pymysql.connect(
        host=Config.DB_HOST,
        port=Config.DB_PORT,
        user=Config.DB_USER,
        password=Config.DB_PASSWORD,
        database=Config.DB_NAME,
        charset=Config.DB_CHARSET,
        cursorclass=DictCursor,
        autocommit=False,
    )


@contextmanager
def get_cursor(commit=False):
    """Context manager untuk cursor. Auto rollback jika error."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            yield cur
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ====================================================================
# Helpers / Mapper
# ====================================================================

def _map_camera(row: dict) -> dict:
    """Map row dari tabel `cameras` → dict yang dipakai template."""
    return {
        "id":            row["id"],
        "name":          row["name"],
        "location":      row.get("location"),
        "rtsp_url":      row.get("rtsp_url"),
        "nvr_dvr":       row.get("nvr_dvr") or "ipcam",
        "channel":       row.get("channel") or 1,
        "codec":         row.get("codec") or "auto",
        "is_public":     bool(row.get("is_public")),
        "is_active":     bool(row.get("is_active")),
        "active":        bool(row.get("is_active")),  # alias utk camera.html
        "lat":           float(row["lat"]) if row.get("lat") is not None else None,
        "lng":           float(row["lng"]) if row.get("lng") is not None else None,
        "youtube_embed": row.get("youtube_embed"),
        "created_at":    row.get("created_at"),
        "readers":       0,  # belum ada tabel viewers, default 0
    }


def _map_user(row: dict) -> dict:
    return {
        "id":         row["id"],
        "username":   row["username"],
        "password":   row["password"],
        "role":       row["role"],
        "created_at": row.get("created_at"),
    }


# ====================================================================
# DVR
# ====================================================================

DVR_STATUS_MAP = {
    0: "offline",
    1: "online",
    2: "recording",
    3: "idle",
}


def _map_dvr(row: dict) -> dict:
    """Map row dari tabel `dvr` → dict yang dipakai template."""
    status_int = row.get("status") or 0
    return {
        "id":         row["id"],
        "name":       row["name"],
        "lokasi":     row.get("lokasi"),
        "location":   row.get("lokasi"),        # alias utk dashboard.html
        "ip":         row.get("ip"),
        "port":       row.get("port"),
        "channel":    row.get("channel") or 0,
        "channels":   row.get("channel") or 0,  # alias utk dvr.html
        "cameras":    row.get("channel") or 0,  # alias utk dashboard.html
        "vendor":     row.get("vendor"),
        "status":     DVR_STATUS_MAP.get(status_int, "offline"),
        "status_int": status_int,
        "active":     status_int == 1,
    }

# db.py - tambahkan di bagian DVR section

def update_dvr_status(dvr_id: int, status_int: int, reason: str = None) -> bool:
    """
    Update kolom `status` DVR.
    status_int: 0=offline, 1=online, 2=recording, 3=idle
    """
    with get_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE dvr SET status = %s WHERE id = %s",
            (status_int, dvr_id),
        )
        return cur.rowcount > 0


def get_last_checked_all_dvr() -> dict:
    """
    Ambil status terakhir semua DVR sebagai dict {id: {status, ...}}.
    (Kalau Anda tidak punya kolom `last_check`, ini return status saja.)
    """
    result = {}
    for dvr in fetch_all_dvr():
        result[dvr["id"]] = {
            "id":     dvr["id"],
            "name":   dvr.get("name"),
            "ip":     dvr.get("ip"),
            "port":   dvr.get("port"),
            "status": dvr.get("status_int", 0),
            "online": dvr.get("status_int") == 1,
        }
    return result

def fetch_all_dvr():
    with get_cursor() as cur:
        cur.execute("""
            SELECT id, name, lokasi, channel, ip, port, status, vendor
            FROM dvr
            ORDER BY name ASC
        """)
        rows = cur.fetchall()
    return [_map_dvr(r) for r in rows]

def fetch_dvr(dvr_id: int):
    with get_cursor() as cur:
        cur.execute("""
            SELECT id, name, lokasi, channel, ip, port, status, vendor
            FROM dvr WHERE id = %s LIMIT 1
        """, (dvr_id,))
        row = cur.fetchone()
    return _map_dvr(row) if row else None

def add_dvr(name, lokasi=None, channel=0, ip=None, port=554,
            status=0, vendor=None):
    with get_cursor(commit=True) as cur:
        cur.execute("""
            INSERT INTO dvr (name, lokasi, channel, ip, port, status, vendor)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (name, lokasi, channel, ip, port, status, vendor))
        return cur.lastrowid

def delete_dvr(dvr_id: int) -> int:
    with get_cursor(commit=True) as cur:
        cur.execute("DELETE FROM dvr WHERE id = %s", (dvr_id,))
        return cur.rowcount

def update_dvr(dvr_id: int, **fields) -> bool:
    """
    Update kolom yang dikirim saja (partial update).
    Return True jika ada row yang ter-update.
    """
    allowed = {"name", "lokasi", "channel", "ip", "port", "status", "vendor"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return False

    set_clause = ", ".join(f"`{k}` = %s" for k in updates)
    values = list(updates.values()) + [dvr_id]

    with get_cursor(commit=True) as cur:
        cur.execute(f"UPDATE dvr SET {set_clause} WHERE id = %s", values)
        return cur.rowcount > 0


# ====================================================================
# Users
# ====================================================================
def get_user_by_username(username: str):
    with get_cursor() as cur:
        cur.execute("""
            SELECT id, username, password, role, created_at
            FROM users WHERE username = %s LIMIT 1
        """, (username,))
        row = cur.fetchone()
    return _map_user(row) if row else None


def fetch_all_users():
    with get_cursor() as cur:
        cur.execute("""
            SELECT id, username, password, role, created_at
            FROM users ORDER BY id ASC
        """)
        rows = cur.fetchall()
    return [_map_user(r) for r in rows]


def create_user(username: str, raw_password: str, role: str = "public"):
    hashed = generate_password_hash(raw_password)
    with get_cursor(commit=True) as cur:
        cur.execute("""
            INSERT INTO users (username, password, role)
            VALUES (%s, %s, %s)
        """, (username, hashed, role))
        return cur.lastrowid


def verify_user(username: str, raw_password: str):
    """Return dict user kalau valid, None kalau tidak."""
    user = get_user_by_username(username)
    if not user:
        return None
    if check_password_hash(user["password"], raw_password):
        return user
    return None

def update_user(user_id: int, username=None, password=None, role=None) -> bool:
    """
    Partial update user.
    - username: string baru (opsional)
    - password: password MENTAH (opsional) — otomatis di-hash
    - role: 'admin' | 'public' (opsional)
    Return True kalau ada row ter-update.
    """
    updates = {}
    if username is not None and username.strip():
        updates["username"] = username.strip()
    if password is not None and password != "":
        updates["password"] = generate_password_hash(password)
    if role is not None:
        if role not in ("admin", "public","supervisor","security"):
            raise ValueError("Role harus 'admin' atau 'public'")
        updates["role"] = role

    if not updates:
        return False

    set_clause = ", ".join(f"`{k}` = %s" for k in updates)
    values = list(updates.values()) + [user_id]

    with get_cursor(commit=True) as cur:
        cur.execute(f"UPDATE users SET {set_clause} WHERE id = %s", values)
        return cur.rowcount > 0


def delete_user(user_id: int) -> int:
    with get_cursor(commit=True) as cur:
        cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
        return cur.rowcount


def get_user_by_id(user_id: int):
    with get_cursor() as cur:
        cur.execute("""
            SELECT id, username, password, role, created_at
            FROM users WHERE id = %s LIMIT 1
        """, (user_id,))
        row = cur.fetchone()
    return _map_user(row) if row else None


def count_admins() -> int:
    with get_cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM users WHERE role = 'admin'")
        return cur.fetchone()["c"] or 0

# ====================================================================
# Stats (untuk dashboard & landing)
# ====================================================================
def compute_stats() -> dict:
    with get_cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM dvr")
        total_dvr = cur.fetchone()["c"] or 0

        cur.execute("SELECT COUNT(*) AS c FROM dvr WHERE status = 1")
        online_dvr = cur.fetchone()["c"] or 0

        cur.execute("SELECT COUNT(*) AS c FROM cameras")
        total_cameras = cur.fetchone()["c"] or 0

        cur.execute("SELECT COUNT(*) AS c FROM cameras WHERE is_active = 1")
        online_cameras = cur.fetchone()["c"] or 0

        cur.execute("""
            SELECT COUNT(DISTINCT lokasi) AS c
            FROM dvr WHERE lokasi IS NOT NULL AND lokasi <> ''
        """)
        total_locations = cur.fetchone()["c"] or 0

    pct = round(online_cameras / total_cameras * 100, 1) if total_cameras else 0.0

    return {
        "device_online":       online_dvr,
        "device_growth":       0.0,          # butuh data historis, dummy = 0
        "total_dvr":           total_dvr,
        "total_locations":     total_locations,
        "total_cameras":       total_cameras,
        "cameras_online":      online_cameras,
        "cameras_percentage":  pct,
    }

def fetch_dvr_with_cameras(cameras_override=None):
    dvrs    = fetch_all_dvr()
    cameras = cameras_override if cameras_override is not None else fetch_all_cameras()

    # Group cameras by DVR name (case-insensitive, trim spasi)
    cam_by_dvr = {}
    orphans    = []
    for cam in cameras:
        dvr_name = (cam.get("dvr") or "").strip().lower()
        if dvr_name:
            cam_by_dvr.setdefault(dvr_name, []).append(cam)
        else:
            orphans.append(cam)

    result = []
    for d in dvrs:
        key = (d.get("name") or "").strip().lower()
        dvr_cams = cam_by_dvr.get(key, [])

        # Sort kamera berdasarkan channel
        dvr_cams.sort(key=lambda c: c.get("channel") or 0)

        # Index kamera by channel untuk lookup cepat di template
        cams_by_channel = {c.get("channel"): c for c in dvr_cams}

        total_channels  = d.get("channel") or 0
        online_channels = sum(1 for c in dvr_cams if c.get("is_active"))
        used_channels   = len(dvr_cams)

        result.append({
            **d,
            "cameras":          dvr_cams,
            "cams_by_channel":  cams_by_channel,
            "total_channels":   total_channels,
            "online_channels":  online_channels,
            "used_channels":    used_channels,
            "empty_channels":   max(total_channels - used_channels, 0),
        })

    # Kamera yatim: `dvr` tidak match dengan DVR manapun
    assigned_names = {d.get("name", "").strip().lower() for d in dvrs}
    for cam in cameras:
        nm = (cam.get("dvr") or "").strip().lower()
        if nm and nm not in assigned_names:
            orphans.append(cam)

    return result, orphans
    
# ====================================================================
# Bootstrap: pastikan ada minimal 1 admin
# ====================================================================
def ensure_default_admin():
    """Kalau tabel users kosong, buat admin default."""
    try:
        with get_cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM users")
            if (cur.fetchone()["c"] or 0) > 0:
                return
        create_user(
            Config.DEFAULT_ADMIN_USERNAME,
            Config.DEFAULT_ADMIN_PASSWORD,
            role="admin",
        )
        print(f"[DB] Default admin dibuat: "
              f"{Config.DEFAULT_ADMIN_USERNAME} / {Config.DEFAULT_ADMIN_PASSWORD}")
    except Exception as e:
        print(f"[DB] Gagal bootstrap admin: {e}")

# ====================================================================
# Cameras
# ====================================================================

def _map_camera(row: dict) -> dict:
    return {
        "id":            row["id"],
        "name":          row["name"],
        "mtx_path":      row.get("mtx_path"),       # ← HARUS ADA
        "location":      row.get("location"),
        "rtsp_url":      row.get("rtsp_url"),
        "nvr_dvr":       row.get("nvr_dvr") or "ipcam",
        "channel":       row.get("channel") or 1,
        "codec":         row.get("codec") or "auto",
        "is_public":     bool(row.get("is_public")),
        "is_active":     bool(row.get("is_active")),
        "active":        bool(row.get("is_active")),
        "lat":           float(row["lat"]) if row.get("lat") is not None else None,
        "lng":           float(row["lng"]) if row.get("lng") is not None else None,
        "youtube_embed": row.get("youtube_embed"),
        "dvr":           row.get("dvr"),
        "created_at":    row.get("created_at"),
        "readers":       0,
    }

def fetch_all_cameras(only_public: bool = False):
    sql = """
        SELECT id, name, mtx_path, location, rtsp_url, nvr_dvr, channel, codec,
               is_public, is_active, lat, lng, youtube_embed, dvr, created_at
        FROM cameras
    """
    params = ()
    if only_public:
        sql += " WHERE is_public = 1"
    sql += " ORDER BY name ASC"

    with get_cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [_map_camera(r) for r in rows]

def fetch_camera(camera_id: int, attach_roles: bool = True):
    with get_cursor() as cur:
        cur.execute("""
            SELECT id, name, mtx_path, location, rtsp_url, nvr_dvr, channel, codec,
                   is_public, is_active, lat, lng, youtube_embed, dvr, created_at
            FROM cameras WHERE id = %s LIMIT 1
        """, (camera_id,))
        row = cur.fetchone()
    cam = _map_camera(row) if row else None
    if cam and attach_roles:
        _attach_roles([cam])
    return cam

def add_camera(name, rtsp_url, location=None, nvr_dvr="ipcam",
               channel=1, codec="auto", is_public=True, is_active=True,
               lat=None, lng=None, youtube_embed=None, dvr=None):
    with get_cursor(commit=True) as cur:
        cur.execute("""
            INSERT INTO cameras
              (name, location, rtsp_url, nvr_dvr, channel, codec,
               is_public, is_active, lat, lng, youtube_embed, dvr)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (name, location, rtsp_url, nvr_dvr, channel, codec,
              int(bool(is_public)), int(bool(is_active)),
              lat, lng, youtube_embed, dvr))
        return cur.lastrowid

def delete_camera(camera_id: int, **fields) -> bool:
    try:
        with get_cursor(commit=True) as cur:
            cur.execute("DELETE FROM cameras WHERE id = %s",
                (camera_id),
            )
            return True	
    except:
        return True
        
def update_camera(camera_id: int, **fields) -> bool:
    """Partial update. Return True jika ada row ter-update."""
    allowed = {
        "name", "location", "rtsp_url", "nvr_dvr", "channel", "codec",
        "is_public", "is_active", "lat", "lng", "youtube_embed", "dvr",
    }
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return False

    # Konversi boolean → int untuk kolom tinyint
    if "is_public" in updates:
        updates["is_public"] = int(bool(updates["is_public"]))
    if "is_active" in updates:
        updates["is_active"] = int(bool(updates["is_active"]))

    set_clause = ", ".join(f"`{k}` = %s" for k in updates)
    values = list(updates.values()) + [camera_id]

    with get_cursor(commit=True) as cur:
        cur.execute(f"UPDATE cameras SET {set_clause} WHERE id = %s", values)
        return cur.rowcount > 0

def set_camera_mtx_path(camera_id: int, mtx_path: str) -> bool:
    with get_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE cameras SET mtx_path = %s WHERE id = %s",
            (mtx_path, camera_id),
        )
        return cur.rowcount > 0
# ====================================================================
# Fallback data (kalau DB_ENABLED = false)
# ====================================================================
FALLBACK_STATS = {
    "device_online": 0, "device_growth": 0.0,
    "total_dvr": 0, "total_locations": 0,
    "total_cameras": 0, "cameras_online": 0, "cameras_percentage": 0.0,
}
# ====================================================================
# CAMERA ACCESS (per-role visibility)
# ====================================================================
ALL_ROLES = ("admin", "supervisor", "public","security")


def _attach_roles(cameras: list) -> list:
    """Lampirkan field 'roles' (list of str) ke setiap camera dict."""
    if not cameras:
        return cameras
    ids = [c["id"] for c in cameras]
    placeholders = ",".join(["%s"] * len(ids))
    with get_cursor() as cur:
        cur.execute(
            f"SELECT camera_id, role FROM camera_access "
            f"WHERE camera_id IN ({placeholders})",
            ids,
        )
        rows = cur.fetchall()
    role_map = {}
    for r in rows:
        role_map.setdefault(r["camera_id"], []).append(r["role"])
    for c in cameras:
        c["roles"] = role_map.get(c["id"], [])
    return cameras


def fetch_cameras_for_role(role: str, attach_roles: bool = True):
    """
    Ambil kamera yang visible untuk role tertentu.
    - admin    : semua kamera (tidak difilter)
    - lainnya  : hanya kamera dengan entry di camera_access
    """
    if role == "admin":
        cameras = fetch_all_cameras()
    else:
        sql = """
            SELECT c.id, c.name, c.mtx_path, c.location, c.rtsp_url, c.nvr_dvr,
                   c.channel, c.codec, c.is_public, c.is_active, c.lat, c.lng,
                   c.youtube_embed, c.dvr, c.created_at
            FROM cameras c
            INNER JOIN camera_access ca ON ca.camera_id = c.id
            WHERE ca.role = %s
            ORDER BY c.name ASC
        """
        with get_cursor() as cur:
            cur.execute(sql, (role,))
            rows = cur.fetchall()
        cameras = [_map_camera(r) for r in rows]

    if attach_roles:
        _attach_roles(cameras)
    return cameras


def get_camera_roles(camera_id: int) -> list:
    """Ambil list role yang punya akses ke kamera."""
    with get_cursor() as cur:
        cur.execute(
            "SELECT role FROM camera_access WHERE camera_id = %s ORDER BY role",
            (camera_id,),
        )
        return [r["role"] for r in cur.fetchall()]


def set_camera_roles(camera_id: int, roles: list) -> bool:
    """Replace role akses untuk kamera. 'admin' selalu dipaksa ada."""
    roles_clean = set(r for r in (roles or []) if r in ALL_ROLES)
    roles_clean.add("admin")

    with get_cursor(commit=True) as cur:
        cur.execute("DELETE FROM camera_access WHERE camera_id = %s", (camera_id,))
        values = [(camera_id, r) for r in roles_clean]
        cur.executemany(
            "INSERT INTO camera_access (camera_id, role) VALUES (%s, %s)",
            values,
        )
    return True


def has_camera_access(camera_id: int, role: str) -> bool:
    """Cek apakah role punya akses ke kamera. Admin selalu True."""
    if role == "admin":
        return True
    with get_cursor() as cur:
        cur.execute(
            "SELECT 1 FROM camera_access WHERE camera_id = %s AND role = %s LIMIT 1",
            (camera_id, role),
        )
        return cur.fetchone() is not None


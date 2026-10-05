import os

class Config:
    # ===================== Flask =====================
    SECRET_KEY = os.environ.get("SECRET_KEY", "Tr@nsdigi53$")

    # ===================== Database MySQL =====================
    DB_ENABLED  = os.environ.get("DB_ENABLED", "1") == "1"
    DB_HOST     = os.environ.get("DB_HOST", "127.0.0.1")
    DB_PORT     = int(os.environ.get("DB_PORT", 3306))
    DB_USER     = os.environ.get("DB_USER", "root")
    DB_PASSWORD = os.environ.get("DB_PASSWORD", "Tr@nsdigi53$_DB")
    DB_NAME     = os.environ.get("DB_NAME", "webcctv")
    DB_CHARSET  = os.environ.get("DB_CHARSET", "utf8mb4")

    # Bootstrap admin pertama kali (hanya jika tabel users kosong)
    DEFAULT_ADMIN_USERNAME = os.environ.get("DEFAULT_ADMIN_USERNAME", "admin")
    DEFAULT_ADMIN_PASSWORD = os.environ.get("DEFAULT_ADMIN_PASSWORD", "admin123")
    
    # Config.py — tambahkan
    HIKVISION_USER = os.getenv("HIKVISION_USER", "admin")
    HIKVISION_PASSWORD = os.getenv("HIKVISION_PASSWORD", "")
    HIKVISION_HTTP_PORT = int(os.getenv("HIKVISION_HTTP_PORT", "80"))
    HIKVISION_RTSP_PORT = int(os.getenv("HIKVISION_RTSP_PORT", "554"))

    # ===================== MediaMTX =====================
    MEDIAMTX_HLS = os.environ.get("MEDIAMTX_HLS", "http://127.0.0.1:8888")
    MEDIAMTX_API = os.environ.get("MEDIAMTX_API", "http://127.0.0.1:9997")

    # Kredensial proxy HLS
    MEDIAMTX_USER = os.environ.get("MEDIAMTX_USER", "flask_proxy")
    MEDIAMTX_PASS = os.environ.get("MEDIAMTX_PASS", "Tr@nsdigi53$_proxy")
    
    # ===================== SIKAWAN SSO =====================
    SIKAWAN_ENABLED  = os.environ.get("SIKAWAN_ENABLED", "1") == "1"
    SIKAWAN_API_URL  = os.environ.get("SIKAWAN_API_URL","http://192.168.10.8/sikawan-api/public/api/v2/login")
    SIKAWAN_TIMEOUT  = int(os.environ.get("SIKAWAN_TIMEOUT", "10"))  # detik

    # Auto-create user lokal dari SIKAWAN jika belum ada
    SIKAWAN_AUTO_CREATE = os.environ.get("SIKAWAN_AUTO_CREATE", "1") == "1"

    # Default role untuk user yang auto-created dari SIKAWAN
    # Pilihan: "public" (view only) atau "admin" (full access)
    SIKAWAN_DEFAULT_ROLE = os.environ.get("SIKAWAN_DEFAULT_ROLE", "public")

    # Daftar username SIKAWAN yang otomatis jadi admin (comma-separated)
    # Contoh: "aries.setiyawan,admin.it,supervisor"
    SIKAWAN_ADMIN_USERS = [
        u.strip().lower()
        for u in os.environ.get("SIKAWAN_ADMIN_USERS", "").split(",")
        if u.strip()
    ]

    # Fallback ke login lokal kalau SIKAWAN down
    SIKAWAN_FALLBACK_LOCAL = os.environ.get("SIKAWAN_FALLBACK_LOCAL", "1") == "1"

    # ===================== Kamera & Token =====================
    TOTAL_CAMERAS       = 16
    TOKEN_TTL_SECONDS   = 24 * 3600
    CAMERA_NAMES = {
        1: "Gerbang Depan", 2: "Gerbang Belakang", 3: "Lobby Utama",
        4: "Ruang IGD", 5: "Ruang ICU", 6: "Ruang Operasi",
        7: "Koridor Lantai 1", 8: "Koridor Lantai 2",
        9: "Parkiran Motor", 10: "Parkiran Mobil",
        11: "Gudang Obat", 12: "Ruang Administrasi",
        13: "Kantin", 14: "Musholla", 15: "Ruang Server",
        16: "Halaman Belakang",
    }
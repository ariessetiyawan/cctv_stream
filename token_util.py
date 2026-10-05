import hmac
import hashlib
import time

# Kunci rahasia — ganti di production!
SECRET_KEY = b"Tr"


def generate_token(path: str, expires: int) -> str:
    """
    Generate HMAC-SHA256 token untuk path tertentu.
    
    Format: HMAC(secret, f"{path}:{expires}")
    """
    message = f"{path}:{expires}".encode()
    return hmac.new(SECRET_KEY, message, hashlib.sha256).hexdigest()


def verify_token(path: str, expires: int, token: str) -> bool:
    """
    Verifikasi token: cek kedaluwarsa + validitas HMAC.
    """
    # Cek kedaluwarsa
    if time.time() > expires:
        return False
    
    # Cek HMAC (constant-time comparison)
    expected = generate_token(path, expires)
    return hmac.compare_digest(expected, token)
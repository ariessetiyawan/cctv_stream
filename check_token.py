# check_token.py
import base64, hmac, hashlib, json, time
from datetime import datetime

TOKEN = "eyJ1aWQiOjcsInUiOiIwODEzMzA0OTY4ODQiLCJyIjoicHVibGljIiwiZXhwIjoxNzkwMzg2NTQ5fQ._-0_LlmOyWcEbvZoEYNQmR15MP1qgiGxOM5XYVrSYqs"

# Import Config
from config import Config

def b64url_decode(s):
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)

print("=" * 60)
print("TOKEN VERIFICATION")
print("=" * 60)

# 1. Ambil SECRET_KEY
secret = Config.SECRET_KEY
if isinstance(secret, str):
    secret = secret.encode()
print(f"SECRET_KEY (first 16 bytes hex): {secret[:16].hex()}")

# 2. Split token
try:
    body, sig_b64 = TOKEN.split(".", 1)
    print(f"Payload (raw b64): {body}")
    print(f"Signature (raw b64): {sig_b64}")
except Exception as e:
    print(f"? Cannot split token: {e}")
    exit(1)

# 3. Decode payload
try:
    payload = json.loads(b64url_decode(body))
    print(f"Payload (JSON): {json.dumps(payload, indent=2)}")
except Exception as e:
    print(f"? Cannot decode payload: {e}")
    exit(1)

# 4. Cek expired
exp = int(payload.get("exp", 0))
now = int(time.time())
print(f"Expired at: {datetime.fromtimestamp(exp)}")
print(f"Now       : {datetime.fromtimestamp(now)}")
if exp < now:
    print(f"? TOKEN EXPIRED (selisih {now - exp} detik)")
    exit(1)
else:
    print(f"? Token belum expired (sisa {exp - now} detik / {(exp-now)//3600} jam)")

# 5. Verify signature
try:
    expected_sig = hmac.new(secret, body.encode(), hashlib.sha256).digest()
    received_sig = b64url_decode(sig_b64)
    print(f"Expected sig: {expected_sig.hex()}")
    print(f"Received sig: {received_sig.hex()}")
    if hmac.compare_digest(expected_sig, received_sig):
        print("? SIGNATURE VALID")
    else:
        print("? SIGNATURE MISMATCH -> SECRET_KEY berbeda!")
        print("   Kemungkinan SECRET_KEY di generate ulang saat restart.")
except Exception as e:
    print(f"? Signature verification error: {e}")
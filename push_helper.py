"""
Web Push / VAPID helper dla ComfyUI Mobile.
Używa cryptography (bez PyJWT – PyJWT dodaje 'typ:JWT' co łamie RFC 8292 / Apple APNs).
"""
import json, base64, time, os, struct
import requests
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import hashes

# ─── Klucze VAPID ───────────────────────────────────────────────────────────

def generate_vapid_keys() -> dict:
    """Generuje nową parę kluczy VAPID (jednorazowo)."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key  = private_key.public_key()

    # Prywatny jako raw 32 bajty (base64url)
    priv_numbers = private_key.private_numbers()
    priv_bytes   = priv_numbers.private_value.to_bytes(32, 'big')

    # Publiczny jako uncompressed point 65 bajtów (04 || x || y)
    pub_numbers = public_key.public_numbers()
    pub_bytes   = b'\x04' + pub_numbers.x.to_bytes(32, 'big') + pub_numbers.y.to_bytes(32, 'big')

    return {
        "private_key": _b64url(priv_bytes),
        "public_key":  _b64url(pub_bytes),
    }

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()

def _b64url_decode(s: str) -> bytes:
    pad = 4 - len(s) % 4
    if pad != 4:
        s += '=' * pad
    return base64.urlsafe_b64decode(s)

# ─── Budowanie nagłówka Authorization VAPID ─────────────────────────────────

def _build_vapid_auth(endpoint: str, private_key_b64: str, public_key_b64: str,
                       contact: str = "mailto:admin@push.local") -> str:
    """Zwraca wartość nagłówka Authorization: vapid ...
    Apple APNs wymaga:
      - exp <= iat + 3600 (max 1 godzina)
      - sub = poprawny mailto lub https URL
      - format: vapid t=TOKEN, k=KEY (spacja po przecinku – RFC 8292)
    """
    from urllib.parse import urlparse
    origin = "{0.scheme}://{0.netloc}".format(urlparse(endpoint))

    now = int(time.time())
    payload = {
        "aud": origin,
        "iat": now,
        "exp": now + 3600,   # max 1h – wymóg Apple APNs
        "sub": contact,
    }

    # Załaduj klucz prywatny
    priv_bytes = _b64url_decode(private_key_b64)
    priv_int   = int.from_bytes(priv_bytes, 'big')
    private_key = ec.derive_private_key(priv_int, ec.SECP256R1())

    # RFC 8292: JWT NIE może mieć pola "typ" – PyJWT zawsze je dodaje,
    # więc konstruujemy token ręcznie.
    import json as _json
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    header_b64  = _b64url(_json.dumps({"alg": "ES256"}, separators=(',', ':')).encode())
    payload_b64 = _b64url(_json.dumps(payload,          separators=(',', ':')).encode())
    signing_input = f"{header_b64}.{payload_b64}".encode()

    der_sig = private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der_sig)
    raw_sig = r.to_bytes(32, 'big') + s.to_bytes(32, 'big')
    token = f"{header_b64}.{payload_b64}.{_b64url(raw_sig)}"

    # RFC 8292: spacja po przecinku jest wymagana
    return f"vapid t={token}, k={public_key_b64}"

# ─── Wysyłka wiadomości ──────────────────────────────────────────────────────

def send_push(subscription: dict, title: str, body: str,
              private_key_b64: str, public_key_b64: str) -> int:
    """
    Wysyła Web Push notification do przeglądarki.
    subscription: {"endpoint": "...", "keys": {"p256dh": "...", "auth": "..."}}
    Zwraca HTTP status code (200/201/202 = sukces, 404/410 = subskrypcja wygasła, 0 = błąd lokalny).
    """
    endpoint = subscription.get("endpoint", "")
    if not endpoint:
        return 0

    # Payload JSON
    payload = json.dumps({"title": title, "body": body}).encode("utf-8")

    # Szyfrowanie (ECDH + AES-GCM) - Web Push Encryption (RFC 8291)
    import logging
    _log = logging.getLogger("push_helper")
    try:
        encrypted, salt, server_pub = _encrypt_payload(payload, subscription["keys"])
    except Exception as e:
        _log.error(f"[push] encrypt error ({endpoint[:60]}): {e}")
        return 0

    auth_header = _build_vapid_auth(endpoint, private_key_b64, public_key_b64)

    headers = {
        "Authorization": auth_header,
        "Content-Type":  "application/octet-stream",
        "Content-Encoding": "aes128gcm",
        "TTL": "86400",
    }

    try:
        if "apple.com" in endpoint:
            # APNs wymaga HTTP/2 – requests nie obsługuje, używamy httpx
            import httpx
            with httpx.Client(http2=True) as client:
                resp = client.post(endpoint, content=encrypted, headers=headers, timeout=10)
        else:
            resp = requests.post(endpoint, data=encrypted, headers=headers, timeout=10)
        ok = resp.status_code in (200, 201, 202)
        lvl = _log.info if ok else _log.warning
        lvl(f"[push] status={resp.status_code} endpoint={endpoint[:60]} body={resp.text[:200]!r}")
        return resp.status_code
    except Exception as e:
        _log.error(f"[push] BLAD wysyłki ({endpoint[:60]}): {e}")
        return 0

# ─── Szyfrowanie payloadu (RFC 8291 aes128gcm) ──────────────────────────────

def _encrypt_payload(plaintext: bytes, keys: dict):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    # Klucze odbiorcy
    receiver_pub_bytes = _b64url_decode(keys["p256dh"])
    auth_secret        = _b64url_decode(keys["auth"])

    # Klucz efemeryczny serwera
    server_private = ec.generate_private_key(ec.SECP256R1())
    server_public  = server_private.public_key()

    server_pub_numbers = server_public.public_numbers()
    server_pub_bytes   = b'\x04' + server_pub_numbers.x.to_bytes(32, 'big') + server_pub_numbers.y.to_bytes(32, 'big')

    # Odtwórz klucz publiczny odbiorcy
    x = int.from_bytes(receiver_pub_bytes[1:33], 'big')
    y = int.from_bytes(receiver_pub_bytes[33:], 'big')
    receiver_public = ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()

    # ECDH
    shared_secret = server_private.exchange(ec.ECDH(), receiver_public)

    # Salt
    salt = os.urandom(16)

    # HKDF dla klucza szyfrującego
    prkLabel   = b"WebPush: info\x00" + receiver_pub_bytes + server_pub_bytes
    prk        = HKDF(algorithm=hashes.SHA256(), length=32, salt=auth_secret, info=prkLabel).derive(shared_secret)

    cekLabel   = b"Content-Encoding: aes128gcm\x00"
    cek        = HKDF(algorithm=hashes.SHA256(), length=16, salt=salt, info=cekLabel).derive(prk)

    nonceLabel = b"Content-Encoding: nonce\x00"
    nonce      = HKDF(algorithm=hashes.SHA256(), length=12, salt=salt, info=nonceLabel).derive(prk)

    # Padding + szyfrowanie
    padded = plaintext + b'\x02'  # delimiter
    aesgcm = AESGCM(cek)
    ciphertext = aesgcm.encrypt(nonce, padded, None)

    # Nagłówek RFC 8291 – rs musi być WIĘKSZE niż len(plaintext)+1+16
    # Standard (RFC przykład + pywebpush + web-push-libs) = 4096
    rs = 4096
    header = salt + struct.pack(">I", rs) + struct.pack("B", len(server_pub_bytes)) + server_pub_bytes

    return header + ciphertext, salt, server_pub_bytes

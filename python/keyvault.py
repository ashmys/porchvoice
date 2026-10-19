from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEY_FILE = ROOT / "user_data" / "secret.key"
PREFIX = "enc:v1:"

SECRET_KEYS = (
    "LLM_API_KEY",
    "STT_API_KEY",
    "TTS_API_KEY",
    "MCP_AUTH_TOKEN",
    "LIVEKIT_API_SECRET",
)


def _load_master() -> bytes:
    try:
        raw = KEY_FILE.read_bytes().strip()
        if len(raw) >= 32:
            return raw
    except OSError:
        pass
    raw = secrets.token_bytes(48)
    try:
        KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        KEY_FILE.write_bytes(raw)
        KEY_FILE.chmod(0o600)
    except OSError:
        pass
    return raw


def _keystream(master: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hmac.new(master, nonce + counter.to_bytes(8, "big"),
                         hashlib.sha256).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


def encrypt_value(plain: str) -> str:
    plain = plain or ""
    if not plain or plain.startswith(PREFIX):
        return plain
    master = _load_master()
    nonce = secrets.token_bytes(16)
    data = plain.encode("utf-8")
    mac = hmac.new(master, nonce + data, hashlib.sha256).digest()[:16]
    stream = _keystream(master, nonce, len(data))
    body = bytes(a ^ b for a, b in zip(data, stream))
    return PREFIX + base64.urlsafe_b64encode(nonce + mac + body).decode("ascii")


def decrypt_value(stored: str) -> str:
    if not stored or not stored.startswith(PREFIX):
        return stored or ""
    try:
        raw = base64.urlsafe_b64decode(stored[len(PREFIX):].encode("ascii"))
        nonce, mac, body = raw[:16], raw[16:32], raw[32:]
        master = _load_master()
        stream = _keystream(master, nonce, len(body))
        data = bytes(a ^ b for a, b in zip(body, stream))
        expect = hmac.new(master, nonce + data, hashlib.sha256).digest()[:16]
        if not hmac.compare_digest(mac, expect):
            return ""
        return data.decode("utf-8")
    except Exception:
        return ""


def is_encrypted(value: str) -> bool:
    return bool(value) and value.startswith(PREFIX)

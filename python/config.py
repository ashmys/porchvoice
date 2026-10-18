from __future__ import annotations

import os
import re
import socket
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / "user_data" / ".env.local"

PUBLISH_MODES = ("local", "wifi", "public")

CONFIG_DEFAULTS = {
    "LIVEKIT_URL": "ws://127.0.0.1:7880",
    "LIVEKIT_API_KEY": "devkey",
    "LIVEKIT_API_SECRET": "porchvoice-secret-1234567890abcdef",
    "PUBLISH_MODE": "local",
    "PUBLIC_PORT": "8090",
    "LLM_BASE_URL": "",
    "LLM_API_KEY": "",
    "LLM_MODEL": "",
    "LLM_COMPAT_MODE": "0",
    "LLM_STREAMING": "1",
    "LLM_THINKING": "0",
    "LLM_SPEAK_THINKING": "0",
    "STT_BASE_URL": "",
    "STT_API_KEY": "",
    "STT_MODEL": "whisper-1",
    "WHISPER_MODEL": "small",
    "WHISPER_DEVICE": "auto",
    "WHISPER_COMPUTE": "auto",
    "WHISPER_LANGUAGE": "en",
    "WHISPER_BEAM": "1",
    "WHISPER_VAD_FILTER": "1",
    "WHISPER_TEMPERATURE": "0.0",
    "WHISPER_MIN_LOGPROB": "-1.0",
    "WHISPER_MAX_COMPRATIO": "2.4",
    "WHISPER_INITIAL_PROMPT": "",
    "TTS_BASE_URL": "",
    "TTS_API_KEY": "",
    "TTS_MODEL": "tts-1",
    "TTS_VOICE": "en_US-amy-medium.onnx",
    "TTS_SPEED": "1.1",
    "MCP_URL": "",
    "MCP_AUTH_TOKEN": "",
    "MCP_TIMEOUT": "5",
    "VAD_MIN_SILENCE": "0.55",
    "VAD_ACTIVATION_THRESHOLD": "0.45",
    "ENDPOINTING_DELAY": "0.7",
    "ECHO_GUARD": "1",
    "ECHO_GUARD_TAIL": "0.8",
    "LOG_LEVEL": "INFO",
    "AUTO_OPEN": "1",
    "UI_WINDOW": "1",
    "UI_BROWSER": "",
    "UI_PROFILE": "1",
    "CLOSE_WITH_WINDOW": "1",
    "CAP_ECHO_CANCEL": "1",
    "CAP_NOISE_SUPPRESS": "1",
    "CAP_AUTO_GAIN": "1",
}

TEXTGEN_ALIASES = {
    "TEXTGEN_BASE_URL": "LLM_BASE_URL",
    "TEXTGEN_API_KEY": "LLM_API_KEY",
    "TEXTGEN_MODEL": "LLM_MODEL",
}

HEAVY_KEYS = (
    "WHISPER_MODEL",
    "WHISPER_DEVICE",
    "WHISPER_COMPUTE",
    "TTS_VOICE",
    "TTS_SPEED",
    "STT_BASE_URL",
    "TTS_BASE_URL",
    "LLM_BASE_URL",
    "LLM_MODEL",
)

LOCAL_LLM_CANDIDATES = (
    "http://127.0.0.1:11434/v1",
    "http://127.0.0.1:1234/v1",
    "http://127.0.0.1:8080/v1",
    "http://127.0.0.1:5000/v1",
)

PROBE_TIMEOUT = 2.0


def classify_endpoint(url: str) -> str:
    url = (url or "").strip().lower()
    if url.startswith("https://"):
        return "cloud"
    return "local"


def load_config() -> dict:
    cfg = dict(CONFIG_DEFAULTS)
    try:
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                cfg[k] = v.strip()
    except FileNotFoundError:
        pass
    for k in list(cfg):
        env_v = os.environ.get(k)
        if env_v:
            cfg[k] = env_v
    for old, new in TEXTGEN_ALIASES.items():
        if cfg.get(old) and not cfg.get(new):
            cfg[new] = cfg[old]
    return cfg


def _num(cfg: dict, key: str, default: str, cast=float):
    raw = str(cfg.get(key, "") or "").strip()
    if not raw:
        return cast(default)
    try:
        return cast(raw)
    except ValueError:
        return cast(default)


def _flag(cfg: dict, key: str, default: str = "0") -> bool:
    return str(cfg.get(key, "") or "").strip() == "1" or (
        not str(cfg.get(key, "") or "").strip() and default == "1"
    )


def http_json_ok(base_url: str, key: str = "", timeout: float = PROBE_TIMEOUT) -> bool:
    url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": "Bearer " + key} if key else {}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def detect_lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        try:
            hostname = socket.gethostname()
            return socket.gethostbyname(hostname)
        except OSError:
            return "127.0.0.1"
    finally:
        s.close()


def publish_mode(cfg: dict) -> str:
    mode = str(cfg.get("PUBLISH_MODE", "local") or "local").strip().lower()
    return mode if mode in PUBLISH_MODES else "local"


def resolve_publish(cfg: dict) -> dict:
    mode = publish_mode(cfg)
    lan_ip = detect_lan_ip()
    info = {
        "mode": mode,
        "http_host": "127.0.0.1",
        "http_port": _num(cfg, "PUBLIC_PORT", "8090", int),
        "lan_ip": lan_ip,
        "tunnel_url": "",
        "client_livekit_url": cfg.get("LIVEKIT_URL", "ws://127.0.0.1:7880"),
        "lan_livekit_url": "ws://" + lan_ip + ":7880",
        "lan_url": "http://" + lan_ip + ":8090",
        "local_url": "http://127.0.0.1:8090",
    }
    if mode == "wifi":
        info["http_host"] = "0.0.0.0"
    elif mode == "public":
        info["http_host"] = "0.0.0.0"
    return info


def resolve_llm(cfg: dict, probe=None) -> dict:
    probe = probe or http_json_ok
    base = (cfg.get("LLM_BASE_URL", "") or "").strip()
    key = (cfg.get("LLM_API_KEY", "") or "").strip()
    model = (cfg.get("LLM_MODEL", "") or "").strip()
    if base:
        kind = classify_endpoint(base)
        return {
            "base_url": base, "api_key": key, "model": model,
            "kind": kind, "source": "config",
        }
    for cand in LOCAL_LLM_CANDIDATES:
        if probe(cand):
            return {
                "base_url": cand, "api_key": "", "model": model,
                "kind": "local", "source": "auto:" + cand,
            }
    cloud_key = key or (os.environ.get("OPENAI_API_KEY", "") or "").strip()
    if cloud_key:
        return {
            "base_url": "https://api.openai.com/v1", "api_key": cloud_key,
            "model": model or "gpt-4o-mini", "kind": "cloud",
            "source": "fallback:openai",
        }
    return {
        "base_url": "", "api_key": "", "model": model, "kind": "none",
        "source": "none",
    }


def resolve_stt(cfg: dict) -> dict:
    base = (cfg.get("STT_BASE_URL", "") or "").strip()
    key = (cfg.get("STT_API_KEY", "") or "").strip()
    if base:
        return {
            "base_url": base.rstrip("/"), "api_key": key,
            "model": (cfg.get("STT_MODEL", "") or "whisper-1").strip(),
            "kind": classify_endpoint(base), "source": "config",
        }
    if key and os.environ.get("PORCHVOICE_STT_CLOUD", "") == "1":
        return {
            "base_url": "https://api.openai.com/v1", "api_key": key,
            "model": (cfg.get("STT_MODEL", "") or "whisper-1").strip(),
            "kind": "cloud", "source": "fallback:openai",
        }
    return {"base_url": "", "api_key": "", "model": "", "kind": "local", "source": "embedded"}


def resolve_tts(cfg: dict) -> dict:
    base = (cfg.get("TTS_BASE_URL", "") or "").strip()
    key = (cfg.get("TTS_API_KEY", "") or "").strip()
    voice = (cfg.get("TTS_VOICE", "") or "").strip()
    if base:
        return {
            "base_url": base.rstrip("/"), "api_key": key,
            "model": (cfg.get("TTS_MODEL", "") or "tts-1").strip(),
            "voice": voice or "piper",
            "speed": _num(cfg, "TTS_SPEED", "1.1"),
            "kind": classify_endpoint(base), "source": "config",
        }
    from local_tts import find_voice

    if not find_voice(voice or "en_US-amy-medium.onnx") and key:
        return {
            "base_url": "https://api.openai.com/v1", "api_key": key,
            "model": (cfg.get("TTS_MODEL", "") or "tts-1").strip(),
            "voice": "alloy", "speed": _num(cfg, "TTS_SPEED", "1.1"),
            "kind": "cloud", "source": "fallback:openai",
        }
    return {
        "base_url": "", "api_key": "", "model": "", "voice": voice,
        "speed": _num(cfg, "TTS_SPEED", "1.1"), "kind": "local",
        "source": "embedded",
    }


def resolve_mcp(cfg: dict) -> dict:
    url = (cfg.get("MCP_URL", "") or "").strip()
    token = (cfg.get("MCP_AUTH_TOKEN", "") or "").strip()
    if not url:
        return {"url": "", "token": "", "kind": "none", "enabled": False}
    return {
        "url": url, "token": token,
        "kind": classify_endpoint(url), "enabled": True,
        "timeout": _num(cfg, "MCP_TIMEOUT", "5"),
    }


def masked(key: str) -> str:
    if not key:
        return ""
    return key[:3] + "..." + key[-4:] if len(key) > 9 else "***"


def safe_room_name(room: str) -> str:
    return re.sub(r"[^\w.-]+", "_", room or "room")

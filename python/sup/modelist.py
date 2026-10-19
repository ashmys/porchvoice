"""Local model inventory and provider probes (health, banner, settings UI)."""
from __future__ import annotations

import subprocess
import urllib.error
import urllib.request

from . import paths, state
from config import llm_gguf_dir, load_config, resolve_llm, resolve_stt, \
    resolve_tts
from .ggufsrv import find_llama_server, gguf_asr_files
from .logx import log

PROBE_WHY = {
    401: "auth failed (check the API key)",
    402: "usage limit reached (out of credits or quota)",
    403: "forbidden (key lacks access)",
    429: "rate limited (usage limit hit)",
    503: "starting (the model is still loading)",
}

WHISPER_SIZES_MB = {
    "tiny.en": 80, "base.en": 150, "small.en": 500, "small": 500,
    "medium.en": 1530, "medium": 1530, "large-v3-turbo": 1620, "large-v3": 3100,
}


def whisper_dir():
    return paths.ROOT / "user_data" / "models" / "whisper"


def piper_dir():
    return paths.ROOT / "user_data" / "models" / "piper"


def local_model_dir(name: str):
    d = whisper_dir() / name
    return d if (d / "model.bin").is_file() else None


def list_stt_models() -> dict:
    local = []
    base = whisper_dir()
    if base.is_dir():
        for d in sorted(base.iterdir()):
            if d.is_dir() and (d / "model.bin").is_file():
                local.append({
                    "name": d.name,
                    "size_mb": round((d / "model.bin").stat().st_size / 1e6),
                    "local": True,
                })
    presets = []
    for name, mb in WHISPER_SIZES_MB.items():
        downloaded = bool(local_model_dir(name))
        presets.append({"name": name, "size_mb": mb, "local": downloaded})
    cfg = load_config()
    model, mmproj = gguf_asr_files()
    gguf = {
        "dir": str(paths.GGUF_ASR_DIR),
        "model": model.name if model else "",
        "mmproj": mmproj.name if mmproj else "",
        "ready": bool(model and mmproj),
        "binary": find_llama_server(),
        "server": (cfg.get("STT_GGUF_SERVER", "") or "").strip()
                  or f"http://127.0.0.1:{cfg.get('STT_GGUF_PORT', '8900')}/v1",
        "engine": (cfg.get("STT_ENGINE", "") or "").strip().lower(),
    }
    return {"presets": presets, "local_folders": local, "gguf": gguf}


def list_tts_models() -> dict:
    from local_kokoro import VOICE_PRESETS, kokoro_ready

    voices = []
    base = piper_dir()
    if base.is_dir():
        for f in sorted(base.glob("*.onnx")):
            voices.append({"name": f.name, "local": True})
    suggestions = [
        "en_US-amy-medium.onnx", "en_US-lessac-medium.onnx",
        "en_US-ryan-high.onnx", "en_GB-alba-medium.onnx",
    ]
    for s in suggestions:
        if all(v["name"] != s for v in voices):
            voices.append({"name": s, "local": False})
    return {"voices": voices, "kokoro_voices": list(VOICE_PRESETS),
            "kokoro_ready": kokoro_ready()}


def count_local_models() -> dict:
    stt = 0
    base = whisper_dir()
    if base.is_dir():
        stt = sum(1 for d in base.iterdir()
                  if d.is_dir() and (d / "model.bin").is_file())
    tts = len(list(piper_dir().glob("*.onnx"))) if piper_dir().is_dir() else 0
    llm = 0
    lbase = llm_gguf_dir(load_config())
    if lbase.is_dir():
        llm = sum(1 for f in lbase.glob("*.gguf")
                  if not f.name.lower().startswith("mmproj"))
    return {"stt": stt, "tts": tts, "llm": llm}


def check_gpu() -> str:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total",
             "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5,
        )
        line = (out.stdout or "").strip().splitlines()[0] if out.stdout else ""
        if out.returncode == 0 and line:
            return f"YES - {line} "
    except Exception:
        pass
    return "no"


def probe_provider(base_url: str, key: str, timeout: float = 5.0) -> dict:
    url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": "Bearer " + key} if key else {}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return {"status": "up", "detail": f"HTTP {r.status}", "code": r.status}
    except urllib.error.HTTPError as e:
        why = PROBE_WHY.get(e.code, f"HTTP {e.code}")
        return {"status": "down", "detail": why, "code": e.code}
    except Exception as exc:
        return {"status": "down", "detail": "unreachable (" + str(exc)[:80] + ")",
                "code": 0}


def check_llm() -> str:
    sel = resolve_llm(load_config())
    if sel["kind"] == "none":
        return "not configured (set LLM_BASE_URL or LLM_API_KEY)"
    probe = probe_provider(sel["base_url"], sel["api_key"], timeout=4)
    if probe["status"] == "up":
        return f"up ({sel['kind']}, HTTP {probe['code']})"
    return f"down ({probe['detail']})"


def check_stt_provider(cfg: dict) -> dict:
    sel = resolve_stt(cfg)
    if sel["engine"] in ("remote", "gguf") and sel["base_url"]:
        probe = probe_provider(sel["base_url"], sel["api_key"])
        probe["engine"] = sel["engine"]
        probe["url"] = sel["base_url"]
        return probe
    models = list_stt_models()
    n = len(models.get("local_folders", []))
    return {"status": "embedded", "engine": "whisper",
            "detail": f"model {cfg.get('WHISPER_MODEL', 'small')}, {n} downloaded",
            "url": ""}


def check_tts_provider(cfg: dict) -> dict:
    sel = resolve_tts(cfg)
    if sel["engine"] == "remote" and sel["base_url"]:
        probe = probe_provider(sel["base_url"], sel["api_key"])
        probe["engine"] = "remote"
        probe["url"] = sel["base_url"]
        return probe
    if sel["engine"] == "kokoro":
        from local_kokoro import kokoro_ready

        ready = kokoro_ready()
        return {"status": "embedded", "engine": "kokoro",
                "detail": ("model ready, voice "
                           + (sel["kokoro_voice"] or "af_heart"))
                          if ready else "model files missing - run setup voice step",
                "ready": ready, "url": ""}
    n = len(list(piper_dir().glob("*.onnx"))) if piper_dir().is_dir() else 0
    return {"status": "embedded", "engine": "piper",
            "detail": f"voice {sel['voice'] or '(none)'}, {n} downloaded",
            "ready": n > 0, "url": ""}

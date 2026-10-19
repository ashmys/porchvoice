"""Shared helpers for the setup wizard: prompts, env file, downloads."""
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
PYTHON_DIR = Path(__file__).resolve().parent.parent
VENV_DIR = ROOT / ".venv"
BIN_DIR = ROOT / "bin"
USER_DATA = ROOT / "user_data"
ENV_FILE = USER_DATA / ".env.local"
YAML_FILE = USER_DATA / "livekit.yaml"
WHISPER_BASE = USER_DATA / "models" / "whisper"
PIPER_BASE = USER_DATA / "models" / "piper"
KOKORO_BASE = USER_DATA / "models" / "kokoro"
GGUF_ASR_BASE = USER_DATA / "models" / "gguf-asr"
LLM_GGUF_DIR = USER_DATA / "models" / "llm"

sys.path.insert(0, str(PYTHON_DIR))

from keyvault import SECRET_KEYS, decrypt_value, encrypt_value

DRY_RUN = False
AUTO = False

LIVEKIT_VERSION = "1.13.10"

WHISPER_SIZES_MB = {
    "tiny.en": 80, "base.en": 150, "small.en": 500, "small": 500,
    "medium.en": 1530, "medium": 1530, "large-v3-turbo": 1620, "large-v3": 3100,
}
WHISPER_ORDER = ["tiny.en", "base.en", "small.en", "small", "medium.en",
                 "medium", "large-v3-turbo", "large-v3"]
WHISPER_REPOS = {"large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo"}

PIPER_VOICES = [
    ("en_US-amy-medium", "en/en_US/amy/medium"),
    ("en_US-lessac-medium", "en/en_US/lessac/medium"),
    ("en_US-ryan-high", "en/en_US/ryan/high"),
    ("en_GB-alba-medium", "en/en_GB/alba/medium"),
]
PIPER_BASE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0"

KOKORO_BASE_URL = ("https://github.com/thewh1teagle/kokoro-onnx/releases/"
                   "download/model-files-v1.0")
KOKORO_FILES = ("kokoro-v1.0.onnx", "voices-v1.0.bin")
KOKORO_VOICES = [
    "af_heart", "af_bella", "af_nicole", "af_alloy", "af_sky", "af_sarah",
    "am_adam", "am_michael", "am_fenrir", "am_puck",
    "bf_emma", "bf_isabella", "bm_george", "bm_fable",
]


def hr(title: str) -> None:
    print(f"\n==================== {title} ====================")


def ask(prompt: str, default: str = "") -> str:
    if AUTO:
        return default
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        return default
    return answer or default


def ask_secret(prompt: str, current: str) -> str:
    """Like ask(), but never prints the secret to the terminal - shows
    '[kept]' instead and returns the existing value on Enter."""
    if AUTO:
        return current
    suffix = " [kept - Enter to keep]" if current else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        return current
    if not answer or answer.lower() in ("-", "none", "no"):
        return current if not answer else ""
    return answer


def venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def run(cmd: list, **kw) -> int:
    print("+ " + " ".join(str(c) for c in cmd))
    if DRY_RUN:
        return 0
    return subprocess.run([str(c) for c in cmd], **kw).returncode


def read_env() -> dict:
    """Current settings from user_data/.env.local (decrypted for internal use)."""
    cfg = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8",
                                       errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                cfg[k.strip()] = v.strip()
    for k in SECRET_KEYS:
        if cfg.get(k, "").startswith("enc:v1:"):
            cfg[k] = decrypt_value(cfg[k])
    return cfg


def write_env(updates: dict) -> None:
    """Update user_data/.env.local.

    Existing values always win over defaults: keys not in `updates` keep
    their current value, keys unknown to the template are preserved at the
    end of the file, and only keys explicitly passed in `updates` change.
    Nothing the user configured is ever overwritten by a default here.
    """
    if DRY_RUN:
        print(f"(dry-run) would update {ENV_FILE.name}: {updates}")
        return
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    cfg = read_env()
    cfg.update(updates)
    for k in SECRET_KEYS:
        if cfg.get(k) and not str(cfg[k]).startswith("enc:v1:"):
            cfg[k] = encrypt_value(str(cfg[k]))
    template_keys = set()
    lines = [
        "# PorchVoice settings",
        "# Edit in a text editor, or use the web UI gear icon.",
        "# Setup only rewrites the keys it asks about - everything else here is kept.",
        "",
        "# ---- LiveKit server (must match livekit.yaml keys) ----",
        f"LIVEKIT_URL={cfg.get('LIVEKIT_URL', 'ws://127.0.0.1:7880')}",
        f"LIVEKIT_API_KEY={cfg.get('LIVEKIT_API_KEY', 'devkey')}",
        f"LIVEKIT_API_SECRET={cfg.get('LIVEKIT_API_SECRET', 'porchvoice-secret-1234567890abcdef')}",
        "",
        "# ---- Publish mode: local | wifi | public ----",
        f"PUBLISH_MODE={cfg.get('PUBLISH_MODE', 'local')}",
        "",
        "# ---- LLM engine: empty = auto (URL, then local servers, then GGUF) | gguf = managed llama.cpp | api = remote only ----",
        f"LLM_ENGINE={cfg.get('LLM_ENGINE', '')}",
        "# ---- LLM API (OpenAI-compatible; http:// = local, https:// = cloud) ----",
        "# Empty LLM_BASE_URL auto-detects: local servers first, then a downloaded",
        "# GGUF chat model, then the cloud fallback if a key is set.",
        f"LLM_BASE_URL={cfg.get('LLM_BASE_URL', cfg.get('TEXTGEN_BASE_URL', ''))}",
        f"LLM_API_KEY={cfg.get('LLM_API_KEY', cfg.get('TEXTGEN_API_KEY', ''))}",
        f"LLM_MODEL={cfg.get('LLM_MODEL', cfg.get('TEXTGEN_MODEL', ''))}",
        f"LLM_COMPAT_MODE={cfg.get('LLM_COMPAT_MODE', '0')}",
        f"LLM_STREAMING={cfg.get('LLM_STREAMING', '1')}",
        "",
        "# ---- LLM: GGUF chat model via llama.cpp (LLM_ENGINE=gguf or auto) ----",
        "# Empty LLM_GGUF_SERVER = PorchVoice runs its own llama-server child",
        f"LLM_GGUF_SERVER={cfg.get('LLM_GGUF_SERVER', '')}",
        f"LLM_GGUF_PORT={cfg.get('LLM_GGUF_PORT', '8899')}",
        f"LLM_GGUF_MODEL={cfg.get('LLM_GGUF_MODEL', '')}",
        "# Custom model folder (empty = user_data/models/llm); any *.gguf you drop",
        "# in is auto-detected:",
        f"LLM_GGUF_MODELS_DIR={cfg.get('LLM_GGUF_MODELS_DIR', '')}",
        f"LLM_GGUF_NGL={cfg.get('LLM_GGUF_NGL', '99')}",
        f"LLM_GGUF_CTX={cfg.get('LLM_GGUF_CTX', '4096')}",
        "",
        "# ---- STT engine: empty = auto (remote if URL set), whisper = embedded, gguf = Qwen3-ASR GGUF, remote = server ----",
        f"STT_ENGINE={cfg.get('STT_ENGINE', '')}",
        "# ---- STT: remote Whisper-compatible server (optional) ----",
        f"STT_BASE_URL={cfg.get('STT_BASE_URL', '')}",
        f"STT_API_KEY={cfg.get('STT_API_KEY', '')}",
        f"STT_MODEL={cfg.get('STT_MODEL', 'whisper-1')}",
        "",
        "# ---- STT: GGUF ASR via llama.cpp (STT_ENGINE=gguf) ----",
        "# Empty STT_GGUF_SERVER = PorchVoice runs its own llama-server child",
        f"STT_GGUF_SERVER={cfg.get('STT_GGUF_SERVER', '')}",
        f"STT_GGUF_PORT={cfg.get('STT_GGUF_PORT', '8900')}",
        f"STT_GGUF_MODEL={cfg.get('STT_GGUF_MODEL', 'Qwen3-ASR-0.6B-Q8_0.gguf')}",
        "",
        "# ---- TTS engine: kokoro (default) | piper | remote ----",
        f"TTS_ENGINE={cfg.get('TTS_ENGINE', 'kokoro')}",
        "# ---- TTS: remote speech server (optional; used when TTS_ENGINE=remote) ----",
        f"TTS_BASE_URL={cfg.get('TTS_BASE_URL', '')}",
        f"TTS_API_KEY={cfg.get('TTS_API_KEY', '')}",
        f"TTS_MODEL={cfg.get('TTS_MODEL', 'tts-1')}",
        "",
        "# ---- MCP tools: single URL, optional bearer token ----",
        f"MCP_URL={cfg.get('MCP_URL', '')}",
        f"MCP_AUTH_TOKEN={cfg.get('MCP_AUTH_TOKEN', '')}",
        f"MCP_TIMEOUT={cfg.get('MCP_TIMEOUT', '5')}",
        "",
        "# ---- Whisper STT (embedded engine) ----",
        f"WHISPER_MODEL={cfg.get('WHISPER_MODEL', 'small')}",
        f"WHISPER_DEVICE={cfg.get('WHISPER_DEVICE', 'auto')}",
        f"WHISPER_COMPUTE={cfg.get('WHISPER_COMPUTE', 'auto')}",
        f"WHISPER_LANGUAGE={cfg.get('WHISPER_LANGUAGE', 'en')}",
        "",
        "# ---- Kokoro TTS (embedded engine, default) ----",
        f"KOKORO_VOICE={cfg.get('KOKORO_VOICE', 'af_heart')}",
        "# ---- Piper TTS (embedded engine, fallback) ----",
        f"TTS_VOICE={cfg.get('TTS_VOICE', 'en_US-amy-medium.onnx')}",
        f"TTS_SPEED={cfg.get('TTS_SPEED', '1.1')}",
        "",
        "# ---- Voice activity / turn taking ----",
        f"VAD_MIN_SILENCE={cfg.get('VAD_MIN_SILENCE', '0.55')}",
        f"VAD_ACTIVATION_THRESHOLD={cfg.get('VAD_ACTIVATION_THRESHOLD', '0.45')}",
        f"ENDPOINTING_DELAY={cfg.get('ENDPOINTING_DELAY', '0.7')}",
        f"ECHO_GUARD={cfg.get('ECHO_GUARD', '1')}",
        f"ECHO_GUARD_TAIL={cfg.get('ECHO_GUARD_TAIL', '0.8')}",
    ]
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            template_keys.add(s.split("=", 1)[0].strip())
    leftovers = {k: v for k, v in cfg.items() if k not in template_keys}
    if leftovers:
        lines.append("")
        lines.append("# ---- Other keys kept from your previous config ----")
        for k in sorted(leftovers):
            lines.append(f"{k}={leftovers[k]}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"saved {ENV_FILE} (existing values kept)")


def socket_timeout():
    import socket

    return socket.timeout


def download_resumable(url: str, dest: Path, desc: str, min_bytes: int = 100,
                       attempts: int = 4, progress_cb=None) -> None:
    if DRY_RUN:
        print(f"  (dry-run) would download {desc} -> {dest.name}")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    got = part.stat().st_size if part.exists() else 0
    if dest.exists() and dest.stat().st_size >= min_bytes:
        print(f"  {desc}: already downloaded ({dest.stat().st_size / 1e6:.1f} MB)")
        return
    headers = {"User-Agent": "voice-kit-setup/1"}
    if got:
        headers["Range"] = f"bytes={got}-"
        print(f"  {desc}: resuming at {got / 1e6:.1f} MB")
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        if got and e.code in (416, 400):
            part.unlink(missing_ok=True)
            return download_resumable(url, dest, desc, min_bytes, attempts,
                                      progress_cb)
        raise
    total = resp.headers.get("Content-Length")
    total = int(total) + got if total else None
    mode = "ab" if got else "wb"
    start = time.time()
    written = got
    try:
        with open(part, mode) as f:
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                written += len(chunk)
                if progress_cb is not None:
                    try:
                        progress_cb(written, total or 0)
                    except Exception:
                        pass
                if total:
                    pct = written * 100 // total
                    speed = written / max(0.1, time.time() - start) / 1e6
                    print(f"\r  {desc}: {written / 1e6:.1f} / {total / 1e6:.1f} MB "
                          f"({pct}%) {speed:.1f} MB/s", end="", flush=True)
        print()
    except (socket_timeout(), ConnectionError, OSError) as exc:
        if attempts > 1:
            print(f"\n  {desc}: interrupted ({exc}) - retrying with resume...")
            time.sleep(2)
            return download_resumable(url, dest, desc, min_bytes, attempts - 1,
                                      progress_cb)
        raise
    if total and written < total:
        if attempts > 1:
            print(f"\n  {desc}: short transfer ({written} < {total}) - retrying...")
            time.sleep(2)
            return download_resumable(url, dest, desc, min_bytes, attempts - 1,
                                      progress_cb)
        part.unlink(missing_ok=True)
        raise IOError(f"{desc}: truncated download")
    part.replace(dest)
    print(f"  {desc}: done -> {dest.name}")


def hf_list_files(base: str, repo: str) -> list:
    url = f"{base}/api/models/{repo}/tree/main?recursive=true"
    with urllib.request.urlopen(urllib.request.Request(
            url, headers={"User-Agent": "voice-kit-setup/1"}), timeout=30) as r:
        tree = json.loads(r.read().decode("utf-8"))
    return [f for f in tree if f.get("type") == "file"]


def pick_hf_endpoint(repo: str) -> str:
    primary = os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")
    mirror = "https://hf-mirror.com"
    for base in (primary, mirror):
        try:
            files = hf_list_files(base, repo)
            if files:
                print(f"  repo OK at {base} ({len(files)} files)")
                return base
        except Exception as exc:
            print(f"  {base} failed ({exc})")
    raise SystemExit("Could not reach HuggingFace or the mirror - check your "
                     "internet.")


def whisper_repo(model: str) -> str:
    return WHISPER_REPOS.get(model, f"Systran/faster-whisper-{model}")


def zip_extract(archive: Path, target: Path) -> None:
    with zipfile.ZipFile(archive) as z:
        z.extractall(target)


def folder_size_mb(path: Path) -> int:
    total = 0
    try:
        for f in path.rglob("*"):
            if f.is_file():
                total += f.stat().st_size
    except OSError:
        pass
    return round(total / 1e6)


CORE_PKGS = [
    "livekit-agents==1.8.8", "livekit-plugins-openai==1.8.8",
    "livekit-plugins-silero==1.8.8", "python-dotenv", "aiohttp",
]

# Optional engine modules - nothing here is installed unless the user picks it,
# so a minimal install stays lean and unused engines never bloat the venv.
OPTIONAL_GROUPS = [
    ("kokoro", "kokoro-onnx", ["kokoro-onnx"],
     "Kokoro TTS engine (the default voice, ~100 MB wheel)"),
    ("whisper", "faster-whisper", ["faster-whisper"],
     "Embedded Whisper STT (~200 MB wheel) - skip if you use GGUF ASR or a "
     "remote STT server"),
    ("piper", "piper-tts", ["piper-tts"],
     "Piper TTS engine (lightweight fallback voice)"),
    ("mcp", "mcp", ["mcp>=1.2"],
     "MCP tool client - skip if you don't use MCP_URL tools"),
    ("gpu", "NVIDIA CUDA libs", ["nvidia-cublas-cu12", "nvidia-cudnn-cu12"],
     "GPU acceleration for Whisper (~700 MB) - skip on CPU-only machines and "
     "when Whisper is not installed"),
]

DEFAULT_GROUPS = ("kokoro",)  # --auto / Enter installs only the default voice


def pip_installed(pkgs: list) -> bool:
    try:
        import importlib.util

        modmap = {"faster-whisper": "faster_whisper",
                  "kokoro-onnx": "kokoro_onnx",
                  "piper-tts": "piper", "mcp": "mcp"}
        for p in pkgs:
            mod = modmap.get(p)
            if mod is None:
                return False  # nvidia wheels: just offer install
            if importlib.util.find_spec(mod) is None:
                return False
        return True
    except Exception:
        return False


def install_group(gid: str, prompt: bool = True, default: str = "") -> bool:
    info = next((g for g in OPTIONAL_GROUPS if g[0] == gid), None)
    if info is None:
        return False
    _gid, label, pkgs, why = info
    if not default:
        # opt-in by default: only the default voice engine answers "yes"
        default = "y" if gid in DEFAULT_GROUPS else "n"
    if not prompt:
        choice = default
    else:
        if pip_installed(pkgs) and gid != "gpu":
            print(f"  {label}: already installed - skipping")
            return True
        ans = ask(f"Install {label}? ({why}) (y/n)", default).strip().lower()
        choice = "y" if ans in ("", "y", "yes") else "n"
    if choice not in ("y", "yes"):
        print(f"  skipped: {label} (you can add it later: setup menu 9)")
        return False
    if DRY_RUN:
        print(f"  (dry-run) pip install {' '.join(pkgs)}")
        return True
    code = run([venv_python(), "-m", "pip", "install", *pkgs])
    if code != 0:
        print(f"  FAILED to install {label} - the related engine will be "
              "unavailable; re-run setup menu 9 to retry")
        return False
    return True

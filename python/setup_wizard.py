from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV_DIR = ROOT / ".venv"
BIN_DIR = ROOT / "bin"
USER_DATA = ROOT / "user_data"
ENV_FILE = USER_DATA / ".env.local"
YAML_FILE = USER_DATA / "livekit.yaml"
WHISPER_BASE = USER_DATA / "models" / "whisper"
PIPER_BASE = USER_DATA / "models" / "piper"
KOKORO_BASE = USER_DATA / "models" / "kokoro"
GGUF_ASR_BASE = USER_DATA / "models" / "gguf-asr"

sys.path.insert(0, str(Path(__file__).resolve().parent))

from keyvault import SECRET_KEYS, decrypt_value, encrypt_value

DRY_RUN = False
AUTO = False

LIVEKIT_VERSION = "1.13.10"


def livekit_download():
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("aarch64", "arm64") else "amd64"
    if system == "windows":
        name, kind = f"livekit_{LIVEKIT_VERSION}_windows_amd64.zip", "zip"
    elif system == "darwin":
        name, kind = f"livekit_{LIVEKIT_VERSION}_darwin_{arch}.zip", "zip"
    else:
        name, kind = f"livekit_{LIVEKIT_VERSION}_linux_{arch}.tar.gz", "tar.gz"
    url = ("https://github.com/livekit/livekit/releases/download/v"
           + LIVEKIT_VERSION + "/" + name)
    return url, kind

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

KOKORO_BASE_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
)
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


def venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def run(cmd: list, **kw) -> int:
    print("+ " + " ".join(str(c) for c in cmd))
    if DRY_RUN:
        return 0
    return subprocess.run([str(c) for c in cmd], **kw).returncode


def write_env(updates: dict) -> None:
    if DRY_RUN:
        print(f"(dry-run) would update {ENV_FILE.name}: {updates}")
        return
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    cfg = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                cfg[k.strip()] = v.strip()
    cfg.update(updates)
    for k in SECRET_KEYS:
        if cfg.get(k) and not str(cfg[k]).startswith("enc:v1:"):
            cfg[k] = encrypt_value(str(cfg[k]))
    lines = [
        "# PorchVoice settings",
        "# Edit in a text editor, or use the web UI gear icon.",
        "",
        "# ---- LiveKit server (must match livekit.yaml keys) ----",
        f"LIVEKIT_URL={cfg.get('LIVEKIT_URL', 'ws://127.0.0.1:7880')}",
        f"LIVEKIT_API_KEY={cfg.get('LIVEKIT_API_KEY', 'devkey')}",
        f"LIVEKIT_API_SECRET={cfg.get('LIVEKIT_API_SECRET', 'porchvoice-secret-1234567890abcdef')}",
        "",
        "# ---- Publish mode: local | wifi | public ----",
        f"PUBLISH_MODE={cfg.get('PUBLISH_MODE', 'local')}",
        "",
        "# ---- LLM API (OpenAI-compatible; http:// = local, https:// = cloud) ----",
        "# Empty LLM_BASE_URL auto-detects: local servers first, then the cloud",
        "# fallback if a key is set. API keys are optional for local servers.",
        f"LLM_BASE_URL={cfg.get('LLM_BASE_URL', cfg.get('TEXTGEN_BASE_URL', ''))}",
        f"LLM_API_KEY={cfg.get('LLM_API_KEY', cfg.get('TEXTGEN_API_KEY', ''))}",
        f"LLM_MODEL={cfg.get('LLM_MODEL', cfg.get('TEXTGEN_MODEL', ''))}",
        f"LLM_COMPAT_MODE={cfg.get('LLM_COMPAT_MODE', '0')}",
        f"LLM_STREAMING={cfg.get('LLM_STREAMING', '1')}",
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
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"saved {ENV_FILE}")


def write_livekit_yaml() -> None:
    if DRY_RUN:
        print(f"(dry-run) would write {YAML_FILE}")
        return
    cfg = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                cfg[k.strip()] = v.strip()
    key = cfg.get("LIVEKIT_API_KEY", "devkey") or "devkey"
    secret = cfg.get("LIVEKIT_API_SECRET", "") or "porchvoice-secret-1234567890abcdef"
    if secret.startswith("enc:v1:"):
        secret = decrypt_value(secret) or "porchvoice-secret-1234567890abcdef"
    YAML_FILE.parent.mkdir(parents=True, exist_ok=True)
    YAML_FILE.write_text(
        "port: 7880\n"
        "bind_addresses:\n"
        "  - 127.0.0.1\n"
        "\n"
        "rtc:\n"
        "  tcp_port: 7881\n"
        "  port_range_start: 50000\n"
        "  port_range_end: 50100\n"
        "  use_external_ip: false\n"
        "\n"
        "keys:\n"
        f"  {key}: {secret}\n",
        encoding="utf-8",
    )
    print(f"saved {YAML_FILE}")


def download_resumable(url: str, dest: Path, desc: str, min_bytes: int = 100,
                       attempts: int = 4) -> None:
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
            return download_resumable(url, dest, desc, min_bytes, attempts)
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
            return download_resumable(url, dest, desc, min_bytes, attempts - 1)
        raise
    if total and written < total:
        if attempts > 1:
            print(f"\n  {desc}: short transfer ({written} < {total}) - retrying...")
            time.sleep(2)
            return download_resumable(url, dest, desc, min_bytes, attempts - 1)
        part.unlink(missing_ok=True)
        raise IOError(f"{desc}: truncated download")
    part.replace(dest)
    print(f"  {desc}: done -> {dest.name}")


def socket_timeout():
    import socket
    return socket.timeout


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
    raise SystemExit("Could not reach HuggingFace or the mirror - check your internet.")


def whisper_repo(model: str) -> str:
    return WHISPER_REPOS.get(model, f"Systran/faster-whisper-{model}")


def step_deps() -> None:
    hr("STEP 1] Python venv + packages")
    if not VENV_DIR.exists():
        run([sys.executable, "-m", "venv", VENV_DIR])
    py = venv_python()
    if DRY_RUN:
        print("(dry-run) pip installs skipped")
        return
    run([py, "-m", "pip", "install", "--upgrade", "pip"])
    pkgs = [
        "livekit-agents==1.8.8", "livekit-plugins-openai==1.8.8",
        "livekit-plugins-silero==1.8.8", "faster-whisper", "piper-tts",
        "kokoro-onnx", "python-dotenv", "aiohttp", "mcp>=1.2",
    ]
    code = run([py, "-m", "pip", "install", *pkgs])
    if code != 0:
        raise SystemExit("Package installation failed - check your internet and retry.")
    if platform.system() != "Darwin":
        run([py, "-m", "pip", "install", "nvidia-cublas-cu12", "nvidia-cudnn-cu12"])


def step_livekit() -> None:
    hr("STEP 2] LiveKit server")
    exe_name = "livekit-server.exe" if os.name == "nt" else "livekit-server"
    exe = BIN_DIR / exe_name
    if exe.exists() and exe.stat().st_size > 10_000_000:
        print(f"  already present: {exe}")
        return
    url, kind = livekit_download()
    apath = BIN_DIR / ("livekit.zip" if kind == "zip" else "livekit.tar.gz")
    download_resumable(url, apath, "LiveKit server", min_bytes=1_000_000)
    if DRY_RUN:
        return
    if kind == "zip":
        with zipfile.ZipFile(apath) as z:
            z.extractall(BIN_DIR)
    else:
        import tarfile
        with tarfile.open(apath) as t:
            try:
                t.extractall(BIN_DIR, filter="data")
            except TypeError:
                t.extractall(BIN_DIR)
    apath.unlink(missing_ok=True)
    if not exe.is_file():
        for p in BIN_DIR.rglob(exe_name):
            p.replace(exe)
            break
    print(f"  extracted -> {exe}")


def step_config() -> None:
    hr("STEP 3] Configuration")
    write_livekit_yaml()
    print("  The LLM can be local (Ollama, LM Studio, llama.cpp, textgen webui) or")
    print("  cloud (OpenAI, Groq, OpenRouter, ...). Any OpenAI-compatible URL")
    print("  works. http:// = local, https:// = cloud. Leave empty to auto-detect")
    print("  local servers first and fall back to the cloud only if a key is set.")
    base = ask("LLM base URL (Enter = auto-detect)", "")
    key = ask("LLM API key (Enter = none; optional for local servers)", "")
    model = ask("LLM model id (Enter = use the server default)", "")
    if key.strip().lower() in ("-", "none", "no"):
        key = ""
    updates = {"LLM_BASE_URL": base, "LLM_API_KEY": key, "LLM_MODEL": model}
    print("  MCP tools: point PorchVoice at one MCP server URL (streamable HTTP or")
    print("  SSE). The bearer token is optional.")
    mcp_url = ask("MCP URL (Enter = none)", "")
    mcp_token = ""
    if mcp_url.strip():
        mcp_token = ask("MCP bearer token (Enter = none)", "")
        if mcp_token.strip().lower() in ("-", "none", "no"):
            mcp_token = ""
    updates["MCP_URL"] = mcp_url
    updates["MCP_AUTH_TOKEN"] = mcp_token
    print("  Publish mode: local = this machine only (default, no extra setup);")
    print("  wifi = share on your LAN; public = quick tunnel via cloudflared (step 6).")
    mode = ask("Publish mode (local/wifi/public)", "local").strip().lower()
    if mode not in ("local", "wifi", "public"):
        mode = "local"
    if mode == "local":
        print("  local mode: nothing to publish - no tunnel, no LAN exposure, no step 6.")
    updates["PUBLISH_MODE"] = mode
    write_env(updates)
    print("  You can change every setting later in the web UI (gear icon).")
    return mode


def step_cloudflared() -> None:
    hr("STEP 6] cloudflared (for public publish mode)")
    exe_name = "cloudflared.exe" if os.name == "nt" else "cloudflared"
    exe = BIN_DIR / exe_name
    if exe.exists() and exe.stat().st_size > 1_000_000:
        print(f"  already present: {exe}")
        return
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("aarch64", "arm64") else "amd64"
    if system == "windows":
        url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
    elif system == "darwin":
        url = ("https://github.com/cloudflare/cloudflared/releases/latest/download/"
               + ("cloudflared-darwin-arm64.tgz" if arch == "arm64"
                  else "cloudflared-darwin-amd64.tgz"))
    else:
        url = ("https://github.com/cloudflare/cloudflared/releases/latest/download/"
               + f"cloudflared-linux-{arch}.tgz")
    if system != "windows":
        apath = BIN_DIR / "cloudflared.tgz"
        download_resumable(url, apath, "cloudflared", min_bytes=1_000_000)
        if DRY_RUN:
            return
        import tarfile
        with tarfile.open(apath) as t:
            t.extractall(BIN_DIR)
        apath.unlink(missing_ok=True)
        if not exe.is_file():
            for p in BIN_DIR.rglob(exe_name):
                p.replace(exe)
                break
    else:
        download_resumable(url, exe, "cloudflared", min_bytes=1_000_000)
        return
    try:
        exe.chmod(exe.stat().st_mode | 0o755)
    except OSError:
        pass
    print(f"  extracted -> {exe}")


_GPU_PROBE_CODE = r"""
import json, os, site, sys
from pathlib import Path
for sp in site.getsitepackages():
    for sub in ("cudnn", "cublas"):
        d = Path(sp) / "nvidia" / sub / "bin"
        if d.is_dir():
            os.add_dll_directory(str(d))
            os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
out = {"cuda_devices": 0, "types": [], "error": ""}
try:
    import ctranslate2
    out["cuda_devices"] = ctranslate2.get_cuda_device_count()
    if out["cuda_devices"]:
        out["types"] = list(ctranslate2.get_supported_compute_types("cuda"))
except Exception as e:
    out["error"] = f"ctranslate2: {e}"
print("PROBE_JSON:" + json.dumps(out), flush=True)
sys.exit(0)
"""


def probe_gpu(timeout: int = 300) -> dict:
    if DRY_RUN:
        return {"cuda_devices": 1, "types": ["float16"], "load_ok": True, "error": ""}
    py = venv_python()
    if not py.exists():
        return {"cuda_devices": 0, "types": [], "load_ok": False,
                "error": "venv not built yet"}
    code = _GPU_PROBE_CODE
    try:
        r = subprocess.run(
            [str(py), "-c", code], capture_output=True, text=True,
            timeout=timeout, cwd=str(ROOT))
        for line in (r.stdout or "").splitlines():
            if line.startswith("PROBE_JSON:"):
                return json.loads(line[len("PROBE_JSON:"):])
        return {"cuda_devices": 0, "types": [], "load_ok": False,
                "error": (r.stderr or "no probe output")[-200:]}
    except subprocess.TimeoutExpired:
        return {"cuda_devices": 0, "types": [], "load_ok": False,
                "error": f"probe timed out after {timeout}s"}


def step_whisper_prefetch() -> None:
    hr("STEP 4] Whisper STT model pre-download + GPU test")
    print("  Pick the model to pre-download (speed vs quality):")
    for i, name in enumerate(WHISPER_ORDER, 1):
        mark = " (already downloaded)" if (WHISPER_BASE / name / "model.bin").is_file() else ""
        print(f"   {i}) {name:<16} ~{WHISPER_SIZES_MB[name]} MB{mark}")
    default_idx = WHISPER_ORDER.index("small") + 1
    choice = ask("Number", str(default_idx))
    try:
        model = WHISPER_ORDER[int(choice) - 1]
    except (ValueError, IndexError):
        model = "small"
    print(f"  Selected: {model}")
    write_env({"WHISPER_MODEL": model})

    if DRY_RUN:
        print("(dry-run) download skipped")
        return
    dest_dir = WHISPER_BASE / model
    bin_file = dest_dir / "model.bin"
    if bin_file.exists() and bin_file.stat().st_size > 50_000_000:
        print("  model.bin already downloaded - skipping")
    else:
        repo = whisper_repo(model)
        base = pick_hf_endpoint(repo)
        files = hf_list_files(base, repo)
        print(f"  downloading {len(files)} files from {repo} ...")
        for finfo in files:
            fpath = finfo["path"]
            url = f"{base}/{repo}/resolve/main/{fpath}"
            download_resumable(url, dest_dir / Path(fpath).name,
                               Path(fpath).name, min_bytes=100)

    probe = probe_gpu()
    if probe.get("cuda_devices"):
        print(f"  GPU: CUDA available ({probe.get('types')}) - Whisper will use it")
    else:
        print(f"  GPU: not usable ({probe.get('error', 'not found')}) - "
              "Whisper will run on CPU (fine for tiny/base/small)")
    print("  Test-loading the model (first run may take a minute)...")
    py = venv_python()
    code = (
        "import sys; sys.path.insert(0, r'%s');\n"
        "from local_stt import create_stt;\n"
        "create_stt(model=r'%s', device='auto', compute_type='auto');\n"
        "print('WHISPER LOAD TEST OK')"
    ) % (str(Path(__file__).resolve().parent), str(dest_dir))
    try:
        r = subprocess.run([str(py), "-c", code], cwd=str(ROOT), timeout=600)
        if r.returncode != 0:
            print("  Load test failed - check the error above. The kit still runs")
            print("  if you pick a smaller model via the web UI.")
    except subprocess.TimeoutExpired:
        print("  Load test timed out - try a smaller model.")


def step_kokoro() -> bool:
    hr("STEP 5a] Kokoro voice (default TTS engine)")
    if DRY_RUN:
        print("(dry-run) kokoro download skipped")
        return True
    model_dest = KOKORO_BASE / "kokoro-v1.0.onnx"
    voices_dest = KOKORO_BASE / "voices-v1.0.bin"
    if model_dest.is_file() and model_dest.stat().st_size > 100_000_000 \
            and voices_dest.is_file():
        print("  kokoro model already downloaded - skipping")
        return True
    for fname in KOKORO_FILES:
        url = f"{KOKORO_BASE_URL}/{fname}"
        min_bytes = 20_000_000 if fname.endswith(".bin") else 100_000_000
        download_resumable(url, KOKORO_BASE / fname, fname, min_bytes=min_bytes)
    print("  Test-synthesizing with kokoro-onnx...")
    py = venv_python()
    code = (
        "from kokoro_onnx import Kokoro;\n"
        f"k = Kokoro(r'{model_dest}', r'{voices_dest}');\n"
        "samples, sr = k.create('Kokoro voice test.', voice='af_heart', speed=1.0, lang='en-us');\n"
        "assert len(samples) > 1000;\n"
        "print('KOKORO TEST OK')"
    )
    try:
        r = subprocess.run([str(py), "-c", code], cwd=str(ROOT), timeout=600)
        if r.returncode != 0:
            print("  Kokoro test failed - check the error above. You can switch")
            print("  TTS_ENGINE=piper in the settings instead.")
            return False
    except subprocess.TimeoutExpired:
        print("  Kokoro test timed out.")
        return False
    return True


def step_piper() -> None:
    hr("STEP 5] Piper TTS voice")
    print("  Pick a voice:")
    for i, (name, _sub) in enumerate(PIPER_VOICES, 1):
        have = (PIPER_BASE / f"{name}.onnx").is_file()
        print(f"   {i}) {name}{' (already downloaded)' if have else ''}")
    choice = ask("Number", "1")
    try:
        name, sub = PIPER_VOICES[int(choice) - 1]
    except (ValueError, IndexError):
        name, sub = PIPER_VOICES[0]
    print(f"  Selected: {name}")
    write_env({"TTS_VOICE": name + ".onnx"})
    if DRY_RUN:
        return
    for suffix in (".onnx", ".onnx.json"):
        url = f"{PIPER_BASE_URL}/{sub}/{name}{suffix}"
        download_resumable(url, PIPER_BASE / f"{name}{suffix}", name + suffix)
    print("  Test-synthesizing...")
    py = venv_python()
    code = (
        "from piper import PiperVoice;\n"
        "v = PiperVoice.load(r'%s');\n"
        "list(v.synthesize('Hello, the voice works.'));\n"
        "print('PIPER TEST OK')"
    ) % str(PIPER_BASE / f"{name}.onnx")
    try:
        r = subprocess.run([str(py), "-c", code], cwd=str(ROOT), timeout=300)
        if r.returncode != 0:
            print("  Piper test failed - check the error above.")
    except subprocess.TimeoutExpired:
        print("  Piper test timed out.")


def step_voice() -> None:
    hr("STEP 5] Voice engine (TTS)")
    print("  1) Kokoro (default - best quality, ~340 MB download)")
    print("  2) Piper (lightweight, ~60 MB voice)")
    print("  3) Skip (choose later in the web UI)")
    choice = ask("Number", "1")
    if choice == "2":
        write_env({"TTS_ENGINE": "piper"})
        step_piper()
        return
    if choice == "3":
        print("  Skipped - the web UI can pick an engine later (TTS_ENGINE).")
        return
    write_env({"TTS_ENGINE": "kokoro"})
    print("  Pick a Kokoro voice:")
    for i, name in enumerate(KOKORO_VOICES, 1):
        print(f"   {i}) {name}")
    vchoice = ask("Number", "1")
    try:
        voice = KOKORO_VOICES[int(vchoice) - 1]
    except (ValueError, IndexError):
        voice = KOKORO_VOICES[0]
    print(f"  Selected: {voice}")
    write_env({"KOKORO_VOICE": voice})
    if not step_kokoro():
        print("  Falling back to Piper (TTS_ENGINE=piper).")
        write_env({"TTS_ENGINE": "piper"})
        step_piper()


GGUF_ASR_BASE_URL = "https://huggingface.co/ggml-org/Qwen3-ASR-0.6B-GGUF/resolve/main"
GGUF_ASR_FILES = ("Qwen3-ASR-0.6B-Q8_0.gguf", "mmproj-Qwen3-ASR-0.6B-Q8_0.gguf")
LLAMA_RELEASE_API = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"


def latest_llama_assets() -> tuple:
    req = urllib.request.Request(LLAMA_RELEASE_API,
                                 headers={"User-Agent": "voice-kit-setup/1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    return data.get("tag_name", ""), [a.get("name", "") for a in data.get("assets", [])]


def pick_llama_assets(assets: list) -> list:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "windows":
        for key in ("-bin-win-cuda-", "-bin-win-vulkan-", "-bin-win-cpu-"):
            main = next((n for n in assets if n.startswith("llama-") and key in n
                         and n.endswith("x64.zip")), "")
            if main:
                extras = ([n for n in assets if n.startswith("cudart-")
                           and key in n and n.endswith(".zip")]
                          if "cuda" in key else [])
                return [main] + extras
    elif system == "darwin":
        arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
        main = next((n for n in assets if n.startswith("llama-")
                     and f"-bin-macos-{arch}" in n and n.endswith(".zip")), "")
        return [main] if main else []
    else:
        main = next((n for n in assets if n.startswith("llama-")
                     and "-bin-ubuntu-x64" in n and n.endswith(".zip")), "")
        return [main] if main else []
    return []


def download_llama_server() -> bool:
    exe_name = "llama-server.exe" if os.name == "nt" else "llama-server"
    if (BIN_DIR / exe_name).is_file() or shutil.which("llama-server"):
        return True
    if DRY_RUN:
        print("(dry-run) llama-server download skipped")
        return True
    print("  Fetching the latest llama.cpp release (llama-server)...")
    try:
        tag, assets = latest_llama_assets()
    except Exception as exc:
        print(f"  could not query llama.cpp releases ({exc})")
        print("  Download llama-server manually from "
              "https://github.com/ggml-org/llama.cpp/releases and put it in bin/")
        return False
    names = pick_llama_assets(assets)
    if not names:
        print("  no matching llama.cpp asset for this platform - download it "
              "manually from https://github.com/ggml-org/llama.cpp/releases")
        return False
    for name in names:
        url = f"https://github.com/ggml-org/llama.cpp/releases/download/{tag}/{name}"
        dest = BIN_DIR / name
        download_resumable(url, dest, name, min_bytes=1_000_000)
        with zipfile.ZipFile(dest) as z:
            z.extractall(BIN_DIR)
        dest.unlink(missing_ok=True)
    if not (BIN_DIR / exe_name).is_file():
        for p in BIN_DIR.rglob(exe_name):
            p.replace(BIN_DIR / exe_name)
            break
    if (BIN_DIR / exe_name).is_file():
        print(f"  extracted -> {BIN_DIR / exe_name}")
        return True
    print(f"  {exe_name} not found after extraction - check bin/ manually")
    return False


def step_gguf_asr(default_yes: str = "y") -> None:
    hr("STEP 7] GGUF ASR - Qwen3-ASR 0.6B via llama.cpp (speech-to-text)")
    print("  Speech-to-text with the Qwen3-ASR-0.6B GGUF model (~1 GB total).")
    print("  PorchVoice starts a local llama-server child automatically and")
    print("  transcribes through its OpenAI-compatible /v1/audio/transcriptions.")
    model_ok = all((GGUF_ASR_BASE / f).is_file() for f in GGUF_ASR_FILES)
    exe_name = "llama-server.exe" if os.name == "nt" else "llama-server"
    bin_ok = (BIN_DIR / exe_name).is_file() or bool(shutil.which("llama-server"))
    print(f"  model files : {'present' if model_ok else 'missing'} ({GGUF_ASR_BASE})")
    print(f"  llama-server: {'present' if bin_ok else 'missing'} (bin/{exe_name})")
    if not model_ok:
        if ask("Download the Qwen3-ASR GGUF model? (Y/n)", default_yes).strip().lower() in \
                ("", "y", "yes"):
            if DRY_RUN:
                print("(dry-run) GGUF model download skipped")
            else:
                for fname in GGUF_ASR_FILES:
                    url = f"{GGUF_ASR_BASE_URL}/{fname}"
                    min_bytes = 50_000_000 if fname.startswith("mmproj") else 500_000_000
                    download_resumable(url, GGUF_ASR_BASE / fname, fname,
                                       min_bytes=min_bytes)
                model_ok = all((GGUF_ASR_BASE / f).is_file() for f in GGUF_ASR_FILES)
    if model_ok and not bin_ok:
        if ask("Download the llama.cpp llama-server binary? (Y/n)", default_yes).strip().lower() in \
                ("", "y", "yes"):
            bin_ok = download_llama_server()
    if model_ok and bin_ok:
        if ask("Enable GGUF ASR now (STT_ENGINE=gguf)? (Y/n)", "y").strip().lower() in \
                ("", "y", "yes"):
            write_env({"STT_ENGINE": "gguf"})
            print("  PorchVoice will start llama-server with Qwen3-ASR on next launch.")
    else:
        print("  GGUF ASR not fully set up - the embedded Whisper engine stays active.")
        print("  You can retry later (setup menu 7) or point STT_GGUF_SERVER at an")
        print("  existing llama-server in the web UI settings.")


def done() -> None:
    hr("ALL DONE")
    if os.name == "nt":
        print("  Start everything with  start.bat")
    else:
        print("  Start everything with  ./start.sh")
    print("  Then open  http://127.0.0.1:8090  and press play.")


def run_all() -> None:
    step_deps(); step_livekit()
    mode = step_config()
    step_whisper_prefetch(); step_voice()
    step_gguf_asr(default_yes="n")
    if mode == "public":
        step_cloudflared()
    else:
        print("\n  Publish mode is not public - skipping cloudflared setup (step 6).")
    done()


def main() -> None:
    global DRY_RUN, AUTO
    flags = set(sys.argv[1:])
    DRY_RUN = "--dry-run" in flags
    AUTO = "--auto" in flags
    models_only = "--models-only" in flags

    print("=" * 60)
    print(" PORCHVOICE - setup")
    print("=" * 60)

    if models_only:
        step_whisper_prefetch()
        step_voice()
        done()
        return

    while True:
        hr("MENU")
        print("  1) Python venv + packages")
        print("  2) LiveKit server")
        print("  3) Configuration (LLM API, MCP, publish mode)")
        print("  4) Whisper STT model")
        print("  5) Voice engine (Kokoro / Piper TTS)")
        print("  6) cloudflared (only for public publish mode)")
        print("  7) GGUF ASR (Qwen3-ASR 0.6B speech-to-text via llama.cpp)")
        print("  0) Run ALL steps, then exit")
        choice = ask("Choose", "0")
        if DRY_RUN and AUTO:
            run_all()
            return
        if choice == "1":
            step_deps()
        elif choice == "2":
            step_livekit()
        elif choice == "3":
            step_config()
        elif choice == "4":
            step_whisper_prefetch()
        elif choice == "5":
            step_voice()
        elif choice == "6":
            step_cloudflared()
        elif choice == "7":
            step_gguf_asr()
        elif choice == "0":
            run_all()
            return


if __name__ == "__main__":
    main()

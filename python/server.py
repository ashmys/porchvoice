import asyncio
import ctypes
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path

if os.name == "nt":
    import ctypes.wintypes

import aiohttp
from aiohttp import WSMsgType, web
from dotenv import load_dotenv
from livekit import api as lk_api

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

WEB_DIR = Path(__file__).resolve().parent / "web"
ENV_FILE = ROOT / "user_data" / ".env.local"
CHAT_DIR = ROOT / "user_data" / "chat"
LIVEKIT_YAML = ROOT / "user_data" / "livekit.yaml"
LIVEKIT_EXE = ROOT / "bin" / ("livekit-server.exe" if os.name == "nt" else "livekit-server")
CLOUDFLARED_EXE = ROOT / "bin" / ("cloudflared.exe" if os.name == "nt" else "cloudflared")
AGENT_SCRIPT = Path(__file__).resolve().parent / "agent.py"
WINDOW_STATE_FILE = ROOT / "user_data" / "window_state.json"

from config import (
    CONFIG_DEFAULTS,
    detect_lan_ip,
    gguf_stt_url,
    load_config,
    masked,
    publish_mode,
    resolve_llm,
    resolve_mcp,
    resolve_stt,
    resolve_tts,
)
from keyvault import SECRET_KEYS, encrypt_value, is_encrypted
from mcp_client import probe_mcp

SECRET_SENTINEL = "__saved__"

GGUF_ASR_DIR = ROOT / "user_data" / "models" / "gguf-asr"
LLAMA_SERVER_EXE = ROOT / "bin" / ("llama-server.exe" if os.name == "nt"
                                   else "llama-server")

RESTART_MAX_CRASHES = 5
FATAL_MARKERS = (
    "api_secret is required",
    "api_key is required",
    "ws_url is required",
    "could not parse config",
    "invalid api key",
)


HB_INTERVAL = 2.5
HB_GAP_SHUTDOWN = 600.0

HB_GAP_PENDING = 3.0

_SHUT_LOCK = threading.Lock()
_SHUTTING = False
_LAST_BEAT = 0.0
_CLOSE_PENDING_AT = 0.0
_WIN_MINIMIZED = False
_CLOSE_CHECK = threading.Event()


def _monotonic() -> float:
    return time.monotonic()


def heartbeat_fresh(window: float = 3.0) -> bool:
    return _LAST_BEAT > 0 and (_monotonic() - _LAST_BEAT) <= window


def request_shutdown(reason: str, fast: bool = False) -> None:
    global _SHUTTING
    if setting("CLOSE_WITH_WINDOW", "1") != "1":
        return
    if heartbeat_fresh():
        if not fast:


            log(f"window closed, but the UI is still sending heartbeats - staying up ({reason})")
            return
        baseline = _LAST_BEAT
        deadline = _monotonic() + 1.2
        while _monotonic() < deadline:
            if _LAST_BEAT != baseline:
                log(f"window closed, but another UI page is still alive - staying up ({reason})")
                return
            time.sleep(0.1)
    with _SHUT_LOCK:
        if _SHUTTING:
            return
        _SHUTTING = True
    log(f"UI window closed ({reason}) - shutting down LiveKit + agent, bye!")
    try:
        stop_children()
    except Exception:
        pass
    os._exit(0)


def heartbeat_watcher() -> None:
    global _WIN_MINIMIZED
    while True:
        time.sleep(1.0)
        if _LAST_BEAT <= 0:
            continue
        if _WIN_MINIMIZED:
            continue
        gap = _monotonic() - _LAST_BEAT
        pending_recent = _CLOSE_PENDING_AT > 0 and (_monotonic() - _CLOSE_PENDING_AT) < 60
        threshold = HB_GAP_PENDING if pending_recent else HB_GAP_SHUTDOWN
        if gap >= threshold:
            request_shutdown(f"no UI heartbeat for {gap:.0f}s")


def venv_python() -> Path:
    if os.name == "nt":
        return ROOT / ".venv" / "Scripts" / "python.exe"
    return ROOT / ".venv" / "bin" / "python"

load_dotenv(ENV_FILE)

HOST = "127.0.0.1"
PORT = 8090

TUNNEL = {"proc": None, "url": "", "started_at": 0.0, "log": deque(maxlen=80)}
TUNNEL_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com", re.IGNORECASE)

CHILDREN = {}
_RESTART = {}
_CRASHES = {}
_SPAWNED_AT = {}
_OUT = {}
_BROWSER = None
_BROWSER_JOBBED = False


def log(msg: str) -> None:
    print(f"[  sup] {msg}", flush=True)


def _harden_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except Exception:
            pass


def env_dict() -> dict:
    cfg = {}
    try:
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    cfg[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return cfg


def repair_env_file() -> None:
    if not ENV_FILE.is_file():
        return
    try:
        lines = ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        log(f"could not read {ENV_FILE.name} for repair ({exc})")
        return
    fixed = False
    out = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" not in stripped:
            line = "# " + stripped
            fixed = True
        out.append(line)
    if not fixed:
        return
    try:
        ENV_FILE.write_text("\n".join(out) + "\n", encoding="utf-8")
        log(f"repaired {ENV_FILE.name}: commented out line(s) that are not "
            "KEY=value (they made python-dotenv warn on every start)")
    except OSError as exc:
        log(f"could not rewrite {ENV_FILE.name} ({exc})")


def mask_secrets(cfg: dict) -> dict:
    out = dict(cfg)
    for k in SECRET_KEYS:
        if out.get(k):
            out[k] = SECRET_SENTINEL
    return out


def setting(key: str, default: str = "") -> str:
    cfg = load_config()
    v = cfg.get(key, "")
    if v:
        return v
    return os.environ.get(key, default)


JOB_OBJECT_EXTENDED_LIMIT = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000


def setup_job_object():
    if os.name != "nt":
        return None
    try:
        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = ctypes.c_void_p
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        k32.SetInformationJobObject.restype = ctypes.c_int
        k32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        k32.AssignProcessToJobObject.restype = ctypes.c_int
        k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

        expected = ctypes.sizeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION)
        if expected != 144:
            log(f"job struct size {expected} unexpected (want 144) - skipping Job Object")
            return None

        job = k32.CreateJobObjectW(None, None)
        if not job:
            log(f"CreateJobObjectW failed (winerror {ctypes.get_last_error()}) "
                "- children stay alive after window close")
            return None

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT,
                                           ctypes.byref(info),
                                           ctypes.sizeof(info)):
            log(f"SetInformationJobObject failed (winerror {ctypes.get_last_error()}) "
                "- children stay alive after window close")
            k32.CloseHandle(job)
            return None

        log("Job Object active: closing this window shuts down all children")
        return job
    except Exception as exc:
        log(f"Job Object setup failed ({exc}) - continuing without it")
        return None


JOB = None
_K32 = None


def assign_job(proc) -> None:
    if JOB is None or _K32 is None or os.name != "nt":
        return
    try:
        if not _K32.AssignProcessToJobObject(JOB, int(proc._handle)):
            log(f"AssignProcessToJobObject failed (winerror {ctypes.get_last_error()})")
    except Exception as exc:
        log(f"job assign failed: {exc}")


def spawn(name: str, argv: list) -> None:
    log(f"[{name}] starting: {' '.join(str(a) for a in argv)}")

    child_env = dict(os.environ)
    child_env.setdefault("PYTHONIOENCODING", "utf-8")
    child_env.setdefault("PYTHONUTF8", "1")
    try:
        proc = subprocess.Popen(
            [str(a) for a in argv],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            cwd=str(ROOT),
            env=child_env,
        )
    except FileNotFoundError:
        log(f"[{name}] FAILED: executable not found: {argv[0]}")
        log(f"[{name}] FIX -> re-run setup.bat, then start.bat again")
        return
    except OSError as exc:
        log(f"[{name}] FAILED to start ({exc})")
        return
    CHILDREN[name] = proc
    _OUT[name] = deque(maxlen=80)
    _SPAWNED_AT[name] = _monotonic()
    assign_job(proc)

    def pump() -> None:
        for line in proc.stdout:
            text = line.rstrip()
            ring = _OUT.get(name)
            if ring is not None:
                ring.append(text)
            print(f"[{name}] {text}", flush=True)

    threading.Thread(target=pump, daemon=True, name=f"{name}-pump").start()

    def watch() -> None:
        code = proc.wait()
        print(f"[  sup] [{name}] exited with code {code}", flush=True)
        CHILDREN.pop(name, None)
        runtime = _monotonic() - _SPAWNED_AT.get(name, _monotonic())
        if name == "lk" and code != 0 and child_saw(name, "could not parse config"):
            heal_yaml()
            _RESTART[name] = 1
        if not _RESTART.get(name):
            return
        fatal = next((m for m in FATAL_MARKERS if child_saw(name, m)), None)
        if fatal:
            _RESTART.pop(name, None)
            log(f"[{name}] stopped retrying: the last run failed with a fatal "
                f"config error ({fatal!r}) - restarting cannot fix this")
            log(f"[{name}] FIX -> correct the setting in user_data/.env.local "
                "or the web UI, then press Settings -> Restart (or POST /api/restart)")
            return
        if runtime < 15.0:
            _CRASHES[name] = _CRASHES.get(name, 0) + 1
        else:
            _CRASHES[name] = 0
        if _CRASHES.get(name, 0) >= RESTART_MAX_CRASHES:
            _RESTART.pop(name, None)
            _CRASHES[name] = 0
            log(f"[{name}] gave up after {RESTART_MAX_CRASHES} fast crashes in "
                "a row - it keeps dying right after start")
            log(f"[{name}] FIX -> read the error above, correct the config, "
                "then press Settings -> Restart (or POST /api/restart) to try again")
            return
        delay = _RESTART[name]
        _RESTART[name] = min(delay * 2, 30)
        log(f"[{name}] restarting in {delay}s...")
        time.sleep(delay)
        spawn(name, argv)

    threading.Thread(target=watch, daemon=True, name=f"{name}-watch").start()


def child_saw(name: str, needle: str) -> bool:
    return any(needle in line for line in _OUT.get(name, ()))


def minimal_yaml(mode: str = "local") -> str:
    key = setting("LIVEKIT_API_KEY", "devkey") or "devkey"
    secret = (setting("LIVEKIT_API_SECRET", "")
              or "porchvoice-secret-1234567890abcdef")
    bind = "127.0.0.1" if mode == "local" else "0.0.0.0"
    external_ip = "true" if mode == "public" else "false"
    return (
        f"port: 7880\n"
        f"bind_addresses:\n"
        f"  - {bind}\n"
        f"\n"
        f"rtc:\n"
        f"  tcp_port: 7881\n"
        f"  port_range_start: 50000\n"
        f"  port_range_end: 50100\n"
        f"  use_external_ip: {external_ip}\n"
        f"\n"
        f"keys:\n"
        f"  {key}: {secret}\n"
    )


def heal_yaml() -> None:
    backup = LIVEKIT_YAML.with_name("livekit.yaml.broken")
    try:
        if LIVEKIT_YAML.exists():
            shutil.copyfile(LIVEKIT_YAML, backup)
            log(f"saved your old config as {backup.name}")
    except OSError as exc:
        log(f"could not back up livekit.yaml ({exc})")
    try:
        LIVEKIT_YAML.write_text(minimal_yaml(current_mode()), encoding="utf-8")
        log("livekit.yaml was rejected by livekit-server - wrote a clean minimal "
            "config it cannot misparse")
        log("the agent joins rooms automatically; no room_config entry is needed")
    except OSError as exc:
        log(f"FAILED to regenerate livekit.yaml ({exc})")
        log("FIX -> open user_data/livekit.yaml in a text editor and simplify it, "
            "or delete it")


def find_cloudflared() -> str:
    if CLOUDFLARED_EXE.is_file():
        return str(CLOUDFLARED_EXE)
    found = shutil.which("cloudflared")
    return found or ""


def find_llama_server() -> str:
    if LLAMA_SERVER_EXE.is_file():
        return str(LLAMA_SERVER_EXE)
    found = shutil.which("llama-server")
    return found or ""


def gguf_asr_files() -> tuple:
    model = mmproj = None
    if GGUF_ASR_DIR.is_dir():
        for f in sorted(GGUF_ASR_DIR.glob("*.gguf")):
            if f.name.lower().startswith("mmproj"):
                if mmproj is None:
                    mmproj = f
            elif model is None:
                model = f
    return model, mmproj


def stt_gguf_cmd() -> tuple:
    cfg = load_config()
    if (cfg.get("STT_ENGINE", "") or "").strip().lower() != "gguf":
        return None, "STT_ENGINE is not gguf"
    base = (cfg.get("STT_GGUF_SERVER", "") or "").strip()
    if base:
        return None, "STT_GGUF_SERVER is set - using the external server"
    exe = find_llama_server()
    if not exe:
        return None, ("llama-server binary not found (bin/llama-server or PATH) - "
                      "run setup step 7, or set STT_GGUF_SERVER to an existing "
                      "llama-server URL")
    model, mmproj = gguf_asr_files()
    if model is None or mmproj is None:
        return None, ("Qwen3-ASR GGUF files missing in user_data/models/gguf-asr "
                      "(need the main model + mmproj-*.gguf) - run setup step 7")
    port = int(str(cfg.get("STT_GGUF_PORT", "8900") or 8900))
    argv = [exe, "-m", str(model), "--mmproj", str(mmproj),
            "--host", "127.0.0.1", "--port", str(port),
            "--ctx-size", "4096", "-ngl", "99", "--no-webui"]
    return argv, ""


def current_mode() -> str:
    return publish_mode(load_config())


def bind_host() -> str:
    return "127.0.0.1" if current_mode() == "local" else "0.0.0.0"


def start_quick_tunnel() -> None:
    exe = find_cloudflared()
    if not exe:
        log("public mode: cloudflared binary not found (bin/cloudflared or PATH) - "
            "the web UI stays local; run setup.sh menu 6 to install it")
        return
    if TUNNEL["proc"] is not None and TUNNEL["proc"].poll() is None:
        return
    cmd = [exe, "tunnel", "--url", f"http://127.0.0.1:{PORT}",
           "--no-autoupdate", "--protocol", "http2"]
    log("[tunnel] starting: " + " ".join(cmd))
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            cwd=str(ROOT))
    except OSError as exc:
        log(f"[tunnel] FAILED to start ({exc})")
        return
    TUNNEL["proc"] = proc
    TUNNEL["started_at"] = time.time()

    def pump() -> None:
        for line in proc.stdout:
            text = line.rstrip()
            TUNNEL["log"].append(text)
            m = TUNNEL_URL_RE.search(text)
            if m and not TUNNEL["url"]:
                TUNNEL["url"] = m.group(0)
                log(f"[tunnel] public URL: {TUNNEL['url']}")
            print(f"[tunnel] {text}", flush=True)

    threading.Thread(target=pump, daemon=True, name="tunnel-pump").start()

    def watch() -> None:
        code = proc.wait()
        TUNNEL["url"] = ""
        TUNNEL["proc"] = None
        log(f"[tunnel] cloudflared exited with code {code}")
        if code != 0 and not _RESTART.get("stop-all"):
            log("[tunnel] retrying in 5s...")
            time.sleep(5)
            start_quick_tunnel()

    threading.Thread(target=watch, daemon=True, name="tunnel-watch").start()


def stop_tunnel() -> None:
    _RESTART["stop-all"] = True
    proc = TUNNEL.get("proc")
    if proc is not None:
        try:
            proc.terminate()
        except OSError:
            pass
    TUNNEL["proc"] = None
    TUNNEL["url"] = ""


def tunnel_info() -> dict:
    proc = TUNNEL.get("proc")
    return {
        "mode": current_mode(),
        "url": TUNNEL.get("url", ""),
        "running": bool(proc is not None and proc.poll() is None),
        "lan_url": "http://" + detect_lan_ip() + f":{PORT}",
        "local_url": f"http://127.0.0.1:{PORT}",
    }


def stop_children() -> None:
    stop_tunnel()
    _RESTART.clear()
    _CRASHES.clear()
    procs = list(CHILDREN.items())
    if _BROWSER is not None:
        procs.append(("ui-browser", _BROWSER))
    for name, proc in procs:
        try:
            proc.terminate()
        except OSError:
            pass


def _tcp_alive(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def livekit_target():
    url = setting("LIVEKIT_URL", "ws://127.0.0.1:7880")
    target = url.replace("ws://", "").replace("wss://", "").split("/")[0]
    host, _, port = target.partition(":")
    return host or "127.0.0.1", int(port or 7880)


def check_gpu():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5,
        )
        line = (out.stdout or "").strip().splitlines()[0] if out.stdout else ""
        if out.returncode == 0 and line:
            return f"YES - {line} "
    except Exception:
        pass
    return "no"


def check_llm() -> str:
    sel = resolve_llm(load_config())
    if sel["kind"] == "none":
        return "not configured (set LLM_BASE_URL or LLM_API_KEY)"
    state = probe_provider(sel["base_url"], sel["api_key"], timeout=4)
    if state["status"] == "up":
        return f"up ({sel['kind']}, HTTP {state['code']})"
    return f"down ({state['detail']})"


def banner() -> None:
    lk_host, lk_port = livekit_target()
    up = False
    for _ in range(10):
        if _tcp_alive(lk_host, lk_port):
            up = True
            break
        time.sleep(0.5)
    lib = WEB_DIR / "livekit-client.js"
    pub = tunnel_info()
    mcp_sel = resolve_mcp(load_config())
    cfg = load_config()
    stt_sel = resolve_stt(cfg)
    tts_sel = resolve_tts(cfg)
    if stt_sel["base_url"]:
        stt_line = f"{stt_sel['engine']} ({stt_sel['base_url']})"
    else:
        stt_line = (f"{stt_sel['engine']} (embedded whisper, model "
                    f"{cfg.get('WHISPER_MODEL', 'small')})")
    log("=" * 68)
    log("PORCHVOICE - a local-first voice assistant")
    log(f"publish mode   : {pub['mode']}")
    log(f"UI + API       : {pub['local_url']}  (LAN: {pub['lan_url']})")
    if pub["mode"] == "public":
        log(f"public URL     : {TUNNEL.get('url') or 'starting...'}")
    log(f"python         : {platform.python_version()}  pid: {os.getpid()}")
    log(f"LiveKit        : ws://{lk_host}:{lk_port} -> {'UP' if up else 'DOWN'}")
    log(f"LLM API        : {check_llm()}")
    log(f"STT            : {stt_line}")
    log(f"TTS            : {tts_sel['engine']}")
    log(f"MCP            : {mcp_sel['url'] or 'not configured'}"
        + (" (bearer set)" if mcp_sel.get("token") else ""))
    log(f"GPU (nvidia)   : {check_gpu()}")
    log(f"livekit-client : {'OK' if lib.is_file() else 'MISSING - re-clone the repo'}")
    log("Children       : livekit-server (auto-restart) + agent worker (auto-restart)")
    log("Stop           : Ctrl+C here, or just close this window - nothing lingers")
    log("=" * 68)


WHISPER_SIZES_MB = {
    "tiny.en": 80, "base.en": 150, "small.en": 500, "small": 500,
    "medium.en": 1530, "medium": 1530, "large-v3-turbo": 1620, "large-v3": 3100,
}


def whisper_dir() -> Path:
    return ROOT / "user_data" / "models" / "whisper"


def piper_dir() -> Path:
    return ROOT / "user_data" / "models" / "piper"


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
        "dir": str(GGUF_ASR_DIR),
        "model": model.name if model else "",
        "mmproj": mmproj.name if mmproj else "",
        "ready": bool(model and mmproj),
        "binary": find_llama_server(),
        "server": (cfg.get("STT_GGUF_SERVER", "") or "").strip()
                  or f"http://127.0.0.1:{cfg.get('STT_GGUF_PORT', '8900')}/v1",
        "engine": (cfg.get("STT_ENGINE", "") or "").strip().lower(),
    }
    return {"presets": presets, "local_folders": local, "gguf": gguf}


def local_model_dir(name: str):
    d = whisper_dir() / name
    return d if (d / "model.bin").is_file() else None


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


def parse_room_from_history(path: Path) -> str:
    m = re.match(r"\d{8}-\d{6}-(.+)\.jsonl$", path.name)
    return m.group(1) if m else "room"


async def api_chat_sessions(_request: web.Request) -> web.Response:
    sessions = []
    if CHAT_DIR.is_dir():
        for f in sorted(CHAT_DIR.glob("*.jsonl"), key=lambda p: p.name, reverse=True):
            turns = 0
            first_user = ""
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            rec = json.loads(line)
                        except ValueError:
                            continue
                        if rec.get("who") in ("user", "bot"):
                            turns += 1
                            if rec.get("who") == "user" and not first_user:
                                first_user = rec.get("text", "")[:80]
            except OSError:
                continue
            if turns:
                try:
                    mtime = int(f.stat().st_mtime)
                except OSError:
                    mtime = 0
                sessions.append({
                    "file": f.name,
                    "room": parse_room_from_history(f),
                    "turns": turns,
                    "first": first_user,
                    "mtime": mtime,
                })
    return web.json_response({"sessions": sessions[:100]})


async def api_chat_session(request: web.Request) -> web.Response:


    name = request.query.get("file", "")
    room = request.query.get("room", "")
    f = None
    if room:
        if not re.fullmatch(r"[\w-]{1,64}", room or ""):
            return web.json_response({"error": "bad room name"}, status=400)
        cands = sorted(CHAT_DIR.glob(f"*-{room}.jsonl"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        f = cands[0] if cands else None
        if f is None:
            return web.json_response({"file": "", "messages": []})
    else:
        if not re.fullmatch(r"[\w.-]+\.jsonl", name or ""):
            return web.json_response({"error": "bad file name"}, status=400)
        f = CHAT_DIR / name
    if not f.is_file():
        return web.json_response({"error": "not found"}, status=404)
    msgs = []
    try:
        with open(f, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                who = rec.get("who")
                if who in ("user", "bot", "error") and rec.get("text"):
                    msgs.append({
                        "who": who, "text": rec["text"],
                        "ts": rec.get("ts", ""), "id": rec.get("id", ""),
                        "think": rec.get("think", ""),
                    })
                elif who == "system":
                    msgs.append({
                        "who": "system", "text": rec.get("text", ""),
                        "ts": rec.get("ts", ""), "id": "",
                    })
    except OSError as exc:
        return web.json_response({"error": str(exc)}, status=500)
    return web.json_response({"file": f.name, "messages": msgs})


async def api_chat_clear(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "invalid json"}, status=400)
    room = (body.get("room") or "").strip() if isinstance(body, dict) else ""
    if not re.fullmatch(r"[\w-]{1,64}", room or ""):
        return web.json_response({"error": "bad room name"}, status=400)
    if not CHAT_DIR.is_dir():
        return web.json_response({"ok": True, "cleared": []})
    name_re = re.compile(r"^\d{8}-\d{6}-[\w.-]+\.jsonl$")
    cleared = []
    for f in sorted(CHAT_DIR.glob(f"*-{room}.jsonl")):
        if not name_re.match(f.name):
            continue
        try:


            with open(f, "w", encoding="utf-8"):
                pass
            cleared.append(f.name)
        except OSError as exc:
            log(f"could not clear {f.name}: {exc}")
    if cleared:
        log(f"chat history cleared for room {room}: {', '.join(cleared)}")
    return web.json_response({"ok": True, "cleared": cleared})


async def api_config_get(_request: web.Request) -> web.Response:
    return web.json_response({"config": mask_secrets(env_dict()),
                              "secret_keys": list(SECRET_KEYS)})


async def api_config_post(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "invalid json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid json"}, status=400)
    updates = body.get("config")
    if not isinstance(updates, dict):
        return web.json_response({"error": "missing config object"}, status=400)
    current = env_dict()
    allowed = set(current) | {
        "LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET",
        "PUBLISH_MODE", "PUBLIC_PORT",
        "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL",
        "LLM_COMPAT_MODE", "LLM_STREAMING",
        "LLM_THINKING", "LLM_SPEAK_THINKING",
        "STT_BASE_URL", "STT_API_KEY", "STT_MODEL", "STT_ENGINE",
        "STT_GGUF_SERVER", "STT_GGUF_PORT", "STT_GGUF_MODEL",
        "TTS_BASE_URL", "TTS_API_KEY", "TTS_MODEL",
        "TTS_ENGINE", "TTS_VOICE", "TTS_SPEED", "KOKORO_VOICE",
        "MCP_URL", "MCP_AUTH_TOKEN", "MCP_TIMEOUT",
        "WHISPER_MODEL", "WHISPER_DEVICE", "WHISPER_COMPUTE", "WHISPER_LANGUAGE",
        "WHISPER_BEAM", "WHISPER_VAD_FILTER", "WHISPER_TEMPERATURE",
        "WHISPER_MIN_LOGPROB", "WHISPER_MAX_COMPRATIO",
        "WHISPER_INITIAL_PROMPT",
        "CAP_ECHO_CANCEL", "CAP_NOISE_SUPPRESS", "CAP_AUTO_GAIN",
        "VAD_MIN_SILENCE", "VAD_ACTIVATION_THRESHOLD",
        "ENDPOINTING_DELAY", "ECHO_GUARD", "ECHO_GUARD_TAIL", "LOG_LEVEL",
        "AUTO_OPEN", "UI_WINDOW", "CLOSE_WITH_WINDOW",
        "UI_BROWSER", "UI_PROFILE",
    }
    for k, v in updates.items():
        if k not in allowed or not isinstance(v, str):
            continue
        v = v.strip()
        if k in SECRET_KEYS and v == SECRET_SENTINEL:
            continue
        if not v and str(CONFIG_DEFAULTS.get(k, "") or ""):
            continue
        if k in SECRET_KEYS and v and not is_encrypted(v):
            v = encrypt_value(v)
        current[k] = v
    lines = ["# PorchVoice settings (saved from the web UI)",
             "# Applies to the NEXT conversation after you click Save."]
    for k, v in sorted(current.items()):
        lines.append(f"{k}={v}")
    try:
        ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
        ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        log(f"config save FAILED: {exc}")
        return web.json_response({"error": f"could not write {ENV_FILE.name}: {exc}"},
                                 status=500)
    log("config saved (applies to the next conversation)")
    return web.json_response({"ok": True, "config": mask_secrets(current)})


async def api_reveal(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "invalid json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid json"}, status=400)
    key = str(body.get("key") or "")
    if key not in SECRET_KEYS:
        return web.json_response({"error": "unknown secret"}, status=400)
    value = load_config().get(key, "")
    return web.json_response({"key": key, "value": value, "set": bool(value)})


async def api_heartbeat(_request: web.Request) -> web.Response:
    global _LAST_BEAT, _CLOSE_PENDING_AT
    _LAST_BEAT = _monotonic()
    _CLOSE_PENDING_AT = 0.0
    return web.Response(status=204)


PROBE_WHY = {
    401: "auth failed (check the API key)",
    402: "usage limit reached (out of credits or quota)",
    403: "forbidden (key lacks access)",
    429: "rate limited (usage limit hit)",
}


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


def check_stt_provider(cfg: dict) -> dict:
    sel = resolve_stt(cfg)
    if sel["engine"] in ("remote", "gguf") and sel["base_url"]:
        state = probe_provider(sel["base_url"], sel["api_key"])
        state["engine"] = sel["engine"]
        state["url"] = sel["base_url"]
        return state
    models = list_stt_models()
    n = len(models.get("local_folders", []))
    return {"status": "embedded", "engine": "whisper",
            "detail": f"model {cfg.get('WHISPER_MODEL', 'small')}, {n} downloaded",
            "url": ""}


def check_tts_provider(cfg: dict) -> dict:
    sel = resolve_tts(cfg)
    if sel["engine"] == "remote" and sel["base_url"]:
        state = probe_provider(sel["base_url"], sel["api_key"])
        state["engine"] = "remote"
        state["url"] = sel["base_url"]
        return state
    if sel["engine"] == "kokoro":
        from local_kokoro import kokoro_ready

        ready = kokoro_ready()
        return {"status": "embedded", "engine": "kokoro",
                "detail": ("model ready, voice " + (sel["kokoro_voice"] or "af_heart"))
                          if ready else "model files missing - run setup voice step",
                "ready": ready, "url": ""}
    n = len(list(piper_dir().glob("*.onnx"))) if piper_dir().is_dir() else 0
    return {"status": "embedded", "engine": "piper",
            "detail": f"voice {sel['voice'] or '(none)'}, {n} downloaded",
            "ready": n > 0, "url": ""}


async def api_check(_request: web.Request) -> web.Response:
    lk_host, lk_port = livekit_target()
    lk_up = await asyncio.to_thread(_tcp_alive, lk_host, lk_port)
    llm_state = await asyncio.to_thread(check_llm)
    gpu_state = await asyncio.to_thread(check_gpu)
    try:
        kids = {name: ("up" if p.poll() is None else f"exit {p.returncode}")
                for name, p in CHILDREN.items()}
    except Exception:
        kids = {}
    lib_ok = (WEB_DIR / "livekit-client.js").is_file()
    env_ok = ENV_FILE.is_file()
    chat_ok, chat_why = True, ""
    try:
        CHAT_DIR.mkdir(parents=True, exist_ok=True)
        probe = CHAT_DIR / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        chat_ok, chat_why = False, str(exc)[:160]
    n_logs = 0
    try:
        n_logs = sum(1 for _ in CHAT_DIR.glob("*.jsonl")) if CHAT_DIR.is_dir() else 0
    except OSError:
        pass
    sel = resolve_llm(load_config())
    cfg = load_config()
    stt_state = await asyncio.to_thread(check_stt_provider, cfg)
    tts_state = await asyncio.to_thread(check_tts_provider, cfg)
    return web.json_response({
        "livekit": {"status": "up" if lk_up else "down",
                    "url": setting("LIVEKIT_URL")},
        "llm": {"status": llm_state, "url": sel["base_url"], "kind": sel["kind"]},
        "stt": stt_state,
        "tts": tts_state,
        "publish": tunnel_info(),
        "gpu": gpu_state,
        "children": kids,
        "lib_file": {"exists": lib_ok},
        "env_file": {"exists": env_ok},
        "chat_log": {"writable": chat_ok, "error": chat_why, "files": n_logs},
    })


async def api_restart(_request: web.Request) -> web.Response:
    if not CHILDREN:
        return web.json_response({"error": "nothing is running"}, status=409)
    names = list(CHILDREN.keys())
    for name in names:
        _CRASHES.pop(name, None)
        _RESTART[name] = 1
        try:
            CHILDREN[name].terminate()
        except OSError:
            pass
    log(f"restart requested from the UI - respawning: {', '.join(names)}")
    return web.json_response({"ok": True, "restarting": names})


async def api_window_closing(_request: web.Request) -> web.Response:
    global _CLOSE_PENDING_AT
    _CLOSE_PENDING_AT = _monotonic()
    _CLOSE_CHECK.set()
    return web.Response(status=204)


async def api_browsers(_request: web.Request) -> web.Response:
    return web.json_response({
        "browsers": detect_browsers(),
        "chosen": setting("UI_BROWSER", ""),
    })


async def api_publish(_request: web.Request) -> web.Response:
    return web.json_response(tunnel_info())


async def api_mcp_status(_request: web.Request) -> web.Response:
    sel = resolve_mcp(load_config())
    if not sel["enabled"]:
        return web.json_response({"enabled": False, "ok": False,
                                  "url": "", "tools": [],
                                  "error": "MCP_URL not configured"})
    result = await probe_mcp(sel["url"], sel["token"], sel["timeout"])
    result["enabled"] = True
    result["url"] = sel["url"]
    result["kind"] = sel["kind"]
    return web.json_response(result)


async def api_models_stt(_request: web.Request) -> web.Response:
    return web.json_response(list_stt_models())


async def api_models_tts(_request: web.Request) -> web.Response:
    return web.json_response(list_tts_models())


async def api_llm_models(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    cfg = load_config()
    base = str(body.get("url") or cfg.get("LLM_BASE_URL", "") or "").strip()
    if "key" in body and body.get("key") is not None:
        key = str(body.get("key")).strip()
    else:
        key = (cfg.get("LLM_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "") or "").strip()
    if not base and key:
        base = "https://api.openai.com/v1"
    if not re.match(r"^https?://", base):
        return web.json_response({"error": "base URL must start with http:// or https://",
                                  "models": []})
    url = base.rstrip("/") + "/models"
    headers = {"Authorization": "Bearer " + key} if key else {}

    def _fetch():
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    try:
        data = await asyncio.to_thread(_fetch)
    except urllib.error.HTTPError as e:
        why = "auth failed (check the API key)" if e.code in (401, 403) \
            else f"HTTP {e.code}"
        return web.json_response({"error": why, "models": []})
    except Exception as exc:
        return web.json_response({"error": str(exc)[:160], "models": []})
    if isinstance(data, dict):
        items = data.get("data") or data.get("models") or []
    elif isinstance(data, list):
        items = data
    else:
        items = []
    models = []
    for m in items:
        if isinstance(m, dict):
            mid = m.get("id") or m.get("name") or m.get("model")
            if mid:
                models.append(str(mid))
        elif isinstance(m, str):
            models.append(m)
    models = sorted(set(models))
    return web.json_response({"base": base, "count": len(models), "models": models})


def count_local_models() -> dict:
    stt = 0
    base = whisper_dir()
    if base.is_dir():
        stt = sum(1 for d in base.iterdir() if d.is_dir() and (d / "model.bin").is_file())
    tts = len(list(piper_dir().glob("*.onnx"))) if piper_dir().is_dir() else 0
    return {"stt": stt, "tts": tts}


async def health(_request: web.Request) -> web.Response:
    lk_host, lk_port = livekit_target()
    lk_up = await asyncio.to_thread(_tcp_alive, lk_host, lk_port)
    llm_state = await asyncio.to_thread(check_llm)
    gpu_state = await asyncio.to_thread(check_gpu)
    try:
        kids = {name: ("up" if p.poll() is None else f"exit {p.returncode}")
                for name, p in CHILDREN.items()}
    except Exception:
        kids = {}
    lib_ok = (WEB_DIR / "livekit-client.js").is_file()
    sel = resolve_llm(load_config())
    mcp_sel = resolve_mcp(load_config())
    return web.json_response({
        "ui": "ok",
        "python": platform.python_version(),
        "livekit": {"status": "up" if lk_up else "down", "url": setting("LIVEKIT_URL")},
        "llm": {"status": llm_state, "url": sel["base_url"], "kind": sel["kind"]},
        "mcp": {"enabled": mcp_sel["enabled"], "url": mcp_sel["url"],
                "kind": mcp_sel["kind"]},
        "publish": tunnel_info(),
        "gpu": gpu_state,
        "models": count_local_models(),
        "children": kids,
        "venv": venv_python().exists(),
        "lib_file": {"exists": lib_ok},
    })


async def index(_request: web.Request) -> web.FileResponse:
    return web.FileResponse(WEB_DIR / "index.html")


async def lib(_request: web.Request) -> web.FileResponse:
    return web.FileResponse(WEB_DIR / "livekit-client.js")


async def favicon(_request: web.Request) -> web.Response:
    return web.Response(status=204)


async def token(request: web.Request) -> web.Response:
    identity = (request.query.get("identity") or f"user-{os.urandom(2).hex()}").strip()
    if not re.fullmatch(r"[\w-]{1,64}", identity):
        return web.json_response({"error": "bad identity"}, status=400)
    room = (request.query.get("room") or "").strip()
    if room:
        if not re.fullmatch(r"[\w-]{1,64}", room):
            return web.json_response({"error": "bad room name"}, status=400)
    else:
        room = f"voice-{os.urandom(4).hex()}"
    key = setting("LIVEKIT_API_KEY", "devkey")
    secret = setting("LIVEKIT_API_SECRET", "")
    if not key or not secret:
        log("token REFUSED: LIVEKIT_API_KEY/SECRET missing - check user_data/.env.local")
        return web.json_response(
            {"error": "server has no LIVEKIT_API_KEY/SECRET configured"}, status=500)
    tk = (
        lk_api.AccessToken(key, secret)
        .with_identity(identity)
        .with_name(identity)
        .with_grants(lk_api.VideoGrants(room_join=True, room=room))
    )
    jwt = tk.to_jwt()
    client_url = client_livekit_url(request)
    log(f"token issued identity={identity} room={room} url={client_url}")
    return web.json_response({"url": client_url, "token": jwt,
                              "room": room, "identity": identity})


def client_livekit_url(request: web.Request) -> str:
    mode = current_mode()
    req_host = (request.headers.get("Host") or "").split(":")[0].lower()
    forwarded_proto = (request.headers.get("X-Forwarded-Proto") or "").lower()
    tunnel_host = ""
    if TUNNEL.get("url"):
        tunnel_host = TUNNEL["url"].split("//", 1)[1].split("/")[0].lower()
    if mode == "public" and tunnel_host and (
            req_host == tunnel_host or forwarded_proto == "https"):
        return f"wss://{tunnel_host}/lk"
    if mode == "public" and forwarded_proto == "https":
        return f"wss://{req_host}/lk"
    if mode in ("wifi", "public"):
        return "ws://" + detect_lan_ip() + ":7880"
    return setting("LIVEKIT_URL", "ws://127.0.0.1:7880")


HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
}


async def lk_proxy(request: web.Request) -> web.StreamResponse:
    suffix = request.match_info.get("suffix", "")
    if (request.headers.get("Upgrade", "") or "").lower() == "websocket":
        return await lk_ws_proxy(request, suffix)
    return await lk_http_proxy(request, suffix)


def _upstream_base() -> str:
    url = setting("LIVEKIT_URL", "ws://127.0.0.1:7880")
    return url.replace("wss://", "https://").replace("ws://", "http://").rstrip("/")


async def lk_http_proxy(request: web.Request, suffix: str) -> web.Response:
    qs = ("?" + request.query_string) if request.query_string else ""
    target = f"{_upstream_base()}/{suffix}{qs}"
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in HOP_BY_HOP}
    body = await request.read()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.request(
                    request.method, target, headers=headers, data=body,
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=15)) as up:
                payload = await up.read()
                out_headers = {k: v for k, v in up.headers.items()
                               if k.lower() not in HOP_BY_HOP}
                return web.Response(status=up.status, body=payload,
                                    headers=out_headers)
    except Exception as exc:
        return web.json_response({"error": f"lk proxy failed: {exc}"}, status=502)


async def lk_ws_proxy(request: web.Request, suffix: str) -> web.StreamResponse:
    qs = ("?" + request.query_string) if request.query_string else ""
    target = (_upstream_base().replace("http://", "ws://")
              .replace("https://", "wss://")) + f"/{suffix}{qs}"
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in HOP_BY_HOP
               and k.lower() not in ("sec-websocket-key", "sec-websocket-version",
                                     "sec-websocket-extensions",
                                     "sec-websocket-protocol")}
    ws_out = web.WebSocketResponse()
    await ws_out.prepare(request)
    session = aiohttp.ClientSession()
    ws_up = None
    try:
        try:
            ws_up = await session.ws_connect(target, headers=headers)
        except Exception as exc:
            log(f"lk ws proxy upstream connect failed ({exc})")
            await ws_out.close()
            return ws_out

        async def pump(src, dst, name):
            try:
                async for msg in src:
                    if msg.type == WSMsgType.TEXT:
                        await dst.send_str(msg.data)
                    elif msg.type == WSMsgType.BINARY:
                        await dst.send_bytes(msg.data)
                    elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING,
                                      WSMsgType.CLOSED, WSMsgType.ERROR):
                        break
            except Exception:
                pass
            finally:
                try:
                    await dst.close()
                except Exception:
                    pass

        await asyncio.gather(
            pump(ws_up, ws_out, "upstream->client"),
            pump(ws_out, ws_up, "client->upstream"),
            return_exceptions=True,
        )
        return ws_out
    finally:
        if ws_up is not None and not ws_up.closed:
            await ws_up.close()
        await session.close()


@web.middleware
async def log_and_nocache(request: web.Request, handler):
    status = 500
    try:
        resp = await handler(request)
        status = resp.status
    except web.HTTPException as e:
        status = e.status
        raise
    finally:

        if request.path not in ("/api/heartbeat", "/api/window/closing"):
            log(f"{request.method} {request.path_qs} -> {status}")
        try:
            resp.headers["Cache-Control"] = "no-store"
        except UnboundLocalError:
            pass
    return resp


_BROWSER_CATALOG = [
    ("brave", "Brave", True, [
        ("nt", ("BraveSoftware", "Brave-Browser", "Application", "brave.exe")),
        ("darwin", ("Brave Browser.app", "Contents", "MacOS", "Brave Browser")),
        ("unix", ("brave-browser", "brave")),
    ]),
    ("chrome", "Google Chrome", True, [
        ("nt", ("Google", "Chrome", "Application", "chrome.exe")),
        ("darwin", ("Google Chrome.app", "Contents", "MacOS", "Google Chrome")),
        ("unix", ("google-chrome", "google-chrome-stable", "chromium",
                  "chromium-browser")),
    ]),
    ("edge", "Microsoft Edge", True, [
        ("nt", ("Microsoft", "Edge", "Application", "msedge.exe")),
        ("darwin", ("Microsoft Edge.app", "Contents", "MacOS",
                    "Microsoft Edge")),
        ("unix", ("microsoft-edge", "microsoft-edge-stable")),
    ]),
    ("vivaldi", "Vivaldi", True, [
        ("nt", ("Vivaldi", "Application", "vivaldi.exe")),
        ("darwin", ("Vivaldi.app", "Contents", "MacOS", "Vivaldi")),
        ("unix", ("vivaldi", "vivaldi-stable")),
    ]),
    ("opera", "Opera", True, [
        ("nt", ("Opera", "launcher.exe")),
        ("darwin", ("Opera.app", "Contents", "MacOS", "Opera")),
        ("unix", ("opera",)),
    ]),
    ("chromium", "Chromium", True, [
        ("nt", ("Chromium", "Application", "chrome.exe")),
        ("darwin", ("Chromium.app", "Contents", "MacOS", "Chromium")),
        ("unix", ("chromium", "chromium-browser")),
    ]),
    ("firefox", "Firefox", False, [
        ("nt", ("Mozilla Firefox", "firefox.exe")),
        ("darwin", ("Firefox.app", "Contents", "MacOS", "firefox")),
        ("unix", ("firefox", "firefox-esr")),
    ]),
]

_APP_PATHS_EXES = {"brave": "brave.exe", "chrome": "chrome.exe",
                   "edge": "msedge.exe", "vivaldi": "vivaldi.exe",
                   "opera": "opera.exe", "chromium": "chromium.exe",
                   "firefox": "firefox.exe"}


def _win_prog_files_dirs() -> list:
    pf = os.environ.get("ProgramFiles", "C:\\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")
    lap = os.environ.get("LocalAppData", "")
    return [d for d in (lap, pf, pf86) if d]


def _win_app_paths_exe(exe_name: str):
    try:
        import winreg
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(
                        root,
                        r"SOFTWARE\Microsoft\Windows\CurrentVersion"
                        r"\App Paths" + "\\" + exe_name) as k:
                    val, _typ = winreg.QueryValueEx(k, None)
            except OSError:
                continue
            if val:
                p = Path(str(val).strip().strip('"').split("\"")[0])
                if p.is_file():
                    return p
    except Exception:
        pass
    return None


def _win_default_browser_exe():
    if os.name != "nt":
        return None
    try:
        import winreg
        for assoc in ("https", "http"):
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                    r"Software\Microsoft\Windows\Shell"
                                    r"\Associations\UrlAssociations" + "\\"
                                    + assoc + r"\UserChoice") as k:
                    progid, _typ = winreg.QueryValueEx(k, "ProgId")
            except OSError:
                continue
            if not progid:
                continue
            try:
                with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT,
                                    progid + r"\shell\open\command") as k:
                    cmd, _typ = winreg.QueryValueEx(k, None)
            except OSError:
                continue
            cmd = (cmd or "").strip()
            if not cmd:
                continue

            if cmd.startswith('"'):
                exe = cmd.split('"', 2)[1]
            else:
                exe = cmd.split(" ", 1)[0]
            p = Path(exe)
            if p.is_file():
                return p
    except Exception:
        pass
    return None


def _browser_id_for_exe(exe: Path):
    base = exe.name.lower()
    for bid, _name, chromium, _cands in _BROWSER_CATALOG:
        if base == _APP_PATHS_EXES[bid]:
            return bid, chromium
    if "brave" in base:
        return "brave", True
    if "msedge" in base or "edge" in base:
        return "edge", True
    if "firefox" in base:
        return "firefox", False
    if "vivaldi" in base:
        return "vivaldi", True
    if "opera" in base:
        return "opera", True
    if "chrome" in base:
        return "chrome", True
    return "custom", True


def _catalog_paths(bid: str) -> list:
    out = []
    for cid, _name, _chromium, cands in _BROWSER_CATALOG:
        if cid != bid:
            continue
        for plat, parts in cands:
            if plat == "nt" and os.name == "nt":
                for root in _win_prog_files_dirs():
                    out.append(Path(root).joinpath(*parts))
                ap = _win_app_paths_exe(parts[-1])
                if ap:
                    out.append(ap)
            elif plat == "darwin" and sys.platform == "darwin":
                out.append(Path("/Applications").joinpath(*parts))
            elif plat == "unix" and os.name != "nt" and sys.platform != "darwin":
                for exe in parts:
                    p = shutil.which(exe)
                    if p:
                        out.append(Path(p))
        break
    seen, uniq = set(), []
    for p in out:
        try:
            rp = str(p.resolve()).lower()
            if p.is_file() and os.access(str(p), os.X_OK) and rp not in seen:
                seen.add(rp)
                uniq.append(p)
        except OSError:
            continue
    return uniq


def detect_browsers() -> list:
    default = _win_default_browser_exe() if os.name == "nt" else None
    found, seen = [], set()
    if default is not None:
        try:
            rp = str(default.resolve()).lower()
        except OSError:
            rp = str(default).lower()
        bid, chromium = _browser_id_for_exe(default)
        found.append({"id": bid, "name": _display_name(bid, default),
                      "path": str(default), "chromium": chromium,
                      "default": True})
        seen.add(rp)
    for bid, name, chromium, _cands in _BROWSER_CATALOG:
        for p in _catalog_paths(bid):
            try:
                rp = str(p.resolve()).lower()
            except OSError:
                rp = str(p).lower()
            if rp in seen:
                continue
            seen.add(rp)
            found.append({"id": bid, "name": name, "path": str(p),
                          "chromium": chromium, "default": False})
    return found


def _display_name(bid: str, exe: Path) -> str:
    for cid, name, _chromium, _cands in _BROWSER_CATALOG:
        if cid == bid:
            return name
    return exe.stem.capitalize()


def load_window_state() -> dict:
    try:
        with open(WINDOW_STATE_FILE, "r", encoding="utf-8") as f:
            st = json.load(f)
        if isinstance(st, dict):
            return st
    except Exception:
        pass
    return {}


def save_window_state(st: dict) -> None:
    try:
        WINDOW_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = WINDOW_STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(st), encoding="utf-8")
        os.replace(tmp, WINDOW_STATE_FILE)
    except Exception as exc:
        log(f"could not save window state: {exc}")


if os.name == "nt":
    class _RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    class _MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.wintypes.DWORD),
                    ("rcMonitor", _RECT), ("rcWork", _RECT),
                    ("dwFlags", ctypes.wintypes.DWORD)]

_WINDOW_TITLE = "PorchVoice"


def _win32():
    if os.name != "nt":
        return None
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.IsWindow.argtypes = [ctypes.wintypes.HWND]
    u.IsWindow.restype = ctypes.c_bool
    u.IsWindowVisible.argtypes = [ctypes.wintypes.HWND]
    u.IsWindowVisible.restype = ctypes.c_bool
    u.IsZoomed.argtypes = [ctypes.wintypes.HWND]
    u.IsZoomed.restype = ctypes.c_bool
    u.IsIconic.argtypes = [ctypes.wintypes.HWND]
    u.IsIconic.restype = ctypes.c_bool
    u.GetWindowTextLengthW.argtypes = [ctypes.wintypes.HWND]
    u.GetWindowTextLengthW.restype = ctypes.c_int
    u.GetWindowTextW.argtypes = [ctypes.wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
    u.GetWindowRect.argtypes = [ctypes.wintypes.HWND, ctypes.POINTER(_RECT)]
    u.GetWindowThreadProcessId.argtypes = [ctypes.wintypes.HWND, ctypes.POINTER(ctypes.wintypes.DWORD)]
    u.GetWindow.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.UINT]
    u.GetWindow.restype = ctypes.wintypes.HWND
    u.MonitorFromWindow.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.DWORD]
    u.MonitorFromWindow.restype = ctypes.wintypes.HMONITOR
    u.GetMonitorInfoW.argtypes = [ctypes.wintypes.HMONITOR, ctypes.POINTER(_MONITORINFO)]
    u.ShowWindow.argtypes = [ctypes.wintypes.HWND, ctypes.c_int]
    try:
        u.GetDpiForWindow.restype = ctypes.c_uint
        u.GetDpiForWindow.argtypes = [ctypes.wintypes.HWND]
    except AttributeError:
        pass
    return u


def _window_title(u, hwnd) -> str:
    n = u.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    u.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value or ""


def _find_ui_window(u, pid: int, deadline: float):
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    u.EnumWindows.argtypes = [WNDENUMPROC, ctypes.wintypes.LPARAM]
    while _monotonic() < deadline:
        by_pid, by_title = [], []

        def _cb(hwnd, _lparam, _by_pid=by_pid, _by_title=by_title):
            try:
                if not u.IsWindowVisible(hwnd):
                    return True
                wpid = ctypes.wintypes.DWORD(0)
                u.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
                title = _window_title(u, hwnd)
                if not title or u.GetWindow(hwnd, 4):
                    return True
                if pid and wpid.value == pid:
                    _by_pid.append((hwnd, title))
                if title == _WINDOW_TITLE:
                    _by_title.append(hwnd)
            except Exception:
                pass
            return True

        cb = WNDENUMPROC(_cb)
        u.EnumWindows(cb, 0)
        if by_pid:
            return by_pid[0][0], True
        if by_title:
            return by_title[0], False
        time.sleep(0.5)
    return None, False


def _sample_window(u, hwnd) -> dict:
    rc = _RECT()
    if not u.GetWindowRect(hwnd, ctypes.byref(rc)):
        return {}
    zoomed = bool(u.IsZoomed(hwnd))
    fullscreen = False
    try:
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        if u.GetMonitorInfoW(u.MonitorFromWindow(hwnd, 2), ctypes.byref(mi)):
            m = mi.rcMonitor
            tol = 8
            fullscreen = (abs(rc.left - m.left) <= tol and abs(rc.top - m.top) <= tol
                          and abs(rc.right - m.right) <= tol and abs(rc.bottom - m.bottom) <= tol)
    except Exception:
        pass
    try:
        dpi = u.GetDpiForWindow(hwnd) or 96
    except Exception:
        dpi = 96
    mode = "fullscreen" if fullscreen else ("maximized" if zoomed else "windowed")
    return {"mode": mode, "x": int(rc.left), "y": int(rc.top),
            "w": int(rc.right - rc.left), "h": int(rc.bottom - rc.top),
            "dpi": int(dpi)}


def track_window(proc, saved_mode: str, chromium: bool = True) -> None:
    global _WIN_MINIMIZED
    u = _win32()
    if u is None:
        return
    pid = proc.pid if hasattr(proc, "pid") else 0
    hwnd, owned = _find_ui_window(u, pid, _monotonic() + 15)
    if not hwnd:


        log("could not track the app window (browser reused?) - heartbeat close-detection only")
        return
    if saved_mode in ("maximized", "fullscreen") and not u.IsZoomed(hwnd):
        u.ShowWindow(hwnd, 3)
    log("app window tracked - window position/size/mode will be remembered")
    last = None
    try:
        while True:


            _CLOSE_CHECK.wait(1.0)
            _CLOSE_CHECK.clear()
            if chromium and owned and proc.poll() is not None:

                request_shutdown("app window closed (browser process exited)", fast=True)
                return
            if not chromium and proc.poll() is not None:

                return
            if not u.IsWindow(hwnd):
                request_shutdown("app window closed", fast=True)
                return
            _WIN_MINIMIZED = bool(u.IsIconic(hwnd))
            if _WIN_MINIMIZED:
                continue
            try:
                st = _sample_window(u, hwnd)
            except Exception:
                continue
            if st and st != last:
                save_window_state(st)
                last = st
    finally:
        _WIN_MINIMIZED = False


def app_browser_candidates() -> list:
    chosen = setting("UI_BROWSER", "").strip().strip('"')
    installed = detect_browsers()
    order = []

    def add(path, bid, chromium):
        try:
            key = str(Path(path).resolve()).lower()
        except OSError:
            key = str(path).lower()
        if not any(str(c[0]).lower() == key for c in order):
            order.append((Path(path), bid, chromium))

    if chosen and chosen.lower() not in ("auto", "default"):
        low = chosen.lower()
        exact = next((b for b in installed if b["id"] == low), None)
        if exact:
            add(exact["path"], exact["id"], bool(exact["chromium"]))
        else:
            custom = Path(chosen)
            if custom.is_file() and os.access(str(custom), os.X_OK):
                bid, chromium = _browser_id_for_exe(custom)
                add(custom, bid, chromium)
    default = next((b for b in installed if b.get("default")), None)
    if default:
        add(default["path"], default["id"], bool(default["chromium"]))
    for b in installed:
        add(b["path"], b["id"], bool(b["chromium"]))
    return order


def _spawn_browser(args: list) -> subprocess.Popen:
    if os.name == "nt":
        return subprocess.Popen(args, close_fds=True, creationflags=0x08000000)
    return subprocess.Popen(args, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)


def open_ui_when_ready() -> None:
    global _BROWSER, _BROWSER_JOBBED
    url = f"http://{HOST}:{PORT}/"
    deadline = time.time() + 20
    while time.time() < deadline:
        if _tcp_alive(HOST, PORT, timeout=0.5):
            break
        time.sleep(0.25)
    if setting("AUTO_OPEN", "1") != "1":
        log(f"UI ready at {url} (AUTO_OPEN=0 - not opening anything)")
        return

    if setting("UI_WINDOW", "1") == "1":
        candidates = app_browser_candidates()
        if candidates:
            tried = []
            for exe, bid, chromium in candidates:
                tried.append(exe.name)
                own_profile = (setting("UI_PROFILE", "1") == "1") and chromium
                try:
                    st = load_window_state()
                    mode = str(st.get("mode") or "windowed")
                    args = [str(exe)]
                    if chromium:
                        args.append(f"--app={url}")
                        args.append("--no-first-run")
                        args.append("--no-default-browser-check")
                        if own_profile:
                            prof = ROOT / "user_data" / "browser-profile"
                            prof.mkdir(parents=True, exist_ok=True)
                            args.append(f"--user-data-dir={prof}")
                    else:
                        args.append("--new-window")
                        args.append(url)
                        mode = "windowed"
                    if chromium and mode == "maximized":
                        args.append("--start-maximized")
                    elif chromium and mode == "fullscreen":
                        args.append("--start-fullscreen")
                    elif chromium:
                        mode = "windowed"
                        w, h = int(st.get("w") or 0), int(st.get("h") or 0)
                        x, y = int(st.get("x") or 0), int(st.get("y") or 0)
                        dpi = int(st.get("dpi") or 96) or 96
                        if 400 <= w <= 12000 and 300 <= h <= 8000:
                            scale = 96.0 / dpi
                            args.append(f"--window-size={round(w * scale)},{round(h * scale)}")
                            if -w + 100 <= x <= 20000 and -40 <= y <= 20000:
                                args.append(f"--window-position={round(x * scale)},{round(y * scale)}")
                        else:
                            args.append("--window-size=1240,860")
                    proc = _spawn_browser(args)
                except Exception as exc:
                    log(f"{exe.name} failed to launch ({exc}) - trying the next browser")
                    continue
                _BROWSER = proc
                assign_job(proc)
                _BROWSER_JOBBED = JOB is not None and os.name == "nt"
                log(f"opened standalone app window via {exe.name} ({bid}) at {url} "
                    f"(mode: {mode})")
                if own_profile:
                    log("(own browser profile: user_data/browser-profile - the app "
                        "window is tracked exactly, and closing the terminal "
                        "closes it too; set UI_PROFILE=0 to use your normal profile)")
                if _BROWSER_JOBBED:
                    log("(browser bound to this console - closing the terminal "
                        "closes the app window too)")
                if mode == "windowed" and chromium and not int(st.get("w") or 0):
                    log("(first run - default size; the window you arrange is "
                        "remembered for next time)")
                log("(set UI_WINDOW=0 to use a normal browser tab instead)")
                if os.name == "nt":
                    threading.Thread(target=track_window,
                                     args=(proc, mode, chromium),
                                     daemon=True, name="win-track").start()
                else:
                    threading.Thread(target=_unix_watch_browser, args=(proc, chromium),
                                     daemon=True, name="win-track").start()
                if own_profile:
                    try:
                        if proc.wait(timeout=6) is not None:
                            log(f"{exe.name} exited immediately - "
                                f"retrying with the next browser")
                            _BROWSER = None
                            continue
                    except subprocess.TimeoutExpired:
                        pass
                return
            log(f"none of the detected browsers worked ({', '.join(tried)}) "
                "- opening the system default")
        else:
            log("no supported browser found for a standalone window - using the browser")
    try:
        if os.name == "nt":
            os.startfile(url)
        else:
            import webbrowser

            webbrowser.open(url)
        log(f"opened {url} in your browser (set AUTO_OPEN=0 to disable)")
    except Exception as exc:
        log(f"could not auto-open browser ({exc}) - open {url} manually")


def _unix_watch_browser(proc, chromium: bool) -> None:
    if not chromium:
        return
    proc.wait()
    request_shutdown("app window closed (browser process exited)", fast=True)


def build_app() -> web.Application:
    app = web.Application(middlewares=[log_and_nocache])
    app.router.add_get("/", index)
    app.router.add_get("/livekit-client.js", lib)
    app.router.add_static("/static/", path=str(WEB_DIR))
    app.router.add_get("/health", health)
    app.router.add_get("/token", token)
    app.router.add_get("/api/config", api_config_get)
    app.router.add_post("/api/config", api_config_post)
    app.router.add_post("/api/reveal", api_reveal)
    app.router.add_get("/api/models/stt", api_models_stt)
    app.router.add_get("/api/models/tts", api_models_tts)
    app.router.add_get("/api/browsers", api_browsers)
    app.router.add_post("/api/llm/models", api_llm_models)
    app.router.add_get("/api/chat/sessions", api_chat_sessions)
    app.router.add_get("/api/chat/session", api_chat_session)
    app.router.add_post("/api/chat/clear", api_chat_clear)
    app.router.add_get("/api/check", api_check)
    app.router.add_post("/api/restart", api_restart)
    app.router.add_post("/api/heartbeat", api_heartbeat)
    app.router.add_post("/api/window/closing", api_window_closing)
    app.router.add_get("/api/publish", api_publish)
    app.router.add_get("/api/mcp/status", api_mcp_status)
    app.router.add_route("*", "/lk/{suffix:.*}", lk_proxy)
    app.router.add_get("/favicon.ico", favicon)
    return app


def prepare_livekit_yaml() -> None:
    mode = current_mode()
    try:
        LIVEKIT_YAML.parent.mkdir(parents=True, exist_ok=True)
        expected_bind = "127.0.0.1" if mode == "local" else "0.0.0.0"
        existing = LIVEKIT_YAML.read_text(encoding="utf-8") if LIVEKIT_YAML.exists() else ""
        key = setting("LIVEKIT_API_KEY", "devkey") or "devkey"
        secret = (setting("LIVEKIT_API_SECRET", "")
                  or "porchvoice-secret-1234567890abcdef")
        keys_ok = f"{key}: {secret}" in existing
        bind_ok = f"- {expected_bind}" in existing
        if not existing or not bind_ok:
            LIVEKIT_YAML.write_text(minimal_yaml(mode), encoding="utf-8")
            log(f"wrote livekit.yaml for {mode} mode (bind {expected_bind}, "
                f"key {masked(key)})")
        elif not keys_ok:
            backup = LIVEKIT_YAML.with_name("livekit.yaml.old-keys")
            try:
                shutil.copyfile(LIVEKIT_YAML, backup)
                log("livekit.yaml keys did not match LIVEKIT_API_KEY/SECRET "
                    f"- rewrote it (old file saved as {backup.name})")
            except OSError:
                log("livekit.yaml keys did not match LIVEKIT_API_KEY/SECRET "
                    "- rewrote it")
            LIVEKIT_YAML.write_text(minimal_yaml(mode), encoding="utf-8")
            log(f"livekit.yaml now uses key {masked(key)} from user_data/.env.local")
    except OSError as exc:
        log(f"could not prepare livekit.yaml ({exc})")


def start_gguf_stt() -> None:
    argv, why = stt_gguf_cmd()
    if argv is None:
        cfg = load_config()
        if (cfg.get("STT_ENGINE", "") or "").strip().lower() == "gguf":
            log(f"[stt-gguf] not started: {why}")
            log("[stt-gguf] FIX -> run setup step 7 (GGUF ASR), or point "
                "STT_GGUF_SERVER at a running llama-server, or pick another "
                "STT engine in Settings")
        return
    _RESTART["stt-gguf"] = 3
    spawn("stt-gguf", argv)
    url = gguf_stt_url(load_config())
    log(f"[stt-gguf] Qwen3-ASR GGUF server will answer at {url} "
        "(/v1/audio/transcriptions)")


def main() -> None:
    global JOB, _K32
    _harden_streams()
    if not venv_python().exists():
        raise SystemExit("[  sup] .venv missing - run setup.sh (or setup.bat on "
                         "Windows) first")
    if not AGENT_SCRIPT.is_file():
        raise SystemExit(f"[  sup] {AGENT_SCRIPT.name} missing - re-clone the repo")
    if not (WEB_DIR / "index.html").is_file():
        log("WARNING: python/web/index.html missing - UI will 404")

    repair_env_file()
    prepare_livekit_yaml()

    JOB = setup_job_object()
    _K32 = ctypes.WinDLL("kernel32", use_last_error=True) if os.name == "nt" else None

    agent_python = venv_python() if venv_python().exists() else Path(sys.executable)
    _RESTART["lk"] = 3
    _RESTART["agent"] = 3
    if LIVEKIT_EXE.exists():
        spawn("lk", [LIVEKIT_EXE, "--config", LIVEKIT_YAML])
    else:
        log("bin/livekit-server missing - run setup.sh (or setup.bat)")
    spawn("agent", [agent_python, "-u", AGENT_SCRIPT, "start"])
    threading.Thread(target=start_gguf_stt, daemon=True,
                     name="gguf-starter").start()

    if current_mode() == "public":
        threading.Thread(target=start_quick_tunnel, daemon=True,
                         name="tunnel-starter").start()

    try:
        banner()
    except Exception as exc:
        log(f"startup banner check failed ({exc}) - continuing")

    log("dispatch: automatic - the agent worker joins every room (no yaml or "
        "token setup)")
    if setting("CLOSE_WITH_WINDOW", "1") == "1":
        log("window close    : closing the app window stops everything instantly")
        log("                  closing this terminal closes the app window too")
        log("                  (set CLOSE_WITH_WINDOW=0 in user_data/.env.local "
            "to disable)")
    if setting("AUTO_OPEN", "1") == "1":
        threading.Thread(target=open_ui_when_ready, daemon=True,
                         name="ui-open").start()
    threading.Thread(target=heartbeat_watcher, daemon=True,
                     name="hb-watch").start()

    app = build_app()
    try:
        web.run_app(app, host=bind_host(), port=PORT, print=None)
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        log(f"CANNOT BIND {bind_host()}:{PORT} ({exc})")
        log("FIX -> is another start.sh window open? Close it, or free the port.")
        sys.exit(1)
    finally:
        stop_children()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        stop_children()
        sys.exit(0)

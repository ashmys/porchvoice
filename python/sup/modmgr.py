"""Optional module manager: list what is installed, remove what is unused."""
from __future__ import annotations

import shutil
import subprocess

from . import paths, state
from config import llm_gguf_dir, load_config
from .downloads import dl_job_snapshot, jobs_running
from .envstore import venv_python, write_env_keys
from .ggufsrv import current_mode, find_llama_server
from .logx import log
from .modelist import whisper_dir, piper_dir

MODULES_CATALOG = (
    # id, kind, display name, what uses it, how to reinstall
    {
        "id": "whisper-stt", "kind": "package", "pkgs": ["faster-whisper"],
        "import": "faster_whisper", "name": "Embedded Whisper (STT)",
        "uses": "STT engine 'whisper' / 'auto' without a server",
        "reinstall": "setup menu 9 (optional python modules)",
    },
    {
        "id": "kokoro-tts", "kind": "package", "pkgs": ["kokoro-onnx"],
        "import": "kokoro_onnx", "name": "Kokoro TTS engine",
        "uses": "TTS engine 'kokoro' (the default voice)",
        "reinstall": "setup menu 9 (optional python modules)",
    },
    {
        "id": "piper-tts", "kind": "package", "pkgs": ["piper-tts"],
        "import": "piper", "name": "Piper TTS engine",
        "uses": "TTS engine 'piper'",
        "reinstall": "setup menu 9 (optional python modules)",
    },
    {
        "id": "mcp", "kind": "package", "pkgs": ["mcp"],
        "import": "mcp", "name": "MCP tool client",
        "uses": "MCP_URL tool servers",
        "reinstall": "setup menu 9 (optional python modules)",
    },
    {
        "id": "gpu-libs", "kind": "package",
        "pkgs": ["nvidia-cublas-cu12", "nvidia-cudnn-cu12"],
        "import": "", "name": "NVIDIA CUDA/cuDNN runtime",
        "uses": "GPU acceleration for Whisper (ctranslate2)",
        "reinstall": "setup menu 9 (optional python modules)",
    },
    {
        "id": "whisper-models", "kind": "files", "name": "Whisper model files",
        "uses": "Embedded Whisper STT", "reinstall": "setup menu 4",
    },
    {
        "id": "kokoro-model", "kind": "files",
        "name": "Kokoro voice model (~340 MB)",
        "uses": "Kokoro TTS", "reinstall": "setup menu 5",
    },
    {
        "id": "piper-voices", "kind": "files", "name": "Piper voice files",
        "uses": "Piper TTS", "reinstall": "setup menu 5",
    },
    {
        "id": "gguf-asr", "kind": "files",
        "name": "Qwen3-ASR 0.6B GGUF (~1 GB)",
        "uses": "STT engine 'gguf'", "reinstall": "setup menu 7",
    },
    {
        "id": "llm-gguf", "kind": "files", "name": "GGUF chat models",
        "uses": "LLM engine 'gguf'", "reinstall": "setup menu 8 or the web UI",
    },
    {
        "id": "llama-server", "kind": "files",
        "name": "llama.cpp llama-server binary",
        "uses": "STT engine 'gguf' and LLM engine 'gguf'",
        "reinstall": "setup menu 7",
    },
    {
        "id": "cloudflared", "kind": "files", "name": "cloudflared (public tunnel)",
        "uses": "publish mode 'public'", "reinstall": "setup menu 6",
    },
    {
        "id": "livekit-server", "kind": "files", "name": "LiveKit server binary",
        "uses": "everything - do NOT remove unless you know why",
        "reinstall": "setup menu 2",
    },
)


def _pkg_installed(module_name: str) -> bool:
    if not module_name:
        return False
    try:
        import importlib.util

        return importlib.util.find_spec(module_name) is not None
    except Exception:
        return False


def _dir_size_mb(path) -> int:
    total = 0
    try:
        for f in path.rglob("*"):
            if f.is_file():
                total += f.stat().st_size
    except OSError:
        pass
    return round(total / 1e6)


def _module_files(mod_id: str) -> list:
    out = []
    if mod_id == "whisper-models":
        base = whisper_dir()
        if base.is_dir():
            out = [d for d in sorted(base.iterdir()) if d.is_dir()]
    elif mod_id == "kokoro-model":
        base = paths.KOKORO_DIR
        out = [f for f in (base / "kokoro-v1.0.onnx", base / "voices-v1.0.bin")
               if f.is_file()]
    elif mod_id == "piper-voices":
        base = piper_dir()
        out = sorted(base.glob("*.onnx*")) if base.is_dir() else []
    elif mod_id == "gguf-asr":
        out = sorted(paths.GGUF_ASR_DIR.glob("*.gguf")) \
            if paths.GGUF_ASR_DIR.is_dir() else []
    elif mod_id == "llm-gguf":
        base = llm_gguf_dir(load_config())
        out = sorted(base.glob("*.gguf")) if base.is_dir() else []
    elif mod_id == "llama-server":
        p = paths.LLAMA_SERVER_EXE
        out = [p] if p.is_file() else []
        for extra in paths.BIN_DIR.glob("llama-*"):
            if extra.is_file() and extra.name != paths.LLAMA_SERVER_EXE.name:
                out.append(extra)
        for extra in paths.BIN_DIR.glob("cudart-*"):
            out.append(extra)
    elif mod_id == "cloudflared":
        p = paths.CLOUDFLARED_EXE
        out = [p] if p.is_file() else []
    elif mod_id == "livekit-server":
        p = paths.LIVEKIT_EXE
        out = [p] if p.is_file() else []
    return [f for f in out if f.is_file() or f.is_dir()]


def _module_in_use(mod_id: str) -> str:
    """Return a reason string when removal would break the active config."""
    cfg = load_config()
    stt_engine = (cfg.get("STT_ENGINE", "") or "").strip().lower()
    tts_engine = (cfg.get("TTS_ENGINE", "") or "").strip().lower()
    llm_engine = (cfg.get("LLM_ENGINE", "") or "").strip().lower()
    checks = {
        "whisper-models": (stt_engine in ("", "whisper")
                           and not (cfg.get("STT_BASE_URL", "") or "").strip(),
                           "STT engine is embedded Whisper"),
        "kokoro-model": (tts_engine in ("", "kokoro")
                         and not (cfg.get("TTS_BASE_URL", "") or "").strip(),
                         "TTS engine is Kokoro"),
        "piper-voices": (tts_engine == "piper", "TTS engine is Piper"),
        "gguf-asr": (stt_engine == "gguf", "STT engine is GGUF ASR"),
        "llm-gguf": (llm_engine == "gguf", "LLM engine is GGUF"),
        "llama-server": (stt_engine == "gguf" or llm_engine == "gguf",
                         "a GGUF engine is selected"),
        "cloudflared": (current_mode() == "public", "publish mode is public"),
        "livekit-server": (True, "LiveKit is the core server"),
    }
    check = checks.get(mod_id)
    if check and check[0]:
        return check[1]
    return ""


def list_modules() -> list:
    out = []
    for mod in MODULES_CATALOG:
        mid = mod["id"]
        entry = {"id": mid, "name": mod["name"], "uses": mod.get("uses", ""),
                 "reinstall": mod.get("reinstall", "")}
        if mod["kind"] == "package":
            entry["type"] = "python package"
            installed = _pkg_installed(mod.get("import", ""))
            entry["installed"] = installed
            entry["pkgs"] = mod["pkgs"]
            entry["size_mb"] = 0
        else:
            entry["type"] = "files"
            files = _module_files(mid)
            entry["installed"] = bool(files)
            entry["files"] = [f.name for f in files][:40]
            if mid == "whisper-models":
                entry["size_mb"] = sum(_dir_size_mb(f) for f in files)
            else:
                entry["size_mb"] = round(sum(
                    (f.stat().st_size if f.is_file() else _dir_size_mb(f))
                    for f in files) / 1e6)
        entry["in_use"] = _module_in_use(mid)
        out.append(entry)
    return out


def remove_module(mod_id: str, target: str = "") -> tuple:
    mod = next((m for m in MODULES_CATALOG if m["id"] == mod_id), None)
    if mod is None:
        return False, f"unknown module '{mod_id}'"
    why = _module_in_use(mod_id)
    if why:
        msg = (f"'{mod['name']}' is in use: {why}. Switch the engine in "
               "Settings first, then remove it.")
        log(f"module removal refused: {msg}")
        return False, msg
    if mod["kind"] == "package":
        py = venv_python()
        if not py.exists():
            return False, ".venv missing - nothing to uninstall from"
        code = subprocess.run(
            [str(py), "-m", "pip", "uninstall", "-y", *mod["pkgs"]],
            capture_output=True, text=True, timeout=300)
        if code.returncode != 0:
            return False, ("pip uninstall failed: "
                           + (code.stderr or code.stdout or "")[-200:])
        log(f"module removed: {mod['name']} (pip: {', '.join(mod['pkgs'])})")
        return True, f"uninstalled {', '.join(mod['pkgs'])}"
    files = _module_files(mod_id)
    if target:
        files = [f for f in files if f.name == target or str(f) == target]
        if not files:
            return False, f"no matching file '{target}' in {mod_id}"
    removed = []
    for f in files:
        if mod_id == "llm-gguf" and not target:
            cfg = load_config()
            if f.name == (cfg.get("LLM_GGUF_MODEL", "") or "").strip():
                write_env_keys({"LLM_GGUF_MODEL": ""})
        try:
            if f.is_dir():
                shutil.rmtree(f)
            else:
                f.unlink()
            removed.append(f.name)
        except OSError as exc:
            log(f"could not remove {f}: {exc}")
    if mod_id == "llama-server" and not target:
        for left in list(paths.BIN_DIR.glob("llama-*")) + \
                list(paths.BIN_DIR.glob("cudart-*")):
            try:
                shutil.rmtree(left) if left.is_dir() else left.unlink()
            except OSError:
                pass
    if not removed:
        return False, "nothing to remove (already gone)"
    log(f"module removed: {mod['name']} -> {', '.join(removed[:5])}"
        + (" ..." if len(removed) > 5 else ""))
    return True, f"removed {len(removed)} item(s)"


def llm_preset_status() -> list:
    cfg = load_config()
    base = llm_gguf_dir(cfg)
    presets = []
    for pid, repo, pattern, mb, note in paths_and_catalog():
        local = ""
        if base.is_dir():
            for f in sorted(base.glob("*.gguf")):
                if pattern.lower() in f.name.lower() and \
                        not f.name.lower().startswith("mmproj"):
                    local = f.name
                    break
        presets.append({"id": pid, "repo": repo, "size_mb": mb, "note": note,
                        "local_file": local, "local": bool(local)})
    return presets


def paths_and_catalog():
    from .ggufsrv import LLM_GGUF_CATALOG

    return LLM_GGUF_CATALOG


def llm_preset_local(preset) -> str:
    """Name of an already-downloaded gguf matching the preset, if any."""
    _pid, _repo, pattern, _mb, _note = preset
    base = llm_gguf_dir(load_config())
    if not base.is_dir():
        return ""
    for f in sorted(base.glob("*.gguf")):
        if pattern.lower() in f.name.lower() \
                and not f.name.lower().startswith("mmproj"):
            return f.name
    return ""

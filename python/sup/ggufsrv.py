"""Managed GGUF llama-server children (ASR + chat LLM)."""
from __future__ import annotations

import shutil

from . import paths, state
from config import gguf_stt_url, llm_gguf_dir, llm_gguf_model_path, llm_gguf_url, \
    load_config, publish_mode
from .envstore import setting
from .logx import log

LLM_GGUF_CATALOG = (
    # id, repo on HuggingFace, filename pattern, approx size MB, note
    ("Qwen3-0.6B-Q8_0.gguf", "Qwen/Qwen3-0.6B-GGUF", "Q8_0",
     640, "best small default - great quality for a voice assistant"),
    ("Qwen3-0.6B-Q4_K_M.gguf", "Qwen/Qwen3-0.6B-GGUF", "q4_k_m",
     400, "smallest, fastest, a bit rougher"),
    ("Qwen3-1.7B-Q4_K_M.gguf", "Qwen/Qwen3-1.7B-GGUF", "q4_k_m",
     1100, "smarter, needs ~1.5 GB VRAM"),
    ("Qwen3-4B-Q4_K_M.gguf", "Qwen/Qwen3-4B-GGUF", "q4_k_m",
     2500, "much smarter, needs ~3 GB VRAM (6 GB GPUs are fine)"),
    ("Qwen2.5-0.5B-Instruct-Q4_K_M.gguf", "Qwen/Qwen2.5-0.5B-Instruct-GGUF",
     "q4_k_m", 420, "older compact fallback"),
)


def find_llama_server() -> str:
    if paths.LLAMA_SERVER_EXE.is_file():
        return str(paths.LLAMA_SERVER_EXE)
    return shutil.which("llama-server") or ""


def gguf_asr_files() -> tuple:
    model = mmproj = None
    if paths.GGUF_ASR_DIR.is_dir():
        for f in sorted(paths.GGUF_ASR_DIR.glob("*.gguf")):
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
            "--ctx-size", "4096", "-ngl", "99", "--temp", "0", "--no-webui"]
    return argv, ""


def current_mode() -> str:
    return publish_mode(load_config())


def bind_host() -> str:
    return "127.0.0.1" if current_mode() == "local" else "0.0.0.0"


def llm_gguf_cmd() -> tuple:
    cfg = load_config()
    engine = (cfg.get("LLM_ENGINE", "") or "").strip().lower()
    if engine != "gguf":
        return None, "LLM_ENGINE is not gguf"
    base = (cfg.get("LLM_GGUF_SERVER", "") or "").strip()
    if base:
        return None, "LLM_GGUF_SERVER is set - using the external server"
    exe = find_llama_server()
    if not exe:
        return None, ("llama-server binary not found (bin/llama-server or PATH) - "
                      "run setup menu 7/8, or set LLM_GGUF_SERVER to an existing "
                      "llama-server URL")
    model = llm_gguf_model_path(cfg)
    if model is None:
        return None, (f"no chat GGUF model found in {llm_gguf_dir(cfg)} "
                      "- download one via setup menu 8 or the web UI settings")
    port = int(str(cfg.get("LLM_GGUF_PORT", "8899") or 8899))
    ngl = str(cfg.get("LLM_GGUF_NGL", "99") or 99)
    ctx = str(cfg.get("LLM_GGUF_CTX", "4096") or 4096)
    argv = [exe, "-m", str(model), "--host", "127.0.0.1", "--port", str(port),
            "--ctx-size", ctx, "-ngl", ngl, "--no-webui"]
    return argv, ""


def llm_gguf_autostart(cfg: dict) -> bool:
    """No explicit engine and no configured URL: auto-start the managed GGUF
    LLM only when a model file exists and no other local LLM server answered."""
    if (cfg.get("LLM_BASE_URL", "") or "").strip():
        return False
    if (cfg.get("LLM_API_KEY", "") or "").strip():
        return False
    if llm_gguf_model_path(cfg) is None:
        return False
    from config import LOCAL_LLM_CANDIDATES, http_json_ok

    return not any(http_json_ok(c, timeout=0.6) for c in LOCAL_LLM_CANDIDATES)


def start_gguf_llm() -> None:
    cfg = load_config()
    engine = (cfg.get("LLM_ENGINE", "") or "").strip().lower()
    if engine != "gguf" and not llm_gguf_autostart(cfg):
        return
    argv, why = llm_gguf_cmd()
    if argv is None:
        if engine == "gguf":
            log(f"[llm-gguf] not started: {why}")
            log("[llm-gguf] FIX -> run setup menu 8 (GGUF LLM), or point "
                "LLM_GGUF_SERVER at a running llama-server, or set LLM_BASE_URL")
        return
    state._RESTART["llm-gguf"] = 3
    from .procs import spawn

    spawn("llm-gguf", argv)
    url = llm_gguf_url(cfg)
    model = llm_gguf_model_path(cfg)
    log(f"[llm-gguf] chat model {model.name if model else '?'} will answer at "
        f"{url} (/v1/chat/completions)")


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
    state._RESTART["stt-gguf"] = 3
    from .procs import spawn

    spawn("stt-gguf", argv)
    url = gguf_stt_url(load_config())
    log(f"[stt-gguf] Qwen3-ASR GGUF server will answer at {url} "
        "(/v1/audio/transcriptions)")

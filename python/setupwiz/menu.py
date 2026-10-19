"""Setup menu: run everything or pick individual steps."""
from __future__ import annotations

import os
import sys

from . import core
from .core import DRY_RUN, ask, hr
from .steps_base import step_cloudflared, step_deps, step_livekit
from .steps_gguf import step_gguf_asr, step_gguf_llm, step_modules, step_remove
from .steps_models import step_kokoro, step_piper, step_voice, \
    step_whisper_prefetch


def done() -> None:
    hr("ALL DONE")
    if os.name == "nt":
        print("  Start everything with  start.bat")
    else:
        print("  Start everything with  ./start.sh")
    print("  Then open  http://127.0.0.1:8090  and press play.")


def run_all() -> None:
    step_deps()
    step_livekit()
    mode = step_config()
    whisper_installed = core.pip_installed(["faster-whisper"])
    if whisper_installed:
        step_whisper_prefetch()
    else:
        print("\n  Embedded Whisper was not installed (step 1) - skipping the")
        print("  Whisper model download. GGUF ASR below is the local STT option.")
    step_voice()
    step_gguf_asr(default_yes="n" if whisper_installed else "y")
    step_gguf_llm(default_yes="n")
    if mode == "public":
        step_cloudflared()
    else:
        print("\n  Publish mode is not public - skipping cloudflared setup "
              "(step 6).")
    done()


def step_config():
    """LLM / MCP / publish mode questions; existing values stay the defaults."""
    from .core import read_env, write_env

    hr("STEP 3] Configuration")
    existing = read_env()
    if existing:
        print("  Existing settings found - they are shown in [brackets] and")
        print("  KEPT unless you type a new value. Nothing gets overwritten.")
    write_livekit_yaml()
    print("  The LLM can be local (Ollama, LM Studio, llama.cpp, textgen webui),")
    print("  a downloaded GGUF chat model (menu 8), or cloud (OpenAI, Groq,")
    print("  OpenRouter, ...). Any OpenAI-compatible URL works.")
    print("  http:// = local, https:// = cloud. Leave empty to auto-detect.")
    base = ask("LLM base URL (Enter = keep / auto-detect)",
               existing.get("LLM_BASE_URL", ""))
    key = core.ask_secret("LLM API key (optional for local servers)",
                          existing.get("LLM_API_KEY", ""))
    model = ask("LLM model id (Enter = keep / server default)",
                existing.get("LLM_MODEL", ""))
    if key.strip().lower() in ("-", "none", "no"):
        key = ""
    updates = {"LLM_BASE_URL": base, "LLM_API_KEY": key, "LLM_MODEL": model}
    print("  MCP tools: point PorchVoice at one MCP server URL (streamable "
          "HTTP or")
    print("  SSE). The bearer token is optional.")
    mcp_url = ask("MCP URL (Enter = keep / none)", existing.get("MCP_URL", ""))
    mcp_token = ""
    if mcp_url.strip():
        mcp_token = core.ask_secret("MCP bearer token (optional)",
                                    existing.get("MCP_AUTH_TOKEN", ""))
        if mcp_token.strip().lower() in ("-", "none", "no"):
            mcp_token = ""
    updates["MCP_URL"] = mcp_url
    updates["MCP_AUTH_TOKEN"] = mcp_token
    print("  Publish mode: local = this machine only (default, no extra setup);")
    print("  wifi = share on your LAN; public = quick tunnel via cloudflared "
          "(step 6).")
    mode = ask("Publish mode (local/wifi/public)",
               existing.get("PUBLISH_MODE", "") or "local").strip().lower()
    if mode not in ("local", "wifi", "public"):
        mode = "local"
    if mode == "local":
        print("  local mode: nothing to publish - no tunnel, no LAN exposure, "
              "no step 6.")
    updates["PUBLISH_MODE"] = mode
    write_env(updates)
    print("  You can change every setting later in the web UI (gear icon).")
    return mode


def write_livekit_yaml() -> None:
    from .core import YAML_FILE, read_env

    if DRY_RUN:
        print(f"(dry-run) would write {YAML_FILE.name}")
        return
    cfg = read_env()
    key = cfg.get("LIVEKIT_API_KEY", "devkey") or "devkey"
    secret = cfg.get("LIVEKIT_API_SECRET", "") \
        or "porchvoice-secret-1234567890abcdef"
    if secret.startswith("enc:v1:"):
        from keyvault import decrypt_value

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


def main() -> None:
    flags = set(sys.argv[1:])
    core.DRY_RUN = "--dry-run" in flags
    core.AUTO = "--auto" in flags
    models_only = "--models-only" in flags

    print("=" * 60)
    print(" PORCHVOICE - setup")
    print("=" * 60)
    if core.ENV_FILE.exists():
        print(" Existing settings found - they are kept and shown as defaults.")

    if models_only:
        step_whisper_prefetch()
        step_voice()
        done()
        return

    while True:
        hr("MENU")
        print("  1) Python venv + packages (core + optional engines)")
        print("  2) LiveKit server")
        print("  3) Configuration (LLM, MCP, publish mode - current values kept)")
        print("  4) Whisper STT model")
        print("  5) Voice engine (Kokoro / Piper TTS)")
        print("  6) cloudflared (only for public publish mode)")
        print("  7) GGUF ASR (Qwen3-ASR 0.6B speech-to-text via llama.cpp)")
        print("  8) GGUF LLM (local chat model via llama.cpp, auto-detect + download)")
        print("  9) Optional python modules (install engines you want)")
        print(" 10) Remove unused modules / models (free disk space)")
        print("  0) Run ALL steps, then exit")
        choice = ask("Choose", "0")
        if DRY_RUN and core.AUTO:
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
        elif choice == "8":
            step_gguf_llm()
        elif choice == "9":
            step_modules()
        elif choice == "10":
            step_remove()
        elif choice == "0":
            run_all()
            return

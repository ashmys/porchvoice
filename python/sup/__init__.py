"""PorchVoice supervisor: LiveKit + agent + GGUF servers + web UI in one process."""
from __future__ import annotations

import ctypes
import os
import platform
import sys
import threading
import time

from . import paths, state

# the engine modules (config, keyvault, ...) live next to this package
if str(paths.PYTHON_DIR) not in sys.path:
    sys.path.insert(0, str(paths.PYTHON_DIR))

from config import load_config, resolve_mcp, resolve_stt, resolve_tts  # noqa: E402
from .envstore import setting, venv_python  # noqa: E402
from .ggufsrv import bind_host, current_mode, start_gguf_llm, start_gguf_stt  # noqa: E402
from .logx import harden_streams, log  # noqa: E402
from .modelist import check_gpu, check_llm  # noqa: E402
from .procs import heartbeat_watcher, livekit_target, prepare_livekit_yaml, \
    spawn, stop_children, tcp_alive  # noqa: E402
from .tunnel import start_quick_tunnel, tunnel_info  # noqa: E402
from .webapi import build_app  # noqa: E402


def banner() -> None:
    lk_host, lk_port = livekit_target()
    up = False
    for _ in range(10):
        if tcp_alive(lk_host, lk_port):
            up = True
            break
        time.sleep(0.5)
    lib = paths.WEB_DIR / "livekit-client.js"
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
        log(f"public URL     : {state.TUNNEL.get('url') or 'starting...'}")
    log(f"python         : {platform.python_version()}  pid: {os.getpid()}")
    log(f"LiveKit        : ws://{lk_host}:{lk_port} -> {'UP' if up else 'DOWN'}")
    log(f"LLM API        : {check_llm()}")
    log(f"STT            : {stt_line}")
    log(f"TTS            : {tts_sel['engine']}")
    log(f"MCP            : {mcp_sel['url'] or 'not configured'}"
        + (" (bearer set)" if mcp_sel.get("token") else ""))
    log(f"GPU (nvidia)   : {check_gpu()}")
    log(f"livekit-client : {'OK' if lib.is_file() else 'MISSING - re-clone the repo'}")
    log("Children       : livekit-server (auto-restart) + agent worker "
        "(auto-restart)")
    log("Stop           : Ctrl+C here, or just close this window - nothing "
        "lingers")
    log("=" * 68)


def main() -> None:
    harden_streams()
    if not venv_python().exists():
        raise SystemExit("[  sup] .venv missing - run setup.sh (or setup.bat on "
                         "Windows) first")
    if not paths.AGENT_SCRIPT.is_file():
        raise SystemExit(f"[  sup] {paths.AGENT_SCRIPT.name} missing - re-clone "
                         "the repo")
    if not (paths.WEB_DIR / "index.html").is_file():
        log("WARNING: python/web/index.html missing - UI will 404")

    from .envstore import repair_env_file

    repair_env_file()
    prepare_livekit_yaml()

    from .winjob import setup_job_object

    if os.name == "nt":
        state._K32 = ctypes.WinDLL("kernel32", use_last_error=True)
    state.JOB = setup_job_object()

    from .procs import agent_python

    state._RESTART["lk"] = 3
    state._RESTART["agent"] = 3
    if paths.LIVEKIT_EXE.exists():
        spawn("lk", [paths.LIVEKIT_EXE, "--config", paths.LIVEKIT_YAML])
    else:
        log("bin/livekit-server missing - run setup.sh (or setup.bat)")
    spawn("agent", [agent_python(), "-u", paths.AGENT_SCRIPT, "start"])
    threading.Thread(target=start_gguf_stt, daemon=True,
                     name="gguf-starter").start()
    threading.Thread(target=start_gguf_llm, daemon=True,
                     name="llm-gguf-starter").start()

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
        from .uiopen import open_ui_when_ready

        threading.Thread(target=open_ui_when_ready, daemon=True,
                         name="ui-open").start()
    threading.Thread(target=heartbeat_watcher, daemon=True,
                     name="hb-watch").start()

    app = build_app()
    try:
        from aiohttp import web as _web

        _web.run_app(app, host=bind_host(), port=paths.PORT, print=None)
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        log(f"CANNOT BIND {bind_host()}:{paths.PORT} ({exc})")
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

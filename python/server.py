"""Compatibility facade for the supervisor.

The implementation moved to the `sup` package (one module per concern, none
of them over a thousand lines). Everything that imports `server` - start.bat,
start.sh and tests/e2e_smoke.py - keeps working unchanged: attribute reads
resolve into the package, and assignments to mutable settings (ENV_FILE and
JOB, as the smoke suite does) are forwarded to `sup.state`.
"""
from __future__ import annotations

import sys
import types

from sup import state as _state
from sup.chatstore import parse_room_from_history
from sup.chatstore import read_messages as _chat_messages
from sup.envstore import env_dict, mask_secrets, repair_env_file, setting, \
    venv_python, write_env_keys
from sup.ggufsrv import bind_host, current_mode, find_llama_server, \
    gguf_asr_files, llm_gguf_autostart, llm_gguf_cmd, start_gguf_llm, \
    start_gguf_stt, LLM_GGUF_CATALOG
from sup.logx import harden_streams, log
from sup.modelist import check_gpu, check_llm, check_stt_provider, \
    check_tts_provider, count_local_models, list_stt_models, list_tts_models, \
    local_model_dir, probe_provider, whisper_dir, piper_dir
from sup.paths import (AGENT_SCRIPT, BIN_DIR, CLOUDFLARED_EXE, CHAT_DIR,
                       FATAL_MARKERS, GGUF_ASR_DIR, HOST, KOKORO_DIR,
                       LLAMA_SERVER_EXE, LIVEKIT_EXE, LIVEKIT_YAML, PORT,
                       ROOT, SECRET_SENTINEL, WEB_DIR, WINDOW_STATE_FILE)
from sup.procs import child_saw, heal_yaml, heartbeat_fresh, heartbeat_watcher, \
    livekit_target, minimal_yaml, prepare_livekit_yaml, request_shutdown, \
    spawn, stop_children
from sup.tunnel import find_cloudflared, start_quick_tunnel, stop_tunnel, \
    tunnel_info
from sup import banner, main
from sup.webapi import (api_browsers, api_check, api_chat_clear,
                        api_chat_session, api_chat_sessions, api_config_get,
                        api_config_post, api_llm_models, api_models_llm,
                        api_models_llm_post, api_models_stt, api_models_tts,
                        api_modules, api_modules_remove, api_mcp_status,
                        api_publish, api_restart, api_reveal, api_heartbeat,
                        api_window_closing, build_app, client_livekit_url,
                        favicon, health, index, lib, lk_proxy, lk_ws_proxy,
                        lk_http_proxy, log_and_nocache, token)
from sup.winjob import assign_job, setup_job_object


# Attribute writes to ENV_FILE / JOB must affect the running supervisor, so
# they are forwarded to sup.state; everything else lands on this module.
_FORWARDED_WRITES = {"ENV_FILE", "JOB", "_BROWSER", "_K32"}


class _Facade(types.ModuleType):
    def __setattr__(self, name, value):
        if name in _FORWARDED_WRITES:
            setattr(_state, name, value)
        else:
            super().__setattr__(name, value)


sys.modules[__name__].__class__ = _Facade


def __getattr__(name):  # PEP 562 - Children, TUNNEL, DL_JOBS, _OUT, ...
    try:
        return getattr(_state, name)
    except AttributeError:
        raise AttributeError(f"module 'server' has no attribute {name!r}") \
            from None


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        stop_children()
        sys.exit(0)

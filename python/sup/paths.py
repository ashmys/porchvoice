"""Filesystem layout and fixed constants for the supervisor."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
PYTHON_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = PYTHON_DIR / "web"
CHAT_DIR = ROOT / "user_data" / "chat"
LIVEKIT_YAML = ROOT / "user_data" / "livekit.yaml"
LIVEKIT_EXE = ROOT / "bin" / ("livekit-server.exe" if os.name == "nt"
                              else "livekit-server")
CLOUDFLARED_EXE = ROOT / "bin" / ("cloudflared.exe" if os.name == "nt"
                                  else "cloudflared")
AGENT_SCRIPT = PYTHON_DIR / "agent.py"
WINDOW_STATE_FILE = ROOT / "user_data" / "window_state.json"
GGUF_ASR_DIR = ROOT / "user_data" / "models" / "gguf-asr"
KOKORO_DIR = ROOT / "user_data" / "models" / "kokoro"
BIN_DIR = ROOT / "bin"
LLAMA_SERVER_EXE = BIN_DIR / ("llama-server.exe" if os.name == "nt"
                              else "llama-server")

HOST = "127.0.0.1"
PORT = 8090

SECRET_SENTINEL = "__saved__"

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

WINDOW_TITLE = "PorchVoice"

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
}

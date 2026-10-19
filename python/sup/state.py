"""Mutable process-wide state. Every reader goes through this module
(`state.X`), so overrides from the tests and the facade stay effective."""
from __future__ import annotations

import threading
import time
from collections import deque

from dotenv import load_dotenv

from . import paths

ENV_FILE = paths.ROOT / "user_data" / ".env.local"
load_dotenv(ENV_FILE)

CHILDREN: dict = {}
_RESTART: dict = {}
_CRASHES: dict = {}
_SPAWNED_AT: dict = {}
_OUT: dict = {}

TUNNEL = {"proc": None, "url": "", "started_at": 0.0, "log": deque(maxlen=80)}
TUNNEL_URL_RE = None  # compiled lazily by the tunnel module

DL_JOBS: dict = {}

JOB = None
_K32 = None
_BROWSER = None
_BROWSER_JOBBED = False

_WIN_MINIMIZED = False
_LAST_BEAT = 0.0
_CLOSE_PENDING_AT = 0.0
_CLOSE_CHECK = threading.Event()
_SHUT_LOCK = threading.Lock()
_SHUTTING = False


def monotonic() -> float:
    return time.monotonic()

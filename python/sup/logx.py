"""Console logging for the supervisor process."""
from __future__ import annotations

import sys


def log(msg: str) -> None:
    print(f"[  sup] {msg}", flush=True)


def harden_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except Exception:
            pass

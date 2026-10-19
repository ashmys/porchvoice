"""Chat history: the jsonl files in user_data/chat."""
from __future__ import annotations

import json
import re

from . import paths, state


def parse_room_from_history(path) -> str:
    m = re.match(r"\d{8}-\d{6}-(.+)\.jsonl$", path.name)
    return m.group(1) if m else "room"


def list_sessions() -> list:
    sessions = []
    if not paths.CHAT_DIR.is_dir():
        return sessions
    for f in sorted(paths.CHAT_DIR.glob("*.jsonl"), key=lambda p: p.name,
                    reverse=True):
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
    return sessions[:100]


def latest_session_for_room(room: str):
    cands = sorted(paths.CHAT_DIR.glob(f"*-{room}.jsonl"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0] if cands else None


def read_messages(f) -> list:
    msgs = []
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
    return msgs


def clear_history(room: str, wipe_all: bool = False) -> list:
    """Empty the current session file for a room. Older conversations are
    kept unless wipe_all is set - 'Clear' must never delete past chats."""
    cleared = []
    if not paths.CHAT_DIR.is_dir():
        return cleared
    name_re = re.compile(r"^\d{8}-\d{6}-[\w.-]+\.jsonl$")
    matches = sorted(paths.CHAT_DIR.glob(f"*-{room}.jsonl"),
                     key=lambda p: p.stat().st_mtime, reverse=True)
    if not wipe_all:
        matches = matches[:1]
    from .logx import log

    for f in matches:
        if not name_re.match(f.name):
            continue
        try:
            with open(f, "w", encoding="utf-8"):
                pass
            cleared.append(f.name)
        except OSError as exc:
            log(f"could not clear {f.name}: {exc}")
    if cleared:
        scope = "all sessions" if wipe_all else "current session"
        log(f"chat history cleared for room {room} ({scope}): "
            f"{', '.join(cleared)}")
    return cleared

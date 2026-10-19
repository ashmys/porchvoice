"""Child processes: spawn, restart policy, LiveKit yaml, shutdown paths."""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

from . import paths, state
from .envstore import setting
from .logx import log


def heartbeat_fresh(window: float = 3.0) -> bool:
    return state._LAST_BEAT > 0 and (state.monotonic() - state._LAST_BEAT) <= window


def request_shutdown(reason: str, fast: bool = False) -> None:
    if setting("CLOSE_WITH_WINDOW", "1") != "1":
        return
    if heartbeat_fresh():
        if not fast:
            log(f"window closed, but the UI is still sending heartbeats - "
                f"staying up ({reason})")
            return
        baseline = state._LAST_BEAT
        deadline = state.monotonic() + 1.2
        while state.monotonic() < deadline:
            if state._LAST_BEAT != baseline:
                log(f"window closed, but another UI page is still alive - "
                    f"staying up ({reason})")
                return
            time.sleep(0.1)
    with state._SHUT_LOCK:
        if state._SHUTTING:
            return
        state._SHUTTING = True
    log(f"UI window closed ({reason}) - shutting down LiveKit + agent, bye!")
    try:
        stop_children()
    except Exception:
        pass
    os._exit(0)


def heartbeat_watcher() -> None:
    while True:
        time.sleep(1.0)
        if state._LAST_BEAT <= 0:
            continue
        if state._WIN_MINIMIZED:
            continue
        gap = state.monotonic() - state._LAST_BEAT
        pending_recent = state._CLOSE_PENDING_AT > 0 and \
            (state.monotonic() - state._CLOSE_PENDING_AT) < 60
        threshold = paths.HB_GAP_PENDING if pending_recent else paths.HB_GAP_SHUTDOWN
        if gap >= threshold:
            request_shutdown(f"no UI heartbeat for {gap:.0f}s")


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
            cwd=str(paths.ROOT),
            env=child_env,
        )
    except FileNotFoundError:
        log(f"[{name}] FAILED: executable not found: {argv[0]}")
        log(f"[{name}] FIX -> re-run setup.bat (or setup.sh), then start again")
        return
    except OSError as exc:
        log(f"[{name}] FAILED to start ({exc})")
        return
    state.CHILDREN[name] = proc
    state._OUT[name] = deque(maxlen=80)
    state._SPAWNED_AT[name] = state.monotonic()
    from .winjob import assign_job

    assign_job(proc)

    def pump() -> None:
        for line in proc.stdout:
            text = line.rstrip()
            ring = state._OUT.get(name)
            if ring is not None:
                ring.append(text)
            print(f"[{name}] {text}", flush=True)

    threading.Thread(target=pump, daemon=True, name=f"{name}-pump").start()

    def watch() -> None:
        code = proc.wait()
        print(f"[  sup] [{name}] exited with code {code}", flush=True)
        state.CHILDREN.pop(name, None)
        runtime = state.monotonic() - state._SPAWNED_AT.get(name, state.monotonic())
        if name == "lk" and code != 0 and child_saw(name, "could not parse config"):
            heal_yaml()
            state._RESTART[name] = 1
        if not state._RESTART.get(name):
            return
        fatal = next((m for m in paths.FATAL_MARKERS if child_saw(name, m)), None)
        if fatal:
            state._RESTART.pop(name, None)
            log(f"[{name}] stopped retrying: the last run failed with a fatal "
                f"config error ({fatal!r}) - restarting cannot fix this")
            log(f"[{name}] FIX -> correct the setting in user_data/.env.local "
                "or the web UI, then press Settings -> Restart (or POST /api/restart)")
            return
        if runtime < 15.0:
            state._CRASHES[name] = state._CRASHES.get(name, 0) + 1
        else:
            state._CRASHES[name] = 0
        if state._CRASHES.get(name, 0) >= paths.RESTART_MAX_CRASHES:
            state._RESTART.pop(name, None)
            state._CRASHES[name] = 0
            log(f"[{name}] gave up after {paths.RESTART_MAX_CRASHES} fast crashes "
                "in a row - it keeps dying right after start")
            log(f"[{name}] FIX -> read the error above, correct the config, "
                "then press Settings -> Restart (or POST /api/restart) to try again")
            return
        delay = state._RESTART[name]
        state._RESTART[name] = min(delay * 2, 30)
        log(f"[{name}] restarting in {delay}s...")
        time.sleep(delay)
        spawn(name, argv)

    threading.Thread(target=watch, daemon=True, name=f"{name}-watch").start()


def child_saw(name: str, needle: str) -> bool:
    return any(needle in line for line in state._OUT.get(name, ()))


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
    backup = paths.LIVEKIT_YAML.with_name("livekit.yaml.broken")
    try:
        if paths.LIVEKIT_YAML.exists():
            shutil.copyfile(paths.LIVEKIT_YAML, backup)
            log(f"saved your old config as {backup.name}")
    except OSError as exc:
        log(f"could not back up livekit.yaml ({exc})")
    try:
        from .ggufsrv import current_mode

        paths.LIVEKIT_YAML.write_text(minimal_yaml(current_mode()),
                                      encoding="utf-8")
        log("livekit.yaml was rejected by livekit-server - wrote a clean minimal "
            "config it cannot misparse")
        log("the agent joins rooms automatically; no room_config entry is needed")
    except OSError as exc:
        log(f"FAILED to regenerate livekit.yaml ({exc})")
        log("FIX -> open user_data/livekit.yaml in a text editor and simplify "
            "it, or delete it")


def prepare_livekit_yaml() -> None:
    from .ggufsrv import current_mode

    mode = current_mode()
    try:
        paths.LIVEKIT_YAML.parent.mkdir(parents=True, exist_ok=True)
        expected_bind = "127.0.0.1" if mode == "local" else "0.0.0.0"
        existing = paths.LIVEKIT_YAML.read_text(encoding="utf-8") \
            if paths.LIVEKIT_YAML.exists() else ""
        key = setting("LIVEKIT_API_KEY", "devkey") or "devkey"
        secret = (setting("LIVEKIT_API_SECRET", "")
                  or "porchvoice-secret-1234567890abcdef")
        keys_ok = f"{key}: {secret}" in existing
        bind_ok = f"- {expected_bind}" in existing
        if not existing or not bind_ok:
            paths.LIVEKIT_YAML.write_text(minimal_yaml(mode), encoding="utf-8")
            log(f"wrote livekit.yaml for {mode} mode (bind {expected_bind})")
        elif not keys_ok:
            backup = paths.LIVEKIT_YAML.with_name("livekit.yaml.old-keys")
            try:
                shutil.copyfile(paths.LIVEKIT_YAML, backup)
                log("livekit.yaml keys did not match LIVEKIT_API_KEY/SECRET "
                    f"- rewrote it (old file saved as {backup.name})")
            except OSError:
                log("livekit.yaml keys did not match LIVEKIT_API_KEY/SECRET "
                    "- rewrote it")
            paths.LIVEKIT_YAML.write_text(minimal_yaml(mode), encoding="utf-8")
            from config import masked

            log(f"livekit.yaml now uses key {masked(key)} from user_data/.env.local")
    except OSError as exc:
        log(f"could not prepare livekit.yaml ({exc})")


def stop_children() -> None:
    from .tunnel import stop_tunnel

    stop_tunnel()
    state._RESTART.clear()
    state._CRASHES.clear()
    procs = list(state.CHILDREN.items())
    if state._BROWSER is not None:
        procs.append(("ui-browser", state._BROWSER))
    for name, proc in procs:
        try:
            proc.terminate()
        except OSError:
            pass


def livekit_target():
    url = setting("LIVEKIT_URL", "ws://127.0.0.1:7880")
    target = url.replace("ws://", "").replace("wss://", "").split("/")[0]
    host, _, port = target.partition(":")
    return host or "127.0.0.1", int(port or 7880)


def tcp_alive(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def agent_python() -> Path:
    from .envstore import venv_python

    return venv_python() if venv_python().exists() else Path(sys.executable)

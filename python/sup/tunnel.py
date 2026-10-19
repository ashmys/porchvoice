"""cloudflared quick tunnel for public publish mode."""
from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time

from . import paths, state
from config import detect_lan_ip
from .envstore import setting
from .logx import log

state.TUNNEL_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com",
                                 re.IGNORECASE)


def find_cloudflared() -> str:
    if paths.CLOUDFLARED_EXE.is_file():
        return str(paths.CLOUDFLARED_EXE)
    return shutil.which("cloudflared") or ""


def start_quick_tunnel() -> None:
    exe = find_cloudflared()
    if not exe:
        log("public mode: cloudflared binary not found (bin/cloudflared or "
            "PATH) - the web UI stays local; run setup menu 6 to install it")
        return
    proc = state.TUNNEL.get("proc")
    if proc is not None and proc.poll() is None:
        return
    cmd = [exe, "tunnel", "--url", f"http://127.0.0.1:{paths.PORT}",
           "--no-autoupdate", "--protocol", "http2"]
    log("[tunnel] starting: " + " ".join(cmd))
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            cwd=str(paths.ROOT))
    except OSError as exc:
        log(f"[tunnel] FAILED to start ({exc})")
        return
    state.TUNNEL["proc"] = proc
    state.TUNNEL["started_at"] = time.time()

    def pump() -> None:
        for line in proc.stdout:
            text = line.rstrip()
            state.TUNNEL["log"].append(text)
            m = state.TUNNEL_URL_RE.search(text)
            if m and not state.TUNNEL["url"]:
                state.TUNNEL["url"] = m.group(0)
                log(f"[tunnel] public URL: {state.TUNNEL['url']}")
            print(f"[tunnel] {text}", flush=True)

    threading.Thread(target=pump, daemon=True, name="tunnel-pump").start()

    def watch() -> None:
        code = proc.wait()
        state.TUNNEL["url"] = ""
        state.TUNNEL["proc"] = None
        log(f"[tunnel] cloudflared exited with code {code}")
        if code != 0 and not state._RESTART.get("stop-all"):
            log("[tunnel] retrying in 5s...")
            time.sleep(5)
            start_quick_tunnel()

    threading.Thread(target=watch, daemon=True, name="tunnel-watch").start()


def stop_tunnel() -> None:
    state._RESTART["stop-all"] = True
    proc = state.TUNNEL.get("proc")
    if proc is not None:
        try:
            proc.terminate()
        except OSError:
            pass
    state.TUNNEL["proc"] = None
    state.TUNNEL["url"] = ""


def tunnel_info() -> dict:
    proc = state.TUNNEL.get("proc")
    return {
        "mode": publish_mode(),
        "url": state.TUNNEL.get("url", ""),
        "running": bool(proc is not None and proc.poll() is None),
        "lan_url": "http://" + detect_lan_ip() + f":{paths.PORT}",
        "local_url": f"http://127.0.0.1:{paths.PORT}",
    }


def publish_mode() -> str:
    from config import load_config, publish_mode as mode_of

    return mode_of(load_config())

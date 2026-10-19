"""App-window launch: browser detection, standalone window, mic pre-grant."""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import paths, state
from config import detect_lan_ip
from .envstore import setting
from .logx import log

if os.name == "nt":
    import ctypes.wintypes  # noqa: F401 - needed for the window structures

_BROWSER_CATALOG = [
    ("brave", "Brave", True, [
        ("nt", ("BraveSoftware", "Brave-Browser", "Application", "brave.exe")),
        ("darwin", ("Brave Browser.app", "Contents", "MacOS", "Brave Browser")),
        ("unix", ("brave-browser", "brave")),
    ]),
    ("chrome", "Google Chrome", True, [
        ("nt", ("Google", "Chrome", "Application", "chrome.exe")),
        ("darwin", ("Google Chrome.app", "Contents", "MacOS", "Google Chrome")),
        ("unix", ("google-chrome", "google-chrome-stable", "chromium",
                  "chromium-browser")),
    ]),
    ("edge", "Microsoft Edge", True, [
        ("nt", ("Microsoft", "Edge", "Application", "msedge.exe")),
        ("darwin", ("Microsoft Edge.app", "Contents", "MacOS",
                    "Microsoft Edge")),
        ("unix", ("microsoft-edge", "microsoft-edge-stable")),
    ]),
    ("vivaldi", "Vivaldi", True, [
        ("nt", ("Vivaldi", "Application", "vivaldi.exe")),
        ("darwin", ("Vivaldi.app", "Contents", "MacOS", "Vivaldi")),
        ("unix", ("vivaldi", "vivaldi-stable")),
    ]),
    ("opera", "Opera", True, [
        ("nt", ("Opera", "launcher.exe")),
        ("darwin", ("Opera.app", "Contents", "MacOS", "Opera")),
        ("unix", ("opera",)),
    ]),
    ("chromium", "Chromium", True, [
        ("nt", ("Chromium", "Application", "chrome.exe")),
        ("darwin", ("Chromium.app", "Contents", "MacOS", "Chromium")),
        ("unix", ("chromium", "chromium-browser")),
    ]),
    ("firefox", "Firefox", False, [
        ("nt", ("Mozilla Firefox", "firefox.exe")),
        ("darwin", ("Firefox.app", "Contents", "MacOS", "firefox")),
        ("unix", ("firefox", "firefox-esr")),
    ]),
]

_APP_PATHS_EXES = {"brave": "brave.exe", "chrome": "chrome.exe",
                   "edge": "msedge.exe", "vivaldi": "vivaldi.exe",
                   "opera": "opera.exe", "chromium": "chromium.exe",
                   "firefox": "firefox.exe"}


def _win_prog_files_dirs() -> list:
    pf = os.environ.get("ProgramFiles", "C:\\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")
    lap = os.environ.get("LocalAppData", "")
    return [d for d in (lap, pf, pf86) if d]


def _win_app_paths_exe(exe_name: str):
    try:
        import winreg

        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(
                        root,
                        r"SOFTWARE\Microsoft\Windows\CurrentVersion"
                        r"\App Paths" + "\\" + exe_name) as k:
                    val, _typ = winreg.QueryValueEx(k, None)
            except OSError:
                continue
            if val:
                p = Path(str(val).strip().strip('"').split("\"")[0])
                if p.is_file():
                    return p
    except Exception:
        pass
    return None


def _win_default_browser_exe():
    if os.name != "nt":
        return None
    try:
        import winreg

        for assoc in ("https", "http"):
            try:
                with winreg.OpenKey(
                        winreg.HKEY_CURRENT_USER,
                        r"Software\Microsoft\Windows\Shell"
                        r"\Associations\UrlAssociations" + "\\"
                        + assoc + r"\UserChoice") as k:
                    progid, _typ = winreg.QueryValueEx(k, "ProgId")
            except OSError:
                continue
            if not progid:
                continue
            try:
                with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT,
                                    progid + r"\shell\open\command") as k:
                    cmd, _typ = winreg.QueryValueEx(k, None)
            except OSError:
                continue
            cmd = (cmd or "").strip()
            if not cmd:
                continue
            if cmd.startswith('"'):
                exe = cmd.split('"', 2)[1]
            else:
                exe = cmd.split(" ", 1)[0]
            p = Path(exe)
            if p.is_file():
                return p
    except Exception:
        pass
    return None


def _browser_id_for_exe(exe: Path):
    base = exe.name.lower()
    for bid, _name, chromium, _cands in _BROWSER_CATALOG:
        if base == _APP_PATHS_EXES[bid]:
            return bid, chromium
    if "brave" in base:
        return "brave", True
    if "msedge" in base or "edge" in base:
        return "edge", True
    if "firefox" in base:
        return "firefox", False
    if "vivaldi" in base:
        return "vivaldi", True
    if "opera" in base:
        return "opera", True
    if "chrome" in base:
        return "chrome", True
    return "custom", True


def _catalog_paths(bid: str) -> list:
    out = []
    for cid, _name, _chromium, cands in _BROWSER_CATALOG:
        if cid != bid:
            continue
        for plat, parts in cands:
            if plat == "nt" and os.name == "nt":
                for root in _win_prog_files_dirs():
                    out.append(Path(root).joinpath(*parts))
                ap = _win_app_paths_exe(parts[-1])
                if ap:
                    out.append(ap)
            elif plat == "darwin" and sys.platform == "darwin":
                out.append(Path("/Applications").joinpath(*parts))
            elif plat == "unix" and os.name != "nt" and sys.platform != "darwin":
                for exe in parts:
                    p = shutil.which(exe)
                    if p:
                        out.append(Path(p))
        break
    seen, uniq = set(), []
    for p in out:
        try:
            rp = str(p.resolve()).lower()
            if p.is_file() and os.access(str(p), os.X_OK) and rp not in seen:
                seen.add(rp)
                uniq.append(p)
        except OSError:
            continue
    return uniq


def detect_browsers() -> list:
    default = _win_default_browser_exe() if os.name == "nt" else None
    found, seen = [], set()
    if default is not None:
        try:
            rp = str(default.resolve()).lower()
        except OSError:
            rp = str(default).lower()
        bid, chromium = _browser_id_for_exe(default)
        found.append({"id": bid, "name": _display_name(bid, default),
                      "path": str(default), "chromium": chromium,
                      "default": True})
        seen.add(rp)
    for bid, name, chromium, _cands in _BROWSER_CATALOG:
        for p in _catalog_paths(bid):
            try:
                rp = str(p.resolve()).lower()
            except OSError:
                rp = str(p).lower()
            if rp in seen:
                continue
            seen.add(rp)
            found.append({"id": bid, "name": name, "path": str(p),
                          "chromium": chromium, "default": False})
    return found


def _display_name(bid: str, exe: Path) -> str:
    for cid, name, _chromium, _cands in _BROWSER_CATALOG:
        if cid == bid:
            return name
    return exe.stem.capitalize()


def load_window_state() -> dict:
    try:
        with open(paths.WINDOW_STATE_FILE, "r", encoding="utf-8") as f:
            st = json.load(f)
        if isinstance(st, dict):
            return st
    except Exception:
        pass
    return {}


def save_window_state(st: dict) -> None:
    try:
        paths.WINDOW_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = paths.WINDOW_STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(st), encoding="utf-8")
        os.replace(tmp, paths.WINDOW_STATE_FILE)
    except Exception as exc:
        log(f"could not save window state: {exc}")


if os.name == "nt":
    class _RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    class _MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.wintypes.DWORD),
                    ("rcMonitor", _RECT), ("rcWork", _RECT),
                    ("dwFlags", ctypes.wintypes.DWORD)]


def _win32():
    if os.name != "nt":
        return None
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.IsWindow.argtypes = [ctypes.wintypes.HWND]
    u.IsWindow.restype = ctypes.c_bool
    u.IsWindowVisible.argtypes = [ctypes.wintypes.HWND]
    u.IsWindowVisible.restype = ctypes.c_bool
    u.IsZoomed.argtypes = [ctypes.wintypes.HWND]
    u.IsZoomed.restype = ctypes.c_bool
    u.IsIconic.argtypes = [ctypes.wintypes.HWND]
    u.IsIconic.restype = ctypes.c_bool
    u.GetWindowTextLengthW.argtypes = [ctypes.wintypes.HWND]
    u.GetWindowTextLengthW.restype = ctypes.c_int
    u.GetWindowTextW.argtypes = [ctypes.wintypes.HWND, ctypes.c_wchar_p,
                                 ctypes.c_int]
    u.GetWindowRect.argtypes = [ctypes.wintypes.HWND, ctypes.POINTER(_RECT)]
    u.GetWindowThreadProcessId.argtypes = [ctypes.wintypes.HWND,
                                           ctypes.POINTER(ctypes.wintypes.DWORD)]
    u.GetWindow.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.UINT]
    u.GetWindow.restype = ctypes.wintypes.HWND
    u.MonitorFromWindow.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.DWORD]
    u.MonitorFromWindow.restype = ctypes.wintypes.HMONITOR
    u.GetMonitorInfoW.argtypes = [ctypes.wintypes.HMONITOR,
                                  ctypes.POINTER(_MONITORINFO)]
    u.ShowWindow.argtypes = [ctypes.wintypes.HWND, ctypes.c_int]
    try:
        u.GetDpiForWindow.restype = ctypes.c_uint
        u.GetDpiForWindow.argtypes = [ctypes.wintypes.HWND]
    except AttributeError:
        pass
    return u


def _window_title(u, hwnd) -> str:
    n = u.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    u.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value or ""


def _find_ui_window(u, pid: int, deadline: float):
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND,
                                     ctypes.wintypes.LPARAM)
    u.EnumWindows.argtypes = [WNDENUMPROC, ctypes.wintypes.LPARAM]
    while state.monotonic() < deadline:
        by_pid, by_title = [], []

        def _cb(hwnd, _lparam, _by_pid=by_pid, _by_title=by_title):
            try:
                if not u.IsWindowVisible(hwnd):
                    return True
                wpid = ctypes.wintypes.DWORD(0)
                u.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
                title = _window_title(u, hwnd)
                if not title or u.GetWindow(hwnd, 4):
                    return True
                if pid and wpid.value == pid:
                    _by_pid.append((hwnd, title))
                if title == paths.WINDOW_TITLE:
                    _by_title.append(hwnd)
            except Exception:
                pass
            return True

        cb = WNDENUMPROC(_cb)
        u.EnumWindows(cb, 0)
        if by_pid:
            return by_pid[0][0], True
        if by_title:
            return by_title[0], False
        time.sleep(0.5)
    return None, False


def _sample_window(u, hwnd) -> dict:
    rc = _RECT()
    if not u.GetWindowRect(hwnd, ctypes.byref(rc)):
        return {}
    zoomed = bool(u.IsZoomed(hwnd))
    fullscreen = False
    try:
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        if u.GetMonitorInfoW(u.MonitorFromWindow(hwnd, 2), ctypes.byref(mi)):
            m = mi.rcMonitor
            tol = 8
            fullscreen = (abs(rc.left - m.left) <= tol
                          and abs(rc.top - m.top) <= tol
                          and abs(rc.right - m.right) <= tol
                          and abs(rc.bottom - m.bottom) <= tol)
    except Exception:
        pass
    try:
        dpi = u.GetDpiForWindow(hwnd) or 96
    except Exception:
        dpi = 96
    mode = "fullscreen" if fullscreen else ("maximized" if zoomed else "windowed")
    return {"mode": mode, "x": int(rc.left), "y": int(rc.top),
            "w": int(rc.right - rc.left), "h": int(rc.bottom - rc.top),
            "dpi": int(dpi)}


def track_window(proc, saved_mode: str, chromium: bool = True) -> None:
    u = _win32()
    if u is None:
        return
    pid = proc.pid if hasattr(proc, "pid") else 0
    hwnd, owned = _find_ui_window(u, pid, state.monotonic() + 15)
    if not hwnd:
        log("could not track the app window (browser reused?) - heartbeat "
            "close-detection only")
        return
    if saved_mode in ("maximized", "fullscreen") and not u.IsZoomed(hwnd):
        u.ShowWindow(hwnd, 3)
    log("app window tracked - window position/size/mode will be remembered")
    last = None
    try:
        while True:
            state._CLOSE_CHECK.wait(1.0)
            state._CLOSE_CHECK.clear()
            if chromium and owned and proc.poll() is not None:
                from .procs import request_shutdown

                request_shutdown("app window closed (browser process exited)",
                                 fast=True)
                return
            if not chromium and proc.poll() is not None:
                return
            if not u.IsWindow(hwnd):
                from .procs import request_shutdown

                request_shutdown("app window closed", fast=True)
                return
            state._WIN_MINIMIZED = bool(u.IsIconic(hwnd))
            if state._WIN_MINIMIZED:
                continue
            try:
                st = _sample_window(u, hwnd)
            except Exception:
                continue
            if st and st != last:
                save_window_state(st)
                last = st
    finally:
        state._WIN_MINIMIZED = False


def app_browser_candidates() -> list:
    chosen = setting("UI_BROWSER", "").strip().strip('"')
    installed = detect_browsers()
    order = []

    def add(path, bid, chromium):
        try:
            key = str(Path(path).resolve()).lower()
        except OSError:
            key = str(path).lower()
        if not any(str(c[0]).lower() == key for c in order):
            order.append((Path(path), bid, chromium))

    if chosen and chosen.lower() not in ("auto", "default"):
        low = chosen.lower()
        exact = next((b for b in installed if b["id"] == low), None)
        if exact:
            add(exact["path"], exact["id"], bool(exact["chromium"]))
        else:
            custom = Path(chosen)
            if custom.is_file() and os.access(str(custom), os.X_OK):
                bid, chromium = _browser_id_for_exe(custom)
                add(custom, bid, chromium)
    default = next((b for b in installed if b.get("default")), None)
    if default:
        add(default["path"], default["id"], bool(default["chromium"]))
    for b in installed:
        add(b["path"], b["id"], bool(b["chromium"]))
    return order


def _spawn_browser(args: list) -> subprocess.Popen:
    if os.name == "nt":
        return subprocess.Popen(args, close_fds=True, creationflags=0x08000000)
    return subprocess.Popen(args, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)


def _seed_mic_permission(prof: Path) -> None:
    """Pre-grant microphone access to the app origin inside the dedicated
    browser profile so the app window never shows (or silently swallows) a
    permission prompt. Chromium stores per-origin grants in Preferences."""
    try:
        pref_dir = prof / "Default"
        pref_dir.mkdir(parents=True, exist_ok=True)
        pref_file = pref_dir / "Preferences"
        data = {}
        try:
            if pref_file.is_file():
                data = json.loads(pref_file.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        origins = {f"http://127.0.0.1:{paths.PORT},*": {"setting": 1},
                   f"http://localhost:{paths.PORT},*": {"setting": 1}}
        lan = detect_lan_ip()
        origins[f"http://{lan}:{paths.PORT},*"] = {"setting": 1}
        if state.TUNNEL.get("url"):
            origins[state.TUNNEL["url"] + ",*"] = {"setting": 1}
        prof_root = data.setdefault("profile", {})
        exceptions = prof_root.setdefault("content_settings", {}) \
            .setdefault("exceptions", {})
        mic = exceptions.setdefault("microphone", {})
        for k, v in origins.items():
            if k not in mic or mic[k].get("setting") != 1:
                mic[k] = dict(v, expiration="0",
                              last_modified=str(int(time.time())))
        audio_capture = exceptions.setdefault("audio_capture_allowed", {})
        for k, v in origins.items():
            if k not in audio_capture:
                audio_capture[k] = dict(v, expiration="0",
                                        last_modified=str(int(time.time())))
        prof_root.setdefault("content_settings", {})["pref_version"] = 1
        tmp = pref_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, pref_file)
        log("mic permission pre-granted for the app window profile "
            "(no prompt, no silent block)")
    except Exception as exc:
        log(f"could not pre-grant mic permission ({exc}) - the browser will ask")


def open_ui_when_ready() -> None:
    from .procs import tcp_alive
    from .winjob import assign_job

    url = f"http://{paths.HOST}:{paths.PORT}/"
    deadline = time.time() + 20
    while time.time() < deadline:
        if tcp_alive(paths.HOST, paths.PORT, timeout=0.5):
            break
        time.sleep(0.25)
    if setting("AUTO_OPEN", "1") != "1":
        log(f"UI ready at {url} (AUTO_OPEN=0 - not opening anything)")
        return

    if setting("UI_WINDOW", "1") == "1":
        candidates = app_browser_candidates()
        if candidates:
            tried = []
            for exe, bid, chromium in candidates:
                tried.append(exe.name)
                own_profile = (setting("UI_PROFILE", "1") == "1") and chromium
                try:
                    st = load_window_state()
                    mode = str(st.get("mode") or "windowed")
                    args = [str(exe)]
                    if chromium:
                        args.append(f"--app={url}")
                        args.append("--no-first-run")
                        args.append("--no-default-browser-check")
                        if own_profile:
                            prof = paths.ROOT / "user_data" / "browser-profile"
                            prof.mkdir(parents=True, exist_ok=True)
                            args.append(f"--user-data-dir={prof}")
                            _seed_mic_permission(prof)
                            if current_publish_mode() != "local":
                                # plain http LAN origins are not a secure
                                # context - getUserMedia would be missing
                                lan_url = f"http://{detect_lan_ip()}:{paths.PORT}"
                                args.append(
                                    "--unsafely-treat-insecure-origin-as-secure="
                                    + lan_url)
                    else:
                        args.append("--new-window")
                        args.append(url)
                        mode = "windowed"
                    if chromium and mode == "maximized":
                        args.append("--start-maximized")
                    elif chromium and mode == "fullscreen":
                        args.append("--start-fullscreen")
                    elif chromium:
                        mode = "windowed"
                        w, h = int(st.get("w") or 0), int(st.get("h") or 0)
                        x, y = int(st.get("x") or 0), int(st.get("y") or 0)
                        dpi = int(st.get("dpi") or 96) or 96
                        if 400 <= w <= 12000 and 300 <= h <= 8000:
                            scale = 96.0 / dpi
                            args.append(f"--window-size={round(w * scale)},"
                                        f"{round(h * scale)}")
                            if -w + 100 <= x <= 20000 and -40 <= y <= 20000:
                                args.append(f"--window-position="
                                            f"{round(x * scale)},{round(y * scale)}")
                        else:
                            args.append("--window-size=1240,860")
                    proc = _spawn_browser(args)
                except Exception as exc:
                    log(f"{exe.name} failed to launch ({exc}) - trying the "
                        "next browser")
                    continue
                state._BROWSER = proc
                assign_job(proc)
                state._BROWSER_JOBBED = state.JOB is not None and os.name == "nt"
                log(f"opened standalone app window via {exe.name} ({bid}) at "
                    f"{url} (mode: {mode})")
                if own_profile:
                    log("(own browser profile: user_data/browser-profile - the "
                        "app window is tracked exactly, and closing the "
                        "terminal closes it too; set UI_PROFILE=0 to use your "
                        "normal profile)")
                if state._BROWSER_JOBBED:
                    log("(browser bound to this console - closing the terminal "
                        "closes the app window too)")
                if mode == "windowed" and chromium and not int(st.get("w") or 0):
                    log("(first run - default size; the window you arrange is "
                        "remembered for next time)")
                log("(set UI_WINDOW=0 to use a normal browser tab instead)")
                if os.name == "nt":
                    threading.Thread(target=track_window,
                                     args=(proc, mode, chromium),
                                     daemon=True, name="win-track").start()
                else:
                    threading.Thread(target=_unix_watch_browser,
                                     args=(proc, chromium),
                                     daemon=True, name="win-track").start()
                if own_profile:
                    try:
                        if proc.wait(timeout=6) is not None:
                            log(f"{exe.name} exited immediately - retrying "
                                "with the next browser")
                            state._BROWSER = None
                            continue
                    except subprocess.TimeoutExpired:
                        pass
                return
            log(f"none of the detected browsers worked ({', '.join(tried)}) "
                "- opening the system default")
        else:
            log("no supported browser found for a standalone window - using "
                "the browser")
    try:
        if os.name == "nt":
            os.startfile(url)
        else:
            import webbrowser

            webbrowser.open(url)
        log(f"opened {url} in your browser (set AUTO_OPEN=0 to disable)")
    except Exception as exc:
        log(f"could not auto-open browser ({exc}) - open {url} manually")


def current_publish_mode() -> str:
    from .ggufsrv import current_mode

    return current_mode()


def _unix_watch_browser(proc, chromium: bool) -> None:
    if not chromium:
        return
    proc.wait()
    from .procs import request_shutdown

    request_shutdown("app window closed (browser process exited)", fast=True)

"""Setup steps: python packages, LiveKit, cloudflared, llama-server, GPU probe."""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

from . import core
from .core import (BIN_DIR, CORE_PKGS, LIVEKIT_VERSION, VENV_DIR,
                   ask, download_resumable, hr, pip_installed, run, venv_python,
                   zip_extract)


def livekit_download():
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("aarch64", "arm64") else "amd64"
    if system == "windows":
        name, kind = f"livekit_{LIVEKIT_VERSION}_windows_amd64.zip", "zip"
    elif system == "darwin":
        name, kind = f"livekit_{LIVEKIT_VERSION}_darwin_{arch}.zip", "zip"
    else:
        name, kind = f"livekit_{LIVEKIT_VERSION}_linux_{arch}.tar.gz", "tar.gz"
    url = ("https://github.com/livekit/livekit/releases/download/v"
           + LIVEKIT_VERSION + "/" + name)
    return url, kind


def step_deps() -> None:
    hr("STEP 1] Python venv + packages")
    if not VENV_DIR.exists():
        run([sys.executable, "-m", "venv", VENV_DIR])
    py = venv_python()
    if core.DRY_RUN:
        print("(dry-run) pip installs skipped")
        return
    run([py, "-m", "pip", "install", "--upgrade", "pip"])
    code = run([py, "-m", "pip", "install", *CORE_PKGS])
    if code != 0:
        raise SystemExit("Core package installation failed - check your "
                         "internet and retry.")
    print("  Core installed: livekit agents + plugins, dotenv, aiohttp.")
    print("  Everything else is OPTIONAL - pick only the engines you want "
          "(keeps the venv lean; you can add/remove anytime via menu 9/10).")
    for gid, _label, _pkgs, _why in core.OPTIONAL_GROUPS:
        core.install_group(gid)


def step_livekit() -> None:
    hr("STEP 2] LiveKit server")
    exe_name = "livekit-server.exe" if os.name == "nt" else "livekit-server"
    exe = BIN_DIR / exe_name
    if exe.exists() and exe.stat().st_size > 10_000_000:
        print(f"  already present: {exe}")
        return
    url, kind = livekit_download()
    apath = BIN_DIR / ("livekit.zip" if kind == "zip" else "livekit.tar.gz")
    download_resumable(url, apath, "LiveKit server", min_bytes=1_000_000)
    if core.DRY_RUN:
        return
    if kind == "zip":
        zip_extract(apath, BIN_DIR)
    else:
        with tarfile.open(apath) as t:
            try:
                t.extractall(BIN_DIR, filter="data")
            except TypeError:
                t.extractall(BIN_DIR)
    apath.unlink(missing_ok=True)
    if not exe.is_file():
        for p in BIN_DIR.rglob(exe_name):
            p.replace(exe)
            break
    print(f"  extracted -> {exe}")


def step_cloudflared() -> None:
    hr("STEP 6] cloudflared (for public publish mode)")
    exe_name = "cloudflared.exe" if os.name == "nt" else "cloudflared"
    exe = BIN_DIR / exe_name
    if exe.exists() and exe.stat().st_size > 1_000_000:
        print(f"  already present: {exe}")
        return
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("aarch64", "arm64") else "amd64"
    if system == "windows":
        url = ("https://github.com/cloudflare/cloudflared/releases/latest/"
               "download/cloudflared-windows-amd64.exe")
    elif system == "darwin":
        url = ("https://github.com/cloudflare/cloudflared/releases/latest/"
               "download/"
               + ("cloudflared-darwin-arm64.tgz" if arch == "arm64"
                  else "cloudflared-darwin-amd64.tgz"))
    else:
        url = ("https://github.com/cloudflare/cloudflared/releases/latest/"
               "download/" + f"cloudflared-linux-{arch}.tgz")
    if system != "windows":
        apath = BIN_DIR / "cloudflared.tgz"
        download_resumable(url, apath, "cloudflared", min_bytes=1_000_000)
        if core.DRY_RUN:
            return
        with tarfile.open(apath) as t:
            t.extractall(BIN_DIR)
        apath.unlink(missing_ok=True)
        if not exe.is_file():
            for p in BIN_DIR.rglob(exe_name):
                p.replace(exe)
                break
    else:
        download_resumable(url, exe, "cloudflared", min_bytes=1_000_000)
        return
    try:
        exe.chmod(exe.stat().st_mode | 0o755)
    except OSError:
        pass
    print(f"  extracted -> {exe}")


_GPU_PROBE_CODE = r"""
import json, os, site, sys
from pathlib import Path
for sp in site.getsitepackages():
    for sub in ("cudnn", "cublas"):
        d = Path(sp) / "nvidia" / sub / "bin"
        if d.is_dir():
            os.add_dll_directory(str(d))
            os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
out = {"cuda_devices": 0, "types": [], "error": ""}
try:
    import ctranslate2
    out["cuda_devices"] = ctranslate2.get_cuda_device_count()
    if out["cuda_devices"]:
        out["types"] = list(ctranslate2.get_supported_compute_types("cuda"))
except Exception as e:
    out["error"] = f"ctranslate2: {e}"
print("PROBE_JSON:" + json.dumps(out), flush=True)
sys.exit(0)
"""


def probe_gpu(timeout: int = 300) -> dict:
    if core.DRY_RUN:
        return {"cuda_devices": 1, "types": ["float16"], "load_ok": True,
                "error": ""}
    py = venv_python()
    if not py.exists():
        return {"cuda_devices": 0, "types": [], "load_ok": False,
                "error": "venv not built yet"}
    code = _GPU_PROBE_CODE
    try:
        r = subprocess.run(
            [str(py), "-c", code], capture_output=True, text=True,
            timeout=timeout, cwd=str(core.ROOT))
        for line in (r.stdout or "").splitlines():
            if line.startswith("PROBE_JSON:"):
                return json.loads(line[len("PROBE_JSON:"):])
        return {"cuda_devices": 0, "types": [], "load_ok": False,
                "error": (r.stderr or "no probe output")[-200:]}
    except subprocess.TimeoutExpired:
        return {"cuda_devices": 0, "types": [], "load_ok": False,
                "error": f"probe timed out after {timeout}s"}


LLAMA_RELEASE_API = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"


def latest_llama_assets() -> tuple:
    req = urllib.request.Request(LLAMA_RELEASE_API,
                                 headers={"User-Agent": "voice-kit-setup/1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    return data.get("tag_name", ""), [a.get("name", "")
                                      for a in data.get("assets", [])]


def pick_llama_assets(assets: list) -> list:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "windows":
        for key in ("-bin-win-cuda-", "-bin-win-vulkan-", "-bin-win-cpu-"):
            main = next((n for n in assets if n.startswith("llama")
                         and key in n and n.endswith("x64.zip")), "")
            if main:
                extras = ([n for n in assets if n.startswith("cudart-")
                           and key in n and n.endswith(".zip")]
                          if "cuda" in key else [])
                return [main] + extras
    elif system == "darwin":
        arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
        main = next((n for n in assets if n.startswith("llama-")
                     and f"-bin-macos-{arch}" in n and n.endswith(".zip")), "")
        return [main] if main else []
    else:
        main = next((n for n in assets if n.startswith("llama-")
                     and "-bin-ubuntu-x64" in n and n.endswith(".zip")), "")
        return [main] if main else []
    return []


def llama_server_present() -> bool:
    exe_name = "llama-server.exe" if os.name == "nt" else "llama-server"
    return (BIN_DIR / exe_name).is_file() or bool(shutil.which("llama-server"))


def download_llama_server() -> bool:
    exe_name = "llama-server.exe" if os.name == "nt" else "llama-server"
    if llama_server_present():
        return True
    if core.DRY_RUN:
        print("(dry-run) llama-server download skipped")
        return True
    print("  Fetching the latest llama.cpp release (llama-server)...")
    try:
        tag, assets = latest_llama_assets()
    except Exception as exc:
        print(f"  could not query llama.cpp releases ({exc})")
        print("  Download llama-server manually from "
              "https://github.com/ggml-org/llama.cpp/releases and put it in bin/")
        return False
    names = pick_llama_assets(assets)
    if not names:
        print("  no matching llama.cpp asset for this platform - download it "
              "manually from https://github.com/ggml-org/llama.cpp/releases")
        return False
    for name in names:
        url = f"https://github.com/ggml-org/llama.cpp/releases/download/{tag}/{name}"
        dest = BIN_DIR / name
        download_resumable(url, dest, name, min_bytes=1_000_000)
        zip_extract(dest, BIN_DIR)
        dest.unlink(missing_ok=True)
    if not (BIN_DIR / exe_name).is_file():
        for p in BIN_DIR.rglob(exe_name):
            p.replace(BIN_DIR / exe_name)
            break
    if (BIN_DIR / exe_name).is_file():
        print(f"  extracted -> {BIN_DIR / exe_name}")
        return True
    print(f"  {exe_name} not found after extraction - check bin/ manually")
    return False

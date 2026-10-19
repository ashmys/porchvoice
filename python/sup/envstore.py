"""Reading and writing user_data/.env.local."""
from __future__ import annotations

import os
from pathlib import Path

from keyvault import SECRET_KEYS, encrypt_value, is_encrypted

from . import paths, state
from .logx import log


def venv_python() -> Path:
    if os.name == "nt":
        return paths.ROOT / ".venv" / "Scripts" / "python.exe"
    return paths.ROOT / ".venv" / "bin" / "python"


def env_dict() -> dict:
    cfg = {}
    try:
        with open(state.ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    cfg[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return cfg


def repair_env_file() -> None:
    if not state.ENV_FILE.is_file():
        return
    try:
        lines = state.ENV_FILE.read_text(encoding="utf-8",
                                         errors="replace").splitlines()
    except OSError as exc:
        log(f"could not read {state.ENV_FILE.name} for repair ({exc})")
        return
    fixed = False
    out = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" not in stripped:
            line = "# " + stripped
            fixed = True
        out.append(line)
    if not fixed:
        return
    try:
        state.ENV_FILE.write_text("\n".join(out) + "\n", encoding="utf-8")
        log(f"repaired {state.ENV_FILE.name}: commented out line(s) that are not "
            "KEY=value (they made python-dotenv warn on every start)")
    except OSError as exc:
        log(f"could not rewrite {state.ENV_FILE.name} ({exc})")


def mask_secrets(cfg: dict) -> dict:
    out = dict(cfg)
    for k in SECRET_KEYS:
        if out.get(k):
            out[k] = paths.SECRET_SENTINEL
    return out


def write_env_keys(updates: dict) -> bool:
    """Persist a few KEY=value pairs into user_data/.env.local without
    touching anything else (existing keys, comments and ordering survive)."""
    try:
        lines = state.ENV_FILE.read_text(encoding="utf-8",
                                         errors="replace").splitlines() \
            if state.ENV_FILE.is_file() else []
    except OSError as exc:
        log(f"could not read {state.ENV_FILE.name}: {exc}")
        return False
    pending = {str(k): str(v) for k, v in updates.items() if str(k)}
    for k in list(pending):
        if k in SECRET_KEYS and pending[k] and not is_encrypted(pending[k]):
            pending[k] = encrypt_value(pending[k])
    out = []
    seen = set()
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k = stripped.split("=", 1)[0].strip()
            if k in pending:
                out.append(f"{k}={pending[k]}")
                seen.add(k)
                continue
        out.append(line)
    for k, v in pending.items():
        if k not in seen:
            out.append(f"{k}={v}")
    try:
        state.ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
        state.ENV_FILE.write_text("\n".join(out) + ("\n" if out else ""),
                                  encoding="utf-8")
    except OSError as exc:
        log(f"could not write {state.ENV_FILE.name}: {exc}")
        return False
    log("config updated: " + ", ".join(sorted(pending)))
    return True


def setting(key: str, default: str = "") -> str:
    from config import load_config

    cfg = load_config()
    v = cfg.get(key, "")
    if v:
        return v
    return os.environ.get(key, default)

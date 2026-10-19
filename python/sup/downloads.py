"""Background model downloads with progress for the web UI."""
from __future__ import annotations

import threading
import time
from pathlib import Path

from . import state
from .logx import log


def _dl_job_worker(job_key: str, url: str, dest: Path, min_bytes: int) -> None:
    job = state.DL_JOBS[job_key]
    try:
        import setup_wizard

        def _prog(written, total):
            job["done"], job["total"] = written, total

        setup_wizard.download_resumable(
            url, dest, dest.name, min_bytes=min_bytes, progress_cb=_prog)
        job["status"] = "done"
        job["done"] = job["total"] = dest.stat().st_size
        log(f"download finished: {dest.name}")
    except Exception as exc:
        job["status"] = "error"
        job["error"] = str(exc)[:300]
        log(f"download FAILED: {dest.name} ({exc})")


def dl_job_snapshot() -> dict:
    return {k: dict(v) for k, v in state.DL_JOBS.items()}


def jobs_running() -> bool:
    return not any(j.get("status") == "running" for j in state.DL_JOBS.values())


def start_download_job(job_key: str, url: str, dest: Path,
                       min_bytes: int = 100) -> dict:
    if job_key in state.DL_JOBS and state.DL_JOBS[job_key].get("status") == "running":
        return {"ok": False, "error": "download already running"}
    state.DL_JOBS[job_key] = {"status": "running", "done": 0, "total": 0,
                              "error": "", "file": dest.name,
                              "started_at": time.strftime("%H:%M:%S")}
    log(f"download started: {dest.name} ({job_key})")
    threading.Thread(target=_dl_job_worker, args=(job_key, url, dest, min_bytes),
                     daemon=True, name=f"dl-{job_key}").start()
    return {"ok": True}


def hf_resolve_file(repo: str, pattern: str) -> tuple:
    """Find the exact download URL for a quant inside a HF repo (the exact
    file naming differs between repos; match case-insensitively)."""
    import setup_wizard

    try:
        base = setup_wizard.pick_hf_endpoint(repo)
        files = setup_wizard.hf_list_files(base, repo)
    except SystemExit as exc:
        raise LookupError(str(exc)) from None
    p = pattern.lower()
    for f in files:
        name = f.get("path", "")
        if name.lower().endswith(".gguf") and p in name.lower():
            return f"{base}/{repo}/resolve/main/{name}", name
    raise LookupError(f"no *.gguf matching '{pattern}' in {repo}")

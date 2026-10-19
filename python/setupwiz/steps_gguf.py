"""Setup steps: GGUF ASR, GGUF chat LLM, optional modules, removal."""
from __future__ import annotations

import os
import shutil

from . import core
from .core import (BIN_DIR, GGUF_ASR_BASE, KOKORO_BASE, LLM_GGUF_DIR,
                   PIPER_BASE, WHISPER_BASE, ask, download_resumable, folder_size_mb,
                   hf_list_files, hr, pip_installed, pick_hf_endpoint, run,
                   venv_python, write_env)
from .steps_base import download_llama_server, llama_server_present

GGUF_ASR_BASE_URL = ("https://huggingface.co/ggml-org/Qwen3-ASR-0.6B-GGUF/"
                     "resolve/main")
GGUF_ASR_FILES = ("Qwen3-ASR-0.6B-Q8_0.gguf", "mmproj-Qwen3-ASR-0.6B-Q8_0.gguf")

LLM_GGUF_CATALOG = (
    # id, HF repo, filename pattern, approx MB, note
    ("Qwen3-0.6B-Q8_0.gguf", "Qwen/Qwen3-0.6B-GGUF", "Q8_0",
     640, "best small default - great quality for a voice assistant"),
    ("Qwen3-0.6B-Q4_K_M.gguf", "Qwen/Qwen3-0.6B-GGUF", "q4_k_m",
     400, "smallest, fastest, a bit rougher"),
    ("Qwen3-1.7B-Q4_K_M.gguf", "Qwen/Qwen3-1.7B-GGUF", "q4_k_m",
     1100, "smarter, needs ~1.5 GB VRAM"),
    ("Qwen3-4B-Q4_K_M.gguf", "Qwen/Qwen3-4B-GGUF", "q4_k_m",
     2500, "much smarter, needs ~3 GB VRAM (6 GB GPUs are fine)"),
    ("Qwen2.5-0.5B-Instruct-Q4_K_M.gguf", "Qwen/Qwen2.5-0.5B-Instruct-GGUF",
     "q4_k_m", 420, "older compact fallback"),
)


def step_gguf_asr(default_yes: str = "y") -> None:
    hr("STEP 7] GGUF ASR - Qwen3-ASR 0.6B via llama.cpp (speech-to-text)")
    print("  Speech-to-text with the Qwen3-ASR-0.6B GGUF model (~1 GB total).")
    print("  PorchVoice starts a local llama-server child automatically and")
    print("  transcribes through its OpenAI-compatible /v1/audio/transcriptions.")
    model_ok = all((GGUF_ASR_BASE / f).is_file() for f in GGUF_ASR_FILES)
    bin_ok = llama_server_present()
    print(f"  model files : {'present' if model_ok else 'missing'} ({GGUF_ASR_BASE})")
    print(f"  llama-server: {'present' if bin_ok else 'missing'}")
    if not model_ok:
        if ask("Download the Qwen3-ASR GGUF model? (Y/n)",
               default_yes).strip().lower() in ("", "y", "yes"):
            if core.DRY_RUN:
                print("(dry-run) GGUF model download skipped")
            else:
                for fname in GGUF_ASR_FILES:
                    url = f"{GGUF_ASR_BASE_URL}/{fname}"
                    min_bytes = 50_000_000 if fname.startswith("mmproj") \
                        else 500_000_000
                    download_resumable(url, GGUF_ASR_BASE / fname, fname,
                                       min_bytes=min_bytes)
                model_ok = all((GGUF_ASR_BASE / f).is_file()
                               for f in GGUF_ASR_FILES)
    if model_ok and not bin_ok:
        if ask("Download the llama.cpp llama-server binary? (Y/n)",
               default_yes).strip().lower() in ("", "y", "yes"):
            bin_ok = download_llama_server()
    if model_ok and bin_ok:
        if ask("Enable GGUF ASR now (STT_ENGINE=gguf)? (Y/n)", "y") \
                .strip().lower() in ("", "y", "yes"):
            write_env({"STT_ENGINE": "gguf"})
            print("  PorchVoice will start llama-server with Qwen3-ASR on "
                  "next launch.")
    else:
        print("  GGUF ASR not fully set up - the embedded Whisper engine stays "
              "active.")
        print("  You can retry later (setup menu 7) or point STT_GGUF_SERVER "
              "at an")
        print("  existing llama-server in the web UI settings.")


def llm_gguf_local_models() -> list:
    if not LLM_GGUF_DIR.is_dir():
        return []
    return [f for f in sorted(LLM_GGUF_DIR.glob("*.gguf"))
            if not f.name.lower().startswith("mmproj")]


def llm_gguf_download(preset) -> bool:
    pid, repo, pattern, _mb, _note = preset
    print(f"  resolving {pid} inside {repo} ...")
    try:
        base = pick_hf_endpoint(repo)
        files = hf_list_files(base, repo)
        name = next(f.get("path", "") for f in files
                    if f.get("path", "").lower().endswith(".gguf")
                    and pattern.lower() in f.get("path", "").lower()
                    and not f.get("path", "").lower().startswith("mmproj"))
    except StopIteration:
        print(f"  no *.gguf matching '{pattern}' found in {repo}")
        return False
    except SystemExit:
        raise
    except Exception as exc:
        print(f"  could not list {repo} ({exc})")
        return False
    url = f"{base}/{repo}/resolve/main/{name}"
    download_resumable(url, LLM_GGUF_DIR / name, name, min_bytes=50_000_000)
    return (LLM_GGUF_DIR / name).is_file()


def step_gguf_llm(default_yes: str = "y") -> None:
    hr("STEP 8] GGUF LLM - local chat model via llama.cpp")
    print("  Run a small chat LLM fully offline. PorchVoice starts its own")
    print("  llama-server child on port 8899 and talks to it over the")
    print("  OpenAI-compatible API - no Ollama / LM Studio needed.")
    local = llm_gguf_local_models()
    bin_ok = llama_server_present()
    print(f"  model folder : {LLM_GGUF_DIR}")
    if local:
        for f in local:
            print(f"    - {f.name} ({f.stat().st_size / 1e6:.0f} MB)")
    else:
        print("    (no chat models found yet)")
    print(f"  llama-server : {'present' if bin_ok else 'missing'}")
    model_name = ""
    if local:
        print("  Use a detected model?")
        for i, f in enumerate(local, 1):
            print(f"   {i}) {f.name}")
        print(f"   {len(local) + 1}) (download another one instead)")
        choice = ask("Number", "1")
        try:
            idx = int(choice) - 1
            model_name = local[idx].name if 0 <= idx < len(local) else ""
        except (ValueError, IndexError):
            model_name = local[0].name
    if not model_name:
        print("  Pick a model to download:")
        for i, (pid, _repo, _pat, mb, note) in enumerate(LLM_GGUF_CATALOG, 1):
            mark = ""
            for f in local:
                if _pat.lower() in f.name.lower():
                    mark = " (already downloaded)"
                    break
            print(f"   {i}) {pid:<38} ~{mb:>5} MB  {note}{mark}")
        choice = ask("Number", "1")
        try:
            preset = LLM_GGUF_CATALOG[int(choice) - 1]
        except (ValueError, IndexError):
            preset = LLM_GGUF_CATALOG[0]
        if core.DRY_RUN:
            print("(dry-run) GGUF LLM download skipped")
            return
        if ask(f"Download {preset[0]} (~{preset[3]} MB)? (Y/n)", default_yes) \
                .strip().lower() in ("", "y", "yes"):
            if llm_gguf_download(preset):
                model_name = preset[0]
    if not model_name:
        print("  GGUF LLM not set up - your other LLM config stays active.")
        return
    updates = {"LLM_GGUF_MODEL": model_name}
    engine_now = (core.read_env().get("LLM_ENGINE", "") or "").strip().lower()
    if engine_now != "gguf":
        if ask("Enable the GGUF LLM now (LLM_ENGINE=gguf)? (Y/n)", "y") \
                .strip().lower() in ("", "y", "yes"):
            updates["LLM_ENGINE"] = "gguf"
        else:
            print("  Kept auto mode: PorchVoice uses the GGUF LLM only when no")
            print("  other local LLM server answers (Ollama/LM Studio win).")
    write_env(updates)
    print(f"  The chat model '{model_name}' will be served at "
          "http://127.0.0.1:8899/v1 on next launch.")
    if not bin_ok:
        print("  llama-server binary is missing - menu 7 downloads it (needed).")


def step_modules() -> None:
    hr("STEP 9] Optional python modules (opt-in, avoid bloat)")
    print("  Install only the engines you actually use. Skipped engines can be")
    print("  added back here anytime; removal is menu 10 or the web UI.")
    for gid, _label, _pkgs, _why in core.OPTIONAL_GROUPS:
        core.install_group(gid)


def step_remove() -> None:
    hr("STEP 10] Remove unused modules / models (free disk space)")
    items = []  # (label, remover callable)
    pkgs = [("faster-whisper", "faster_whisper", "Embedded Whisper (STT)"),
            ("kokoro-onnx", "kokoro_onnx", "Kokoro TTS engine"),
            ("piper-tts", "piper", "Piper TTS engine"),
            ("mcp", "mcp", "MCP tool client"),
            ("nvidia-cublas-cu12", "nvidia.cublas", "NVIDIA CUDA runtime"),
            ("nvidia-cudnn-cu12", "nvidia.cudnn", "NVIDIA cuDNN runtime")]
    for pip_name, mod_name, label in pkgs:
        if _spec_present(mod_name):
            items.append((f"python package: {label}",
                          lambda n=pip_name: (
                              run([venv_python(), "-m", "pip", "uninstall",
                                   "-y", n]), True)[1]))
    file_groups = [
        ("whisper models (user_data/models/whisper)", WHISPER_BASE),
        ("kokoro voice model (user_data/models/kokoro)", KOKORO_BASE),
        ("piper voices (user_data/models/piper)", PIPER_BASE),
        ("GGUF ASR model (user_data/models/gguf-asr)", GGUF_ASR_BASE),
        ("GGUF chat models (user_data/models/llm)", LLM_GGUF_DIR),
    ]
    for label, base in file_groups:
        if base.is_dir() and any(base.iterdir()):
            items.append((f"model folder: {label} "
                          f"(~{folder_size_mb(base)} MB)",
                          lambda b=base: (shutil.rmtree(b), True)[1]))
    for exe_name, label in (("llama-server",
                             "llama.cpp llama-server binary"),
                            ("cloudflared", "cloudflared tunnel binary")):
        exe = BIN_DIR / (exe_name + (".exe" if os.name == "nt" else ""))
        if exe.is_file():
            items.append((f"binary: {label} "
                          f"(~{exe.stat().st_size / 1e6:.0f} MB)",
                          lambda e=exe: (e.unlink(), True)[1]))
    if not items:
        print("  Nothing removable found - already minimal.")
        return
    print("  Removable items (your settings in .env.local are NOT touched):")
    for i, (label, _fn) in enumerate(items, 1):
        print(f"   {i}) {label}")
    print("   0) remove nothing")
    choice = ask("Number to remove (or comma-separated list, e.g. 1,3)", "0")
    picked = []
    for part in choice.replace(" ", "").split(","):
        try:
            idx = int(part) - 1
            if 0 <= idx < len(items):
                picked.append(idx)
        except ValueError:
            continue
    if not picked:
        print("  Removed nothing.")
        return
    for idx in picked:
        label, fn = items[idx]
        print(f"  removing: {label} ...")
        try:
            fn()
        except Exception as exc:
            print(f"    failed ({exc})")
    print("  Note: engines still selected in .env.local will fail until you")
    print("  reinstall them or pick another engine in Settings.")


def _spec_present(mod_name: str) -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec(mod_name) is not None
    except Exception:
        return False

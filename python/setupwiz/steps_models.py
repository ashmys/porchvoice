"""Setup steps: STT model (Whisper), TTS engines (Kokoro / Piper)."""
from __future__ import annotations

import subprocess
from pathlib import Path

from . import core
from .core import (GGUF_ASR_BASE, KOKORO_BASE, KOKORO_BASE_URL,
                   KOKORO_FILES, KOKORO_VOICES, PIPER_BASE, PIPER_BASE_URL,
                   PIPER_VOICES, WHISPER_BASE, WHISPER_ORDER, WHISPER_SIZES_MB,
                   ask, download_resumable, hf_list_files, hr, pick_hf_endpoint,
                   pip_installed, run, venv_python, whisper_repo)
from .steps_base import probe_gpu


def step_whisper_prefetch() -> None:
    hr("STEP 4] Whisper STT model pre-download + GPU test")
    print("  Pick the model to pre-download (speed vs quality):")
    for i, name in enumerate(WHISPER_ORDER, 1):
        mark = (" (already downloaded)"
                if (WHISPER_BASE / name / "model.bin").is_file() else "")
        print(f"   {i}) {name:<16} ~{WHISPER_SIZES_MB[name]} MB{mark}")
    default_idx = WHISPER_ORDER.index("small") + 1
    choice = ask("Number", str(default_idx))
    try:
        model = WHISPER_ORDER[int(choice) - 1]
    except (ValueError, IndexError):
        model = "small"
    print(f"  Selected: {model}")
    core.write_env({"WHISPER_MODEL": model})

    if core.DRY_RUN:
        print("(dry-run) download skipped")
        return
    dest_dir = WHISPER_BASE / model
    bin_file = dest_dir / "model.bin"
    if bin_file.exists() and bin_file.stat().st_size > 50_000_000:
        print("  model.bin already downloaded - skipping")
    else:
        repo = whisper_repo(model)
        base = pick_hf_endpoint(repo)
        files = hf_list_files(base, repo)
        print(f"  downloading {len(files)} files from {repo} ...")
        for finfo in files:
            fpath = finfo["path"]
            url = f"{base}/{repo}/resolve/main/{fpath}"
            download_resumable(url, dest_dir / Path(fpath).name,
                               Path(fpath).name, min_bytes=100)

    probe = probe_gpu()
    if probe.get("cuda_devices"):
        print(f"  GPU: CUDA available ({probe.get('types')}) - Whisper will use it")
    else:
        print(f"  GPU: not usable ({probe.get('error', 'not found')}) - "
              "Whisper will run on CPU (fine for tiny/base/small)")
    print("  Test-loading the model (first run may take a minute)...")
    if not pip_installed(["faster-whisper"]):
        print("  faster-whisper is not installed - skipping the load test.")
        print("  Install it (setup menu 9) or use STT engine gguf/remote instead.")
        return
    py = venv_python()
    code = (
        "import sys; sys.path.insert(0, r'%s');\n"
        "from local_stt import create_stt;\n"
        "create_stt(model=r'%s', device='auto', compute_type='auto');\n"
        "print('WHISPER LOAD TEST OK')"
    ) % (str(Path(__file__).resolve().parent.parent), str(dest_dir))
    try:
        r = subprocess.run([str(py), "-c", code], cwd=str(core.ROOT), timeout=600)
        if r.returncode != 0:
            print("  Load test failed - check the error above. The kit still runs")
            print("  if you pick a smaller model via the web UI.")
    except subprocess.TimeoutExpired:
        print("  Load test timed out - try a smaller model.")


def kokoro_ready_files() -> bool:
    model_dest = KOKORO_BASE / "kokoro-v1.0.onnx"
    voices_dest = KOKORO_BASE / "voices-v1.0.bin"
    return (model_dest.is_file() and model_dest.stat().st_size > 100_000_000
            and voices_dest.is_file())


def step_kokoro() -> bool:
    hr("STEP 5a] Kokoro voice (default TTS engine)")
    if core.DRY_RUN:
        print("(dry-run) kokoro download skipped")
        return True
    if kokoro_ready_files():
        print("  kokoro model already downloaded - skipping")
        return True
    for fname in KOKORO_FILES:
        url = f"{KOKORO_BASE_URL}/{fname}"
        min_bytes = 20_000_000 if fname.endswith(".bin") else 100_000_000
        download_resumable(url, KOKORO_BASE / fname, fname, min_bytes=min_bytes)
    print("  Test-synthesizing with kokoro-onnx...")
    py = venv_python()
    code = (
        "from kokoro_onnx import Kokoro;\n"
        f"k = Kokoro(r'{KOKORO_BASE / 'kokoro-v1.0.onnx'}', "
        f"r'{KOKORO_BASE / 'voices-v1.0.bin'}');\n"
        "samples, sr = k.create('Kokoro voice test.', voice='af_heart', "
        "speed=1.0, lang='en-us');\n"
        "assert len(samples) > 1000;\n"
        "print('KOKORO TEST OK')"
    )
    try:
        r = subprocess.run([str(py), "-c", code], cwd=str(core.ROOT), timeout=600)
        if r.returncode != 0:
            print("  Kokoro test failed - check the error above. You can switch")
            print("  TTS_ENGINE=piper in the settings instead.")
            return False
    except subprocess.TimeoutExpired:
        print("  Kokoro test timed out.")
        return False
    return True


def step_piper() -> None:
    hr("STEP 5] Piper TTS voice")
    print("  Pick a voice:")
    for i, (name, _sub) in enumerate(PIPER_VOICES, 1):
        have = (PIPER_BASE / f"{name}.onnx").is_file()
        print(f"   {i}) {name}{' (already downloaded)' if have else ''}")
    choice = ask("Number", "1")
    try:
        name, sub = PIPER_VOICES[int(choice) - 1]
    except (ValueError, IndexError):
        name, sub = PIPER_VOICES[0]
    print(f"  Selected: {name}")
    core.write_env({"TTS_VOICE": name + ".onnx"})
    if core.DRY_RUN:
        return
    for suffix in (".onnx", ".onnx.json"):
        url = f"{PIPER_BASE_URL}/{sub}/{name}{suffix}"
        download_resumable(url, PIPER_BASE / f"{name}{suffix}", name + suffix)
    print("  Test-synthesizing...")
    py = venv_python()
    code = (
        "from piper import PiperVoice;\n"
        "v = PiperVoice.load(r'%s');\n"
        "list(v.synthesize('Hello, the voice works.'));\n"
        "print('PIPER TEST OK')"
    ) % str(PIPER_BASE / f"{name}.onnx")
    try:
        r = subprocess.run([str(py), "-c", code], cwd=str(core.ROOT), timeout=300)
        if r.returncode != 0:
            print("  Piper test failed - check the error above.")
    except subprocess.TimeoutExpired:
        print("  Piper test timed out.")


def step_voice() -> None:
    hr("STEP 5] Voice engine (TTS)")
    print("  1) Kokoro (default - best quality, ~340 MB download)")
    print("  2) Piper (lightweight, ~60 MB voice)")
    print("  3) Skip (choose later in the web UI)")
    choice = ask("Number", "1")
    if choice == "2":
        core.write_env({"TTS_ENGINE": "piper"})
        step_piper()
        return
    if choice == "3":
        print("  Skipped - the web UI can pick an engine later (TTS_ENGINE).")
        return
    core.write_env({"TTS_ENGINE": "kokoro"})
    print("  Pick a Kokoro voice:")
    for i, name in enumerate(KOKORO_VOICES, 1):
        print(f"   {i}) {name}")
    vchoice = ask("Number", "1")
    try:
        voice = KOKORO_VOICES[int(vchoice) - 1]
    except (ValueError, IndexError):
        voice = KOKORO_VOICES[0]
    print(f"  Selected: {voice}")
    core.write_env({"KOKORO_VOICE": voice})
    if not step_kokoro():
        print("  Falling back to Piper (TTS_ENGINE=piper).")
        core.write_env({"TTS_ENGINE": "piper"})
        step_piper()

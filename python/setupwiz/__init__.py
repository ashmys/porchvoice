"""PorchVoice setup wizard, split into focused step modules."""
from . import core  # noqa: F401 - flags + helpers live here
from .menu import main, run_all, step_config
from .steps_base import download_llama_server, probe_gpu, step_cloudflared, \
    step_deps, step_livekit
from .steps_gguf import step_gguf_asr, step_gguf_llm, step_modules, step_remove
from .steps_models import step_kokoro, step_piper, step_voice, \
    step_whisper_prefetch

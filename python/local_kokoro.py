from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

import numpy as np
from livekit import rtc
from livekit.agents import APIConnectOptions, tts

logger = logging.getLogger("local-kokoro")

ROOT = Path(__file__).resolve().parent.parent
KOKORO_DIRS = [ROOT / "user_data" / "models" / "kokoro"]
SAMPLES_PER_FRAME = 4096
MODEL_FILE = "kokoro-v1.0.onnx"
VOICES_FILE = "voices-v1.0.bin"

VOICE_PRESETS = (
    "af_heart", "af_alloy", "af_aoede", "af_bella", "af_jessica", "af_kore",
    "af_nicole", "af_nova", "af_river", "af_sarah", "af_sky",
    "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam", "am_michael",
    "am_onyx", "am_puck", "am_santa",
    "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
    "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
)

VOICE_LANGS = {
    "a": "en-us", "b": "en-gb", "e": "es", "f": "fr-fr", "h": "hi",
    "i": "it", "j": "ja", "p": "pt-br", "z": "zh-cn",
}


def find_model() -> tuple[Path, Path] | None:
    for base in KOKORO_DIRS:
        model = base / MODEL_FILE
        voices = base / VOICES_FILE
        if model.is_file() and voices.is_file():
            return model, voices
    return None


def kokoro_ready() -> bool:
    return find_model() is not None


def voice_lang(voice: str) -> str:
    return VOICE_LANGS.get((voice or "a")[:1].lower(), "en-us")


def create_kokoro_tts(*, voice_name: str = "af_heart", speed: float = 1.0,
                      lang: str = ""):
    paths = find_model()
    if paths is None:
        raise FileNotFoundError(
            "Kokoro model not found: user_data/models/kokoro/"
            f"{MODEL_FILE} + {VOICES_FILE}\n"
            "Run setup (voice step) or switch TTS_ENGINE to piper."
        )

    from kokoro_onnx import Kokoro

    model_path, voices_path = paths
    logger.info("Loading Kokoro: %s", model_path)
    engine = Kokoro(str(model_path), str(voices_path))
    available = []
    try:
        available = [str(n) for n in engine.voices]
    except Exception:
        pass
    voice = (voice_name or "af_heart").strip()
    if available and voice not in available:
        fallback = "af_heart" if "af_heart" in available else available[0]
        logger.warning("Kokoro voice '%s' not in voices.bin - using '%s'",
                       voice, fallback)
        voice = fallback
    rate = voice_lang(voice) if not lang else lang
    logger.info("Kokoro voice loaded (%s, lang %s)", voice, rate)

    class KokoroStream(tts.ChunkedStream):
        def __init__(self, tts_obj, text, conn_options=None):
            self._text = text
            self._request_id = uuid.uuid4().hex[:12]
            super().__init__(
                tts=tts_obj,
                input_text=text,
                conn_options=conn_options or APIConnectOptions(),
            )

        async def _run(self, output_emitter) -> None:
            raise RuntimeError("KokoroStream uses _main_task")

        def _synth(self, text: str):
            samples, sr = engine.create(text, voice=voice, speed=speed,
                                        lang=rate)
            pcm = (np.clip(np.asarray(samples, dtype=np.float32).reshape(-1),
                           -1.0, 1.0) * 32767.0).astype(np.int16)
            return pcm, int(sr)

        async def _main_task(self) -> None:
            try:
                pcm, sr = await asyncio.to_thread(self._synth, self._text)
                if pcm.size == 0:
                    logger.warning("Kokoro produced no audio for %r",
                                   self._text[:80])
                    return
                offset = 0
                total = pcm.size
                while offset < total:
                    n = min(SAMPLES_PER_FRAME, total - offset)
                    frame = rtc.AudioFrame(
                        data=pcm[offset:offset + n].tobytes(),
                        sample_rate=sr,
                        num_channels=1,
                        samples_per_channel=n,
                    )
                    self._event_ch.send_nowait(tts.SynthesizedAudio(
                        frame=frame, request_id=self._request_id))
                    offset += n
            except Exception:
                logger.exception("Kokoro synthesis failed")
                raise

    class KokoroTTS(tts.TTS):
        def __init__(self) -> None:
            super().__init__(
                capabilities=tts.TTSCapabilities(streaming=False),
                sample_rate=24000,
                num_channels=1,
            )

        def synthesize(self, text: str, *, conn_options=None):
            return KokoroStream(self, text, conn_options)

    return KokoroTTS()

from __future__ import annotations

import asyncio
import io
import json
import logging
import wave

import numpy as np
from livekit.agents import APIConnectOptions, stt as lk_stt

logger = logging.getLogger("remote-stt")

SAMPLE_RATE = 16000


def buffer_to_wav_bytes(buffer) -> bytes:
    arr = buffer.data
    if isinstance(arr, (bytes, bytearray, memoryview)):
        arr = np.frombuffer(bytes(arr), dtype=np.int16)
    else:
        arr = np.asarray(arr).astype(np.int16, copy=False).reshape(-1)
    num_ch = getattr(buffer, "num_channels", 1) or 1
    sr = int(getattr(buffer, "sample_rate", SAMPLE_RATE) or SAMPLE_RATE)
    if num_ch > 1:
        arr = arr.reshape(-1, num_ch).mean(axis=1).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(arr.tobytes())
    return buf.getvalue()


class RemoteSTT(lk_stt.STT):
    def __init__(self, *, base_url: str, api_key: str = "", model: str = "whisper-1",
                 language: str = "en") -> None:
        super().__init__(
            capabilities=lk_stt.STTCapabilities(streaming=False, interim_results=False)
        )
        import aiohttp

        self._session: aiohttp.ClientSession | None = None
        self._base_url = base_url.rstrip("/")
        self._api_key = (api_key or "").strip()
        self._model = model or "whisper-1"
        self._language = None if language in ("auto", "") else language

    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            import aiohttp

            headers = {"Authorization": "Bearer " + self._api_key} if self._api_key else {}
            self._session = aiohttp.ClientSession(headers=headers)
        return self._session

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _recognize_impl(self, buffer, *, language=None, conn_options=None):
        t0 = asyncio.get_event_loop().time()
        text = await self._transcribe(buffer)
        lang = language or self._language or "en"
        duration = getattr(buffer, "duration", 0.0)
        if text:
            logger.info("Remote STT heard %r", text[:120])
        return lk_stt.SpeechEvent(
            type=lk_stt.SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[
                lk_stt.SpeechData(
                    language=lang, text=text, start_time=0.0, end_time=duration
                )
            ],
        )

    async def _transcribe(self, buffer) -> str:
        import aiohttp

        session = await self._ensure_session()
        wav = await asyncio.to_thread(buffer_to_wav_bytes, buffer)
        form = aiohttp.FormData()
        form.add_field(
            "file", wav, filename="audio.wav",
            content_type="audio/wav",
        )
        form.add_field("model", self._model)
        if self._language:
            form.add_field("language", self._language)
        url = self._base_url + "/audio/transcriptions"
        try:
            async with session.post(url, data=form,
                                    timeout=aiohttp.ClientTimeout(total=60)) as resp:
                body = await resp.text()
                if resp.status != 200:
                    logger.error("Remote STT HTTP %s at %s: %s",
                                 resp.status, url, body[:200])
                    return ""
                data = json.loads(body)
                return (data.get("text") or "").strip()
        except Exception as exc:
            logger.error("Remote STT request failed (%s)", exc)
            return ""


def create_remote_stt(*, base_url: str, api_key: str = "", model: str = "whisper-1",
                      language: str = "en") -> RemoteSTT:
    logger.info("Remote STT: %s (model=%s, key %s)",
                base_url, model, "set" if api_key else "none")
    return RemoteSTT(base_url=base_url, api_key=api_key, model=model, language=language)

from __future__ import annotations

import asyncio
import io
import json
import logging
import uuid

import numpy as np
from livekit import rtc
from livekit.agents import APIConnectOptions, tts as lk_tts

logger = logging.getLogger("remote-tts")

TARGET_RATE = 16000
FRAME_SAMPLES = 480


def decode_audio_bytes(data: bytes) -> tuple[np.ndarray, int]:
    import av

    container = av.open(io.BytesIO(data))
    resampler = av.AudioResampler(format="s16", layout="mono", rate=TARGET_RATE)
    frames = []
    try:
        for packet in container.demux(audio=0):
            for frame in packet.decode():
                for out in resampler.resample(frame):
                    arr = out.to_ndarray().reshape(-1)
                    frames.append(arr)
    finally:
        container.close()
    if not frames:
        return np.zeros(0, dtype=np.int16), TARGET_RATE
    return np.concatenate(frames).astype(np.int16), TARGET_RATE


class RemoteTTS(lk_tts.TTS):
    def __init__(self, *, base_url: str, api_key: str = "", model: str = "tts-1",
                 voice: str = "alloy", speed: float = 1.0) -> None:
        super().__init__(
            capabilities=lk_tts.TTSCapabilities(streaming=False),
            sample_rate=TARGET_RATE,
            num_channels=1,
        )
        import aiohttp

        self._aiohttp = aiohttp
        self._session = None
        self._base_url = base_url.rstrip("/")
        self._api_key = (api_key or "").strip()
        self._model = model or "tts-1"
        self._voice = voice or "alloy"
        self._speed = speed

    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            headers = {"Authorization": "Bearer " + self._api_key} if self._api_key else {}
            self._session = self._aiohttp.ClientSession(headers=headers)
        return self._session

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def synthesize(self, text: str, *, conn_options=None) -> "RemoteTTStream":
        return RemoteTTStream(self, text, conn_options)


class RemoteTTStream(lk_tts.ChunkedStream):
    def __init__(self, tts_obj: RemoteTTS, text: str, conn_options=None) -> None:
        super().__init__(
            tts=tts_obj,
            input_text=text,
            conn_options=conn_options or APIConnectOptions(),
        )
        self._tts_obj = tts_obj
        self._text = text
        self._request_id = uuid.uuid4().hex[:12]

    async def _run(self, output_emitter) -> None:
        raise RuntimeError("RemoteTTStream uses _main_task")

    async def _main_task(self) -> None:
        t = self._tts_obj
        payload = {
            "model": t._model,
            "voice": t._voice,
            "input": self._text,
            "response_format": "mp3",
            "speed": t._speed,
        }
        try:
            session = await t._ensure_session()
            url = t._base_url + "/audio/speech"
            timeout = t._aiohttp.ClientTimeout(total=120)
            async with session.post(url, json=payload, timeout=timeout) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:200]
                    logger.error("Remote TTS HTTP %s at %s: %s",
                                 resp.status, url, body)
                    raise RuntimeError(f"remote tts http {resp.status}")
                raw = await resp.read()
            pcm, sr = await asyncio.to_thread(decode_audio_bytes, raw)
            if pcm.size == 0:
                logger.warning("Remote TTS produced no audio for %r", self._text[:80])
                return
            offset = 0
            total = pcm.size
            while offset < total:
                n = min(FRAME_SAMPLES, total - offset)
                chunk = pcm[offset:offset + n]
                frame = rtc.AudioFrame(
                    data=chunk.tobytes(),
                    sample_rate=sr,
                    num_channels=1,
                    samples_per_channel=n,
                )
                self._event_ch.send_nowait(
                    lk_tts.SynthesizedAudio(frame=frame, request_id=self._request_id)
                )
                offset += n
        except Exception as exc:
            logger.error("Remote TTS synthesis failed (%s)", exc)
            raise


def create_remote_tts(*, base_url: str, api_key: str = "", model: str = "tts-1",
                      voice: str = "alloy", speed: float = 1.0) -> RemoteTTS:
    logger.info("Remote TTS: %s (model=%s voice=%s key %s)",
                base_url, model, voice, "set" if api_key else "none")
    return RemoteTTS(base_url=base_url, api_key=api_key, model=model,
                     voice=voice, speed=speed)

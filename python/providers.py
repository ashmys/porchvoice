from __future__ import annotations

import logging

from livekit.agents import llm

logger = logging.getLogger("porchvoice-providers")


class _TapStream:
    """Forward an LLMStream to the caller while reporting each text delta,
    so the UI can show the assistant's words as they are generated."""

    def __init__(self, inner, on_reply) -> None:
        self._inner = inner
        self._on_reply = on_reply
        self._acc = []
        self._msg_id = ""

    async def __aenter__(self):
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return await self._inner.__aexit__(exc_type, exc, tb)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            chunk = await self._inner.__anext__()
        except StopAsyncIteration:
            self._acc.clear()
            raise
        delta = getattr(chunk, "delta", None)
        piece = getattr(delta, "content", None) if delta is not None else None
        if piece:
            self._msg_id = self._msg_id or str(getattr(chunk, "id", "") or "b-tap")
            self._acc.append(piece)
            try:
                self._on_reply("".join(self._acc), self._msg_id, False, "")
            except Exception:
                logger.exception("on_reply tap failed")
        return chunk

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _TapLLM(llm.LLM):
    """Wrap a livekit LLM plugin and tap its streamed text deltas."""

    def __init__(self, inner, on_reply) -> None:
        super().__init__()
        self._inner = inner
        self._on_reply = on_reply

    def chat(self, *, chat_ctx, tools=None, conn_options=None, **kwargs):
        stream = self._inner.chat(chat_ctx=chat_ctx, tools=tools,
                                  conn_options=conn_options, **kwargs)
        return _TapStream(stream, self._on_reply)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def build_llm(cfg: dict, probe=None, on_error=None, on_reply=None,
              on_think=None):
    from config import resolve_llm

    sel = resolve_llm(cfg, probe=probe)
    if sel["kind"] == "none":
        raise RuntimeError(
            "No LLM provider found. Set LLM_BASE_URL (any OpenAI-compatible "
            "server, local http:// or cloud https://), download a GGUF chat "
            "model (setup menu 8 / Settings), or set LLM_API_KEY/"
            "OPENAI_API_KEY, then restart the agent."
        )
    if sel.get("engine") == "gguf" and not (cfg.get("LLM_GGUF_SERVER", "") or "").strip():
        # managed child: the supervisor starts llama-server right after the
        # agent, give it a moment to come up before the first request
        _wait_gguf_server(sel["base_url"], timeout=30.0)
    compat = str(cfg.get("LLM_COMPAT_MODE", "0")).strip() == "1"
    streaming = str(cfg.get("LLM_STREAMING", "1")).strip() == "1"
    thinking = str(cfg.get("LLM_THINKING", "0")).strip() == "1"
    speak_thinking = thinking and str(cfg.get("LLM_SPEAK_THINKING", "0")).strip() == "1"
    key = sel["api_key"] or "sk-no-key"
    model = sel["model"] or "local-model"
    if compat:
        from local_llm import create_textgen_llm

        logger.info("LLM: compat adapter at %s (model=%s, streaming=%s)",
                    sel["base_url"], model, streaming)
        return create_textgen_llm(
            base_url=sel["base_url"], api_key=key, model=model,
            streaming=streaming, thinking=thinking, speak_thinking=speak_thinking,
            on_error=on_error, on_reply=on_reply, on_think=on_think,
        )
    from livekit.plugins.openai import LLM as OpenAILLM

    logger.info("LLM: %s (%s, key %s, model=%s)",
                sel["base_url"], sel["kind"],
                "set" if sel["api_key"] else "none", model)
    inner = OpenAILLM(base_url=sel["base_url"], api_key=key, model=model)
    if on_reply is not None:
        return _TapLLM(inner, on_reply)
    return inner


def build_stt(cfg: dict):
    from config import resolve_stt

    sel = resolve_stt(cfg)
    if sel["engine"] in ("remote", "gguf") and sel["base_url"]:
        from remote_stt import create_remote_stt

        if sel["engine"] == "gguf":
            _wait_gguf_server(sel["base_url"])
        return create_remote_stt(
            base_url=sel["base_url"], api_key=sel["api_key"],
            model=sel["model"], language=cfg.get("WHISPER_LANGUAGE", "en"),
        )
    from local_stt import create_stt

    return create_stt(
        model=cfg.get("WHISPER_MODEL", "small"),
        device=cfg.get("WHISPER_DEVICE", "auto"),
        compute_type=cfg.get("WHISPER_COMPUTE", "auto"),
        language=cfg.get("WHISPER_LANGUAGE", "en"),
        beam_size=int(str(cfg.get("WHISPER_BEAM", "1") or 1)),
        vad_filter=str(cfg.get("WHISPER_VAD_FILTER", "1")) == "1",
        temperature=float(str(cfg.get("WHISPER_TEMPERATURE", "0.0") or 0)),
        min_logprob=float(str(cfg.get("WHISPER_MIN_LOGPROB", "-1.0") or -1)),
        max_compratio=float(str(cfg.get("WHISPER_MAX_COMPRATIO", "2.4") or 2.4)),
        initial_prompt=cfg.get("WHISPER_INITIAL_PROMPT", ""),
    )


def _health_urls(base_url: str) -> list:
    base = base_url.rstrip("/")
    root = base[:-3] if base.endswith("/v1") else base
    urls = [root + "/health"]
    if root != base:
        urls.append(base + "/health")
    return urls


def _wait_gguf_server(base_url: str, timeout: float = 20.0) -> None:
    import time
    import urllib.error
    import urllib.request

    logger.info("GGUF STT: waiting for the ASR server at %s ...", base_url)
    urls = _health_urls(base_url)
    deadline = time.time() + timeout
    while time.time() < deadline:
        for url in urls:
            try:
                with urllib.request.urlopen(url, timeout=2) as r:
                    if r.status == 200:
                        logger.info("GGUF STT server is ready at %s", base_url)
                        return
            except urllib.error.HTTPError as e:
                if e.code != 503:
                    logger.info("GGUF STT server is up at %s (HTTP %s on %s)",
                                base_url, e.code, url)
                    return
            except Exception:
                pass
        time.sleep(0.5)
    logger.warning("GGUF STT server not ready at %s after %.0fs - the first "
                   "utterance may fail if it never comes up. Check the "
                   "supervisor log for the stt-gguf child.", base_url, timeout)


def build_tts(cfg: dict):
    from config import resolve_tts

    sel = resolve_tts(cfg)
    if sel["engine"] == "remote" and sel["base_url"]:
        from remote_tts import create_remote_tts

        return create_remote_tts(
            base_url=sel["base_url"], api_key=sel["api_key"],
            model=sel["model"], voice=sel["voice"], speed=sel["speed"],
        )
    if sel["engine"] == "kokoro":
        from local_kokoro import create_kokoro_tts

        return create_kokoro_tts(
            voice_name=sel["kokoro_voice"] or "af_heart",
            speed=sel["speed"],
        )
    from local_tts import create_piper_tts

    return create_piper_tts(
        voice_name=sel["voice"] or "en_US-amy-medium.onnx",
        speed=sel["speed"],
    )

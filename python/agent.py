import asyncio
import ast
import difflib
import json
import logging
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

ENV_FILE = ROOT / "user_data" / ".env.local"
CHAT_DIR = ROOT / "user_data" / "chat"

load_dotenv(ENV_FILE)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("porchvoice-agent")

ERROR_HINTS = (
    (("401", "unauthorized", "invalid api key", "invalid_api_key",
      "no auth credentials", "incorrect api key"),
     "LLM rejected the API key (401). Check the key in Settings."),
    (("402", "insufficient", "credits", "quota exceeded", "billing"),
     "LLM usage limit reached (402): out of credits or quota on this "
     "provider. Top up or switch provider."),
    (("429", "rate limit", "too many requests", "rate_limit"),
     "LLM rate limit hit (429): usage is limited right now. Wait a bit, "
     "switch model, or change provider."),
    (("403", "forbidden"),
     "LLM refused the request (403): key lacks access to this model."),
    (("timeout", "timed out", "connection", "unreachable", "getaddrinfo",
      "failed to establish", "temporarily unavailable", "502", "503",
      "504", "refused"),
     "Cannot reach the LLM API. Check the URL, your network, and the "
     "provider status in Settings."),
)


def friendly_llm_error(detail: str) -> str:
    d = (detail or "").lower()
    for needles, hint in ERROR_HINTS:
        if any(n in d for n in needles):
            return hint
    return "LLM error: " + ((detail or "unknown")[:220])


CONFIG_FALLBACK = {
    "WHISPER_MODEL": "small",
    "WHISPER_DEVICE": "auto",
    "WHISPER_COMPUTE": "auto",
    "TTS_VOICE": "en_US-amy-medium.onnx",
    "TTS_SPEED": "1.1",
    "TTS_ENGINE": "kokoro",
    "KOKORO_VOICE": "af_heart",
    "STT_BASE_URL": "",
    "TTS_BASE_URL": "",
    "LLM_BASE_URL": "",
    "LLM_MODEL": "",
}

from config import (
    HEAVY_KEYS,
    load_config,
    masked,
    resolve_mcp,
    safe_room_name,
)
from mcp_client import create_mcp_toolset
from providers import build_llm, build_stt, build_tts

from livekit import agents
from livekit.agents import (
    Agent,
    AgentSession,
    RoomInputOptions,
    RoomOutputOptions,
    function_tool,
    stt as lk_stt,
)
from livekit.plugins import silero

INSTRUCTIONS = (
    "You are PorchVoice, a helpful AI voice assistant running locally on the "
    "user's own hardware. Keep replies short and natural for spoken "
    "conversation: usually 1-3 sentences, plain conversational text. Never "
    "use markdown, code blocks, bullet points, or emoji - your words are "
    "spoken out loud. If the user asks for something long, summarize it "
    "first and offer to give more detail. You can use the tools available "
    "to you when they would answer the user's question."
)


class EchoGuard:
    def __init__(self, tail: float = 0.8, threshold: float = 0.55) -> None:
        self._tail = max(0.0, tail)
        self._threshold = threshold
        self._speaking = False
        self._quiet_at = 0.0
        self._last_spoken = ""

    def on_agent_state(self, ev) -> None:
        state = getattr(ev, "new_state", "")
        if state == "speaking":
            self._speaking = True
        elif state in ("listening", "thinking", "idle"):
            self._speaking = False
            self._quiet_at = time.time()

    def note_spoken(self, text: str) -> None:
        text = (text or "").strip()
        if text:
            self._last_spoken = text

    def in_window(self) -> bool:
        return self._speaking or (time.time() - self._quiet_at) <= self._tail

    def check(self, text: str):
        if not text or not text.strip():
            return False, ""
        if not self.in_window():
            return False, ""
        if not self._last_spoken:
            return False, ""
        sim = echo_similarity(text, self._last_spoken)
        if sim >= self._threshold:
            return True, "matches the assistant's last words (similarity %.2f)" % sim
        return False, ""


def echo_similarity(a: str, b: str) -> float:
    a, b = (a or "").lower().strip(), (b or "").lower().strip()
    if not a or not b:
        return 0.0
    ta = set(re.findall(r"\w+", a))
    tb = set(re.findall(r"\w+", b))
    union = ta | tb
    jac = len(ta & tb) / len(union) if union else 0.0
    seq = difflib.SequenceMatcher(None, a, b).ratio()
    return max(jac, seq)


def build_echo_guard(cfg: dict):
    if cfg.get("ECHO_GUARD", "1") != "1":
        logger.info("Echo guard disabled by settings (ECHO_GUARD=0)")
        return None
    tail = _num(cfg, "ECHO_GUARD_TAIL", "0.8")
    logger.info("Echo guard ON: agent-speech window + %.2fs tail", tail)
    return EchoGuard(tail=tail)


class GuardedSTT(lk_stt.STT):
    def __init__(self, inner, guard: EchoGuard) -> None:
        super().__init__(
            capabilities=lk_stt.STTCapabilities(streaming=False, interim_results=False)
        )
        self._inner = inner
        self._guard = guard

    async def _recognize_impl(self, buffer, *, language=None, conn_options=None):
        ev = await self._inner.recognize(
            buffer, language=language, conn_options=conn_options
        )
        text = ""
        if ev.alternatives:
            text = (ev.alternatives[0].text or "").strip()
        if text:
            drop, why = self._guard.check(text)
            if drop:
                logger.warning(
                    "ECHO GUARD dropped transcript %r (%s) - the mic picked up "
                    "the assistant's own voice",
                    text, why,
                )
                return lk_stt.SpeechEvent(
                    type=lk_stt.SpeechEventType.FINAL_TRANSCRIPT,
                    alternatives=[lk_stt.SpeechData(
                        language=(ev.alternatives[0].language or language or "en"),
                        text="", start_time=0.0, end_time=0.0)],
                )
        return ev


class TranscriptHub:
    PARTIAL_MIN_GAP = 0.12

    def __init__(self, room, history_path=None) -> None:
        self._room = room
        self._seq = 0
        self._tasks = set()
        self._history = None
        self._history_path = None
        self._written = 0
        self._pending_id = ""
        self._pending_text = ""
        self._last_partial_pub = 0.0
        self._last_final = ("", "", 0.0)
        if history_path is not None:
            try:
                CHAT_DIR.mkdir(parents=True, exist_ok=True)
                self._history_path = Path(history_path)
                self._history = open(self._history_path, "a", encoding="utf-8")
                logger.info("Chat log: %s", history_path)
                self._write_history("system", "session-start", "")
            except OSError as exc:
                logger.warning("Chat log disabled (%s)", exc)

    def _write_history(self, who: str, text: str, msg_id: str, think: str = "") -> None:
        if self._history_path is None:
            return
        rec = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "who": who,
            "text": text,
            "id": msg_id,
        }
        if think:
            rec["think"] = think
        try:
            if self._history is None:
                try:
                    self._history = open(self._history_path, "a", encoding="utf-8")
                except OSError as exc:
                    logger.warning("chat history reopen failed (%s)", exc)
                    return
            line = json.dumps(rec, ensure_ascii=False)
            self._history.write(line + "\n")
            self._history.flush()
            os.fsync(self._history.fileno())
            self._written += 1
        except OSError as exc:
            logger.warning("chat history write failed (%s)", exc)
    def _send(self, who: str, text: str, msg_id: str, final: bool,
              think: str = "") -> None:
        if who == "bot" and not final:
            now = time.monotonic()
            if now - self._last_partial_pub < self.PARTIAL_MIN_GAP:
                return
            self._last_partial_pub = now
            if self._pending_id and self._pending_id != msg_id:
                self._finalize(self._pending_id, self._pending_text, "")
            self._pending_id = msg_id
            self._pending_text = text
        if final:
            self._write_history(who, text, msg_id, think)
        payload = {"who": who, "text": text, "id": msg_id, "final": final}
        if think:
            payload["think"] = think
        self._publish(payload, "transcript")

    def _finalize(self, msg_id: str, text: str, think: str) -> None:
        now = time.monotonic()
        prev_text, _prev_id, prev_at = self._last_final
        fresh = text and text == prev_text and (now - prev_at) < 3.0
        if not fresh:
            self._write_history("bot", text, msg_id, think)
            self._publish({"who": "bot", "text": text, "id": msg_id,
                           "final": True}, "transcript")
            self._last_final = (text, msg_id, now)
        if self._pending_id == msg_id:
            self._pending_id = ""
            self._pending_text = ""

    def _publish(self, payload: dict, topic: str) -> None:
        try:
            coro = self._room.local_participant.publish_data(
                json.dumps(payload, ensure_ascii=False),
                reliable=True, topic=topic)
        except Exception as exc:
            logger.warning("%s publish failed (%s)", topic, exc)
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coro.close()
            logger.warning("%s publish skipped - no event loop yet", topic)
            return
        task = loop.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(lambda t, topic=topic: self._on_pub_done(topic, t))

    def _on_pub_done(self, topic: str, task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception():
            logger.warning("%s publish failed (%s)", topic, task.exception())

    def think(self, text: str, msg_id: str, final: bool) -> None:
        payload = {"who": "bot", "text": "", "id": msg_id,
                   "final": False, "think": text or "",
                   "think_final": final}
        self._publish(payload, "transcript")

    def user(self, text: str) -> None:
        text = (text or "").strip()
        if text:
            self._seq += 1
            msg_id = f"u{self._seq}"
            self._send("user", text, msg_id, True)
            logger.info("user said: %s", text)

    def bot(self, text: str, msg_id: str = "", final: bool = True,
            think: str = "") -> None:
        text = (text or "").strip()
        if text or think:
            if final:
                self._finalize(msg_id, text, think)
                if self._last_final[1] != msg_id:
                    return
            else:
                self._send("bot", text, msg_id, False, think)

    def adopt_pending(self, item_id: str) -> str:
        """Let a completed assistant item finalize under the same bubble id
        the live stream already created, so the UI has exactly one bubble."""
        return self._pending_id or item_id

    def error(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        self._seq += 1
        msg_id = f"e{self._seq}"
        self._write_history("error", text, msg_id)
        self._publish({"who": "error", "text": text, "id": msg_id,
                       "final": True}, "transcript")
        logger.error("surfaced to chat: %s", text)

    def tool_call(self, name: str, args: str, output: str, is_error: bool = False) -> None:
        payload = {"name": name, "arguments": args[:500],
                   "output": (output or "")[:800], "error": is_error}
        self._publish(payload, "tool-call")
        logger.info("tool %s(%s) -> %s", name, args[:120],
                    (output or "")[:160])

    def system_note(self, text: str) -> None:
        text = (text or "").strip()
        if text:
            self._write_history("system", text, "")
            logger.info("chat log note: %s", text)

    def close(self) -> None:
        if self._history is not None:
            try:
                self._history.close()
            except OSError:
                pass
            logger.info("Chat log closed: %d record(s) written this session",
                        self._written)
            self._history = None


def build_vad(cfg: dict):
    return silero.VAD.load(
        min_speech_duration=0.05,
        min_silence_duration=_num(cfg, "VAD_MIN_SILENCE", "0.55"),
        prefix_padding_duration=0.3,
        activation_threshold=_num(cfg, "VAD_ACTIVATION_THRESHOLD", "0.45"),
        deactivation_threshold=0.35,
    )


def _num(cfg: dict, key: str, default: str, cast=float):
    raw = str(cfg.get(key, "") or "").strip()
    if not raw:
        return cast(default)
    try:
        return cast(raw)
    except ValueError:
        logger.warning("Config %s=%r is not a valid number - using default %s",
                       key, raw, default)
        return cast(default)


def history_path_for(room_name: str) -> Path:
    safe = safe_room_name(room_name)
    try:
        CHAT_DIR.mkdir(parents=True, exist_ok=True)
        existing = sorted(CHAT_DIR.glob(f"*-{safe}.jsonl"),
                          key=lambda p: p.stat().st_mtime, reverse=True)
        if existing:
            logger.info("Resuming existing chat log for room %s", room_name)
            return existing[0]
    except OSError:
        pass
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return CHAT_DIR / f"{stamp}-{safe}.jsonl"


def _loopback_api(base: str) -> bool:
    try:
        host = re.sub(r"^https?://", "", base).split("/")[0].split(":")[0]
        return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0")
    except Exception:
        return False


def fix_local_model_name(cfg: dict) -> dict:
    base = (cfg.get("LLM_BASE_URL", "") or "").strip()
    if not base or not _loopback_api(base):
        return cfg
    model = (cfg.get("LLM_MODEL", "") or "").strip()
    key = (cfg.get("LLM_API_KEY", "") or "").strip()
    headers = {"Authorization": "Bearer " + key} if key else {}
    try:
        req = urllib.request.Request(base.rstrip("/") + "/models", headers=headers)
        with urllib.request.urlopen(req, timeout=4) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        items = (data.get("data") or data.get("models") or []) if isinstance(data, dict) else (data or [])
        ids = []
        for m in items:
            if isinstance(m, dict):
                mid = m.get("id") or m.get("name") or m.get("model")
                if mid:
                    ids.append(str(mid))
            elif isinstance(m, str):
                ids.append(m)
    except Exception:
        return cfg
    if not ids:
        return cfg
    if model and model in ids:
        return cfg
    fixed = dict(cfg)
    fixed["LLM_MODEL"] = ids[0]
    logger.warning("Model '%s' is not on this local server's model list - "
                   "using '%s' for this session (pick a real id in Settings "
                   "to make it permanent).", model or "(empty)", ids[0])
    return fixed


_MODELS = {"fp": None, "vad": None, "stt": None, "tts": None, "last_good": {}}
_MODELS_LOCK = threading.Lock()


def models_fingerprint(cfg: dict) -> str:
    return "|".join(cfg.get(k, "") for k in HEAVY_KEYS)


def get_models(cfg: dict):
    fp = models_fingerprint(cfg)
    with _MODELS_LOCK:
        if _MODELS["fp"] == fp and _MODELS["vad"] is not None:
            return _MODELS["vad"], _MODELS["stt"], _MODELS["tts"]
        t0 = time.time()
        logger.info("Loading voice models (this happens once per settings change)...")
        _MODELS["vad"] = build_vad(cfg)
        _MODELS["stt"] = build_stt(cfg)
        _MODELS["tts"] = build_tts(cfg)
        _MODELS["fp"] = fp
        _MODELS["last_good"] = {k: cfg.get(k, "") for k in HEAVY_KEYS}
        logger.info("Voice models ready in %.1fs.", time.time() - t0)
        return _MODELS["vad"], _MODELS["stt"], _MODELS["tts"]


def prewarm_models() -> None:
    def _run() -> None:
        try:
            get_models(load_config())
        except Exception:
            logger.exception("Prewarm failed - models will load at the first "
                             "conversation start instead.")
    threading.Thread(target=_run, name="model-prewarm", daemon=True).start()


class Assistant(Agent):
    def __init__(self) -> None:
        super().__init__(instructions=INSTRUCTIONS)

    @function_tool
    async def get_current_time(self) -> str:
        """Get the user's current local date and time.

        Use this whenever the user asks about the time, the date, or words
        like today, tomorrow, or tonight.
        """
        return datetime.now().strftime("It is %H:%M on %A, %B %d, %Y.")

    @function_tool
    async def calculate(self, expression: str) -> str:
        """Evaluate a simple arithmetic expression.

        Use this when the user asks for a quick calculation with numbers and
        the operators + - * / ( ).

        Args:
            expression: A plain arithmetic expression, for example (2 + 3) * 4
        """
        try:
            node = ast.parse(expression, mode="eval")
            allowed = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant,
                       ast.Add, ast.Sub, ast.Mult, ast.Div, ast.USub, ast.UAdd,
                       ast.Mod, ast.Pow, ast.FloorDiv)
            if any(type(n) not in allowed for n in ast.walk(node)):
                return "Sorry, I can only do plain arithmetic."
            value = eval(compile(node, "<calc>", "eval"),
                         {"__builtins__": {}}, {})
            return f"{expression} = {value}"
        except Exception:
            return "Sorry, I could not evaluate that expression."


async def entrypoint(ctx: agents.JobContext) -> None:
    await ctx.connect()
    logger.info("Agent joined room %s", ctx.room.name)

    hub = None
    session = None
    early_inputs: list[str] = []

    try:
        cfg = load_config()
        try:
            vad, stt, tts = await asyncio.to_thread(get_models, cfg)
        except Exception:
            logger.exception("Loading models with the new settings FAILED - "
                             "retrying with the previous settings. Check the "
                             "model/voice names in the web UI.")
            prev = dict(cfg)
            prev.update(_MODELS.get("last_good")
                        or {k: CONFIG_FALLBACK.get(k, "") for k in HEAVY_KEYS})
            vad, stt, tts = await asyncio.to_thread(get_models, prev)

        hub = TranscriptHub(ctx.room, history_path_for(ctx.room.name))
        guard = build_echo_guard(cfg)

        def publish_tool(name: str, args: str, output: str,
                         is_error: bool = False) -> None:
            hub.tool_call(name, args, output, is_error)

        def on_llm_error(detail: str) -> None:
            hub.error(friendly_llm_error(detail))

        def on_llm_think(text: str, msg_id: str, final: bool) -> None:
            hub.think(text, msg_id, final)

        def on_llm_reply(text: str, msg_id: str, final: bool,
                         think: str = "") -> None:
            hub.bot(text, msg_id=msg_id, final=final, think=think)

        mcp_sel = resolve_mcp(cfg)
        mcp_toolset = None
        if mcp_sel["enabled"]:
            try:
                mcp_toolset = create_mcp_toolset(
                    mcp_sel["url"], mcp_sel["token"], mcp_sel["timeout"])
                logger.info("MCP toolset attached: %s", mcp_sel["url"])
            except Exception as exc:
                logger.error("MCP toolset could not be created (%s) - "
                             "continuing without MCP tools", exc)

        session = AgentSession(
            vad=vad,
            stt=lk_stt.StreamAdapter(
                stt=GuardedSTT(stt, guard) if guard is not None else stt,
                vad=vad,
            ),
            llm=build_llm(cfg, on_error=on_llm_error, on_think=on_llm_think,
                          on_reply=on_llm_reply),
            tts=tts,
            turn_handling={
                "turn_detection": None,
                "endpointing": {
                    "mode": "fixed",
                    "min_delay": _num(cfg, "ENDPOINTING_DELAY", "0.7"),
                    "max_delay": 6.0,
                },
                "interruption": {
                    "mode": "vad",
                    "min_duration": 0.6,
                    "min_words": 2,
                },
            },
            tools=[mcp_toolset] if mcp_toolset is not None else [],
        )

        def _extract_text(payload) -> str:
            try:
                data = json.loads(bytes(payload).decode("utf-8"))
            except Exception:
                return ""
            return (data.get("text") or "").strip() if isinstance(data, dict) else ""

        def on_data_early(ev) -> None:
            topic = getattr(ev, "topic", "") or ""
            if topic != "chat-input":
                return
            text = _extract_text(getattr(ev, "data", b""))
            if text:
                early_inputs.append(text)

        ctx.room.on("data_received")(on_data_early)

        await session.start(
            room=ctx.room,
            agent=Assistant(),
            room_input_options=RoomInputOptions(audio_enabled=True,
                                                text_enabled=True),
            room_output_options=RoomOutputOptions(audio_enabled=True,
                                                  transcription_enabled=False),
        )
        logger.info("Session started for room %s", ctx.room.name)

        def on_user_transcript(ev) -> None:
            if ev.is_final and getattr(ev, "transcript", ""):
                hub.user(ev.transcript)

        session.on("user_input_transcribed")(on_user_transcript)

        def on_session_error(ev) -> None:
            err = getattr(ev, "error", None)
            if err is None:
                return
            code = getattr(err, "status_code", None) or ""
            body = getattr(err, "body", None)
            detail = str(getattr(err, "message", "") or body or err)
            if code:
                detail = f"HTTP {code}: {detail}"
            hub.error(friendly_llm_error(detail))

        session.on("error")(on_session_error)

        def item_text(item) -> str:
            txt = (getattr(item, "text_content", None) or "").strip()
            if txt:
                return txt
            parts = [c for c in (getattr(item, "content", None) or [])
                     if isinstance(c, str)]
            return " ".join(parts).strip()

        def on_conversation_item(ev) -> None:
            item = getattr(ev, "item", None)
            if item is None:
                return
            role = str(getattr(item, "role", ""))
            item_id = str(getattr(item, "id", "") or "")
            if role == "assistant":
                txt = item_text(item)
                if txt:
                    hub.bot(txt, msg_id=hub.adopt_pending(item_id), final=True)
                if guard is not None:
                    guard.note_spoken(txt)

        session.on("conversation_item_added")(on_conversation_item)

        def clear_model_context() -> None:
            try:
                session.history.truncate(max_items=0)
                logger.info("model context cleared (Clear button)")
            except Exception:
                logger.exception("clearing model context failed")

        def on_data(ev) -> None:
            topic = getattr(ev, "topic", "") or ""
            if topic == "ctx-clear":
                clear_model_context()
                hub.system_note("conversation cleared (model context reset)")
                return
            if topic != "chat-input":
                return
            text = _extract_text(getattr(ev, "data", b""))
            if not text:
                return
            hub.user(text)
            asyncio.ensure_future(reply_to_text(session, hub, text))

        ctx.room.on("data_received")(on_data)

        if early_inputs:
            logger.info("replaying %d message(s) typed while models loaded",
                        len(early_inputs))
            for text in list(early_inputs):
                hub.user(text)
                asyncio.ensure_future(reply_to_text(session, hub, text))
            early_inputs.clear()

        if guard is not None:
            session.on("agent_state_changed")(guard.on_agent_state)

        def on_tools_executed(ev) -> None:
            try:
                for call, out in zip(ev.function_calls,
                                     ev.function_call_outputs):
                    publish_tool(
                        getattr(call, "name", ""),
                        getattr(call, "arguments", "") or "",
                        getattr(out, "output", "") or "",
                        is_error=bool(getattr(out, "is_error", False)),
                    )
            except Exception:
                logger.exception("tool event publish failed")

        session.on("function_tools_executed")(on_tools_executed)

        if mcp_sel["enabled"]:
            hub.system_note("MCP tools attached from " + mcp_sel["url"])

        await session.generate_reply(
            instructions="Greet the user in one short friendly sentence and "
                         "offer to help."
        )
    except Exception as exc:
        logger.exception("Agent session for room %s ended with an error",
                         ctx.room.name)
        logger.error("The conversation stopped, but the agent worker is "
                     "still alive.")
        if hub is not None:
            try:
                hub.error(friendly_llm_error(str(exc)))
            except Exception:
                logger.exception("error publish failed")
        msg = str(exc).lower()
        if "Connection" in type(exc).__name__ or "connect" in msg:
            logger.error("FIX -> check the LLM provider and the LiveKit server, "
                         "then press Start conversation in the browser again.")
        else:
            logger.error("FIX -> read the traceback above. Then press Start "
                         "conversation in the browser to open a fresh session.")
    finally:
        if hub is not None:
            hub.close()


async def reply_to_text(session, hub, text: str) -> None:
    try:
        await session.generate_reply(user_input=text)
    except Exception as exc:
        logger.exception("text chat reply failed")
        try:
            hub.error(friendly_llm_error(str(exc)))
        except Exception:
            pass


def preflight() -> None:
    cfg = load_config()
    logger.info("-" * 60)
    logger.info("PREFLIGHT: checking dependencies...")

    lk_key = (cfg.get("LIVEKIT_API_KEY", "") or "").strip()
    lk_secret = (cfg.get("LIVEKIT_API_SECRET", "") or "").strip()
    if lk_key and lk_secret:
        logger.info("PREFLIGHT: LiveKit credentials OK (key %s)",
                    masked(lk_key))
    else:
        logger.error("PREFLIGHT: LiveKit API key/secret missing - defaults "
                     "apply automatically; set LIVEKIT_API_KEY/LIVEKIT_API_SECRET "
                     "in user_data/.env.local to override.")

    lk = cfg.get("LIVEKIT_URL", "ws://127.0.0.1:7880")
    target = re.sub(r"^wss?://", "", lk).split("/")[0]
    host, _, port = target.partition(":")
    host = host or "127.0.0.1"
    port = int(port or 7880)
    try:
        socket.create_connection((host, port), timeout=3).close()
        logger.info("PREFLIGHT: LiveKit server OK at %s:%s", host, port)
    except OSError as e:
        logger.error("PREFLIGHT: LiveKit server NOT reachable at %s:%s (%s).",
                     host, port, e)
        logger.error("PREFLIGHT: FIX -> start it via start.sh/start.bat first.")

    from providers import build_llm as _bl

    try:
        _bl(cfg)
        logger.info("PREFLIGHT: LLM provider resolved OK")
    except Exception as e:
        logger.error("PREFLIGHT: LLM provider NOT available (%s)", e)
        logger.error("PREFLIGHT: FIX -> set LLM_BASE_URL to an OpenAI-compatible "
                     "server (local http:// or cloud https://) or set "
                     "LLM_API_KEY/OPENAI_API_KEY in user_data/.env.local.")

    mcp_sel = resolve_mcp(cfg)
    if mcp_sel["enabled"]:
        logger.info("PREFLIGHT: MCP %s (bearer %s)",
                    mcp_sel["url"], "set" if mcp_sel["token"] else "none")
    else:
        logger.info("PREFLIGHT: MCP not configured (set MCP_URL to enable tools)")
    logger.info("-" * 60)


if __name__ == "__main__":
    try:
        boot_cfg = load_config()
        preflight()
        prewarm_models()
        agents.cli.run_app(
            agents.WorkerOptions(
                entrypoint_fnc=entrypoint,
                ws_url=(boot_cfg.get("LIVEKIT_URL", "")
                        or "ws://127.0.0.1:7880").strip(),
                api_key=(boot_cfg.get("LIVEKIT_API_KEY", "")
                         or "devkey").strip(),
                api_secret=(boot_cfg.get("LIVEKIT_API_SECRET", "")
                            or "porchvoice-secret-1234567890abcdef").strip(),
            )
        )
    except KeyboardInterrupt:
        sys.exit(0)

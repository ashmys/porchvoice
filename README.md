# PorchVoice

A local-first realtime voice assistant that lives on your own hardware. Talk to it out loud, it listens, thinks, and answers with a natural voice. Speech-to-text and text-to-speech run locally by default, every provider URL is auto-detected (`http://` = local, `https://` = cloud, local always tried first), every API key is optional, and MCP tools plug in through a single URL. Built on the **livekit-agents 1.8.8 basic_agent pattern**.

```
browser mic -> LiveKit -> Silero VAD -> Whisper STT (local, remote, or cloud)
    -> LLM (auto-detected local OpenAI-compatible server, or cloud with a key)
    -> Kokoro TTS (local, default) / Piper TTS (local) / OpenAI-compatible speech server (remote or cloud)
    -> LiveKit -> browser speakers, with live transcripts and tool-call log
```

- **Agent**: canonical `WorkerOptions(entrypoint_fnc=entrypoint)` + `AgentSession` + `Agent` with `@function_tool` built-ins, matching the livekit-agents 1.8.8 basic_agent example
- **STT**: embedded faster-whisper by default; **GGUF ASR** (`Qwen3-ASR 0.6B` on a local llama.cpp `llama-server` that PorchVoice starts and stops for you); or any whisper.cpp / faster-whisper / OpenAI-compatible `/audio/transcriptions` server via `STT_BASE_URL` + `STT_ENGINE=remote`
- **LLM**: any OpenAI-compatible chat API — auto-detects Ollama, LM Studio, llama.cpp, text-generation-webui on localhost, falls back to a cloud endpoint when a key is set; a compatibility mode exists for minimal servers that reject tool calls; provider errors (bad key, usage limits, unreachable) are surfaced as red messages in the chat panel
- **TTS**: embedded Kokoro-82M by default (Piper as the lightweight fallback); or any OpenAI-compatible `/audio/speech` server via `TTS_BASE_URL` + `TTS_ENGINE=remote`
- **LiveKit credentials**: shipped defaults (`devkey` / a fixed dev secret) work with zero setup — key and secret are applied to the token endpoint, the agent worker, and `livekit.yaml` automatically; set `LIVEKIT_API_KEY`/`LIVEKIT_API_SECRET` in `user_data/.env.local` only if you want your own, and the yaml follows
- **Crash protection**: the supervisor stops restarting a child that fails with a fatal config error, and gives up after repeated fast crashes instead of looping forever; fix the setting and press *Settings → Restart*
- **API keys**: encrypted at rest in `user_data/.env.local` (`enc:v1:...`), masked in the UI and API responses, with a show/hide toggle in Settings
- **MCP**: one `MCP_URL` (streamable HTTP or SSE, auto-selected), optional bearer token, tools attached straight into the agent session
- **Publish modes**: `local` (this machine), `wifi` (your LAN), `public` (cloudflared quick tunnel + `/lk` WebSocket proxy)
- **UI**: single-page voice client — live audio visualization, conversation panel with text chat, tool-call log, full settings, browsable chat history

---

## 1. Requirements

| What | Minimum | Notes |
|---|---|---|
| OS | Windows 10/11 64-bit, Linux (Ubuntu 22.04+, Debian 12+, Fedora, Arch), macOS 12+ | |
| Python | **3.11 or 3.12** | Windows: check *"Add python.exe to PATH"*. Linux: needs `python3-venv`/`python3-pip`. |
| RAM | 8 GB recommended | Whisper + the browser both use RAM. |
| Disk | 2–6 GB free | Whisper model (80 MB – 3 GB) + Piper voice (~60 MB) + packages. |
| GPU | Optional | NVIDIA speeds up Whisper via CUDA. CPU is fine for `tiny`/`base`/`small`. |
| An LLM endpoint | Anything that speaks the OpenAI chat API | Local (Ollama / LM Studio / llama.cpp / textgen webui) needs no key. Cloud needs a key. |
| Browser | Chrome, Edge, or Firefox | Microphone permission required. |
| Free ports | 8090, 7880, 7881, 50000–50100 | |
| Internet | Only for setup downloads, cloud providers, and public mode | Everything runs offline once set up. |

---

## 2. Setup

### Windows 11 / 10

1. Install Python 3.11 or 3.12 from [python.org](https://www.python.org/downloads/), ticking **"Add python.exe to PATH"**.
2. Double-click **`setup.bat`**. The wizard offers:
   - `1` Python venv + packages
   - `2` LiveKit server download
   - `3` Configuration — LLM URL/key (blank = auto-detect local first), MCP URL + bearer token, publish mode (local mode needs no publish setup)
   - `4` Whisper STT model pre-download + GPU test
   - `5` Voice engine — Kokoro (default, ~340 MB) or Piper voice
   - `6` cloudflared (only needed for `public` publish mode; skipped automatically for local)
   - `7` GGUF ASR — Qwen3-ASR 0.6B (~1 GB) + llama.cpp `llama-server` for GGUF speech-to-text
3. Double-click **`start.bat`** and open `http://127.0.0.1:8090`.

### Linux / macOS

```bash
./setup.sh      # same wizard, same menu
./start.sh      # one window starts LiveKit + agent + web UI
```

Then open `http://127.0.0.1:8090`, press play, and talk.

---

## 3. Publish modes

Set `PUBLISH_MODE` in `user_data/.env.local` (or the settings panel, then restart):

| Mode | Who can connect | What happens |
|---|---|---|
| `local` | This computer only | UI binds `127.0.0.1:8090`; LiveKit binds the loopback. Default and fully private. |
| `wifi` | Devices on your LAN | UI binds `0.0.0.0:8090`; the token endpoint hands LAN clients `ws://<lan-ip>:7880`. Allow the ports through your firewall. |
| `public` | Anyone with the link | Also starts a **cloudflared quick tunnel** (`https://<random>.trycloudflare.com`). Remote browsers load the UI over HTTPS and reach LiveKit signaling through this server's **`/lk` WebSocket proxy** (`wss://<tunnel>/lk/rtc -> ws://127.0.0.1:7880/rtc`). |

Notes on `public` mode:

- Quick tunnels are ephemeral: the URL changes on every start and is shown in the console, in `/health`, `/api/publish`, and the settings panel.
- WebRTC media does not ride HTTP tunnels. Far-away listeners therefore need UDP `50000-50100` forwarded on your router (the generated `livekit.yaml` already advertises the external IP), or a TURN server. On the same LAN, media flows directly.
- The tunnel runs only while PorchVoice runs; closing the window stops everything.

---

## 4. Providers and auto-detection

Every provider follows the same rule: **a `http://` base URL counts as local, `https://` counts as cloud, and local is always tried first.** API keys are optional everywhere; local servers never need one.

**LLM**

- `LLM_BASE_URL` empty → PorchVoice probes `http://127.0.0.1:11434/v1` (Ollama), `http://127.0.0.1:1234/v1` (LM Studio), `http://127.0.0.1:8080/v1` (llama.cpp), `http://127.0.0.1:5000/v1` (textgen) and uses the first that answers.
- If none answer and `LLM_API_KEY` or `OPENAI_API_KEY` is set → `https://api.openai.com/v1`.
- `LLM_COMPAT_MODE=1` swaps the tool-capable plugin for the minimal-payload adapter (for tiny local servers that choke on the `tools` parameter; MCP and built-in tools are disabled in that mode).

**STT**

- `STT_BASE_URL` empty (and `STT_ENGINE` not `remote`) → embedded faster-whisper (the model picked in setup).
- `STT_ENGINE=gguf` → speech-to-text runs **Qwen3-ASR-0.6B GGUF** on a local llama.cpp `llama-server`:
  - With `STT_GGUF_SERVER` empty, PorchVoice starts its own `llama-server` child (`bin/llama-server`, model files in `user_data/models/gguf-asr/`) on `STT_GGUF_PORT` (default 8900) and transcribes through `POST /v1/audio/transcriptions`.
  - Point `STT_GGUF_SERVER` (e.g. `http://127.0.0.1:8900/v1`) at any running llama-server carrying an audio-capable GGUF (`-m model.gguf --mmproj mmproj-*.gguf`) to skip the managed child.
  - Install the model + binary with `setup` menu option `7`; the RTX-class GPU path uses CUDA automatically when the downloaded build includes it.
- Set `STT_BASE_URL` to any server exposing `POST {url}/audio/transcriptions` (whisper.cpp server `http://...:9000/v1`, faster-whisper-server, Groq, OpenAI). Key optional.
- `STT_ENGINE=whisper` forces the embedded engine even when a URL is set; `STT_ENGINE=remote` forces the remote path.

**TTS**

- `TTS_ENGINE` = `kokoro` (default) / `piper` / `remote`.
- Kokoro: embedded Kokoro-82M ONNX engine, models in `user_data/models/kokoro/`, voice picked by `KOKORO_VOICE` (`af_heart` by default). If the model is missing, PorchVoice falls back to Piper, then to a cloud endpoint when a key is set.
- Piper: embedded Piper, voice picked by `TTS_VOICE` (the old default).
- `TTS_BASE_URL` set (or `TTS_ENGINE=remote`) → any server exposing `POST {url}/audio/speech` (openedai-speech, piper wrappers, OpenAI). Key optional.

**MCP**

- `MCP_URL` — a single MCP server URL. URLs ending in `/mcp` use streamable HTTP; anything else uses SSE (matching livekit-agents' auto-selection).
- `MCP_AUTH_TOKEN` — optional; sent as `Authorization: Bearer <token>` on every request.
- The agent attaches every tool the server exposes, announces them in the chat log, and reports each call (name, arguments, output) to the UI over a `tool-call` data topic.

---

## 5. Built-in agent tools

`python/agent.py` follows the basic_agent pattern: an `Assistant(Agent)` with instructions plus `@function_tool` methods. Two ship by default — `get_current_time` and `calculate` — and they are the template for adding your own. MCP tools join the same tool list automatically, so the LLM can interleave them freely.

---

## 6. Architecture

```
start.sh / start.bat
  └─ python/server.py            one-window supervisor + web UI/API (aiohttp)
       ├─ bin/livekit-server     child, auto-restart, yaml written per publish mode
       ├─ python/agent.py        child, livekit-agents 1.8.8 worker (start mode)
       │    ├─ AgentSession(stt=, llm=, tts=, vad=, tools=[MCPToolset])
       │    ├─ Assistant(Agent) with @function_tool built-ins
       │    ├─ providers: config auto-detect (local-first) -> remote/embedded engines
       │    └─ TranscriptHub: transcript + tool-call data topics, .jsonl chat log
       ├─ bin/llama-server       child, only when STT_ENGINE=gguf (Qwen3-ASR GGUF)
       ├─ cloudflared            child, only in public mode (quick tunnel)
       └─ web UI                 python/web/*, incl. /lk WS+HTTP proxy routes
```

Chat history lands in `user_data/chat/<timestamp>-<room>.jsonl` (one file per room, fsynced on every record). The settings panel writes `user_data/.env.local`; changes apply to the next conversation.

## 7. Configuration reference (user_data/.env.local)

| Key | Default | Meaning |
|---|---|---|
| `LIVEKIT_URL` | `ws://127.0.0.1:7880` | LiveKit server URL |
| `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` | `devkey` / built-in dev secret | Optional overrides; defaults are applied automatically everywhere (tokens, agent worker, `livekit.yaml`) |
| `PUBLISH_MODE` | `local` | `local` / `wifi` / `public` |
| `LLM_BASE_URL` | *(empty)* | OpenAI-compatible base URL; empty = auto-detect local first |
| `LLM_API_KEY` | *(empty)* | Optional; stored encrypted (`enc:v1:...`) in this file |
| `LLM_MODEL` | *(empty)* | Model id; empty = server default |
| `LLM_COMPAT_MODE` | `0` | `1` = minimal payloads, no tool calling |
| `STT_ENGINE` | *(empty)* | *(empty)* = auto, `whisper` = embedded, `gguf` = Qwen3-ASR GGUF via llama.cpp, `remote` = server |
| `STT_BASE_URL` / `STT_API_KEY` / `STT_MODEL` | *(empty)* | Remote STT server; empty = embedded Whisper |
| `STT_GGUF_SERVER` | *(empty)* | Empty = PorchVoice manages its own llama-server; or a full base URL |
| `STT_GGUF_PORT` / `STT_GGUF_MODEL` | `8900` / `Qwen3-ASR-0.6B-Q8_0.gguf` | Managed GGUF ASR server port / model file name in `user_data/models/gguf-asr/` |
| `TTS_ENGINE` | `kokoro` | `kokoro` / `piper` / `remote` |
| `KOKORO_VOICE` | `af_heart` | Embedded Kokoro voice |
| `TTS_BASE_URL` / `TTS_API_KEY` / `TTS_MODEL` | *(empty)* | Remote TTS server |
| `MCP_URL` | *(empty)* | Single MCP server URL (`/mcp` suffix = streamable HTTP) |
| `MCP_AUTH_TOKEN` | *(empty)* | Optional bearer token for MCP; stored encrypted |
| `MCP_TIMEOUT` | `5` | MCP request timeout in seconds |
| `WHISPER_*` | `small`… | Embedded Whisper engine settings |
| `TTS_VOICE` / `TTS_SPEED` | `en_US-amy-medium.onnx` / `1.1` | Embedded Piper engine settings |
| `VAD_*` / `ENDPOINTING_DELAY` / `ECHO_GUARD*` | see defaults | Turn-taking and echo protection |
| `UI_WINDOW` / `AUTO_OPEN` / `CLOSE_WITH_WINDOW` | `1` | Desktop app-window behavior |

## 8. Troubleshooting

- **`python-dotenv could not parse statement` warnings** — older versions wrote two header lines without `#`. PorchVoice now repairs `user_data/.env.local` automatically at startup (the junk lines get commented out).
- **`api_secret is required` / token 500 errors** — caused by empty `LIVEKIT_API_KEY`/`LIVEKIT_API_SECRET` lines in `user_data/.env.local`. Fixed: empty values no longer wipe the built-in defaults, the agent worker receives the credentials directly, and `livekit.yaml` is re-synced to your key/secret at startup.
- **Agent keeps restarting / stops retrying** — the supervisor no longer loops forever: a fatal config error stops the restarts immediately, and repeated fast crashes stop after a few attempts. Read the error, fix the setting in `user_data/.env.local` or the web UI, then press *Settings → Restart*.
- **GGUF STT does not answer** — check the startup banner (`STT:` line) and the `stt-gguf` child in the console. Missing model files or `llama-server` → run setup step 7; or set `STT_GGUF_SERVER` to a llama-server you run yourself. `Settings → check status` shows the resolved engine.
- **No reply from the bot** — the console prints the resolved LLM endpoint at startup (`LLM: ...`), and LLM failures now appear as red error bubbles in the conversation panel (bad key = 401, usage limit = 402/429, unreachable = connection). Press *check status* in Settings → Provider status to test every endpoint.
- **API key hidden** — keys are stored encrypted and the settings form shows `••••`; press *show* next to a key field to reveal it.
- **MCP tools not firing** — press *test connection* in settings; it lists the tools the server exposes. Remember `LLM_COMPAT_MODE=1` disables tool calling entirely.
- **Public mode connects but no audio remotely** — forward UDP 50000–50100 on your router, or run a TURN server; the quick tunnel carries signaling and the web UI only.
- **Whisper slow** — pick a smaller model in settings, or check the GPU line in the startup banner.
- **Kokoro voice missing** — run setup step 5 again, or switch `TTS_ENGINE=piper` in Settings → Speech (Piper runs automatically when the Kokoro model files are absent).
- **Port 8090 taken** — set `PUBLIC_PORT` in `user_data/.env.local`.

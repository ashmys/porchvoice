# PorchVoice

A local-first voice assistant for your own hardware. Press play in the app window and talk: speech recognition, the chat model, and the voice run on your machine by default, with cloud providers as an option — never a requirement.

```
browser mic ──► LiveKit ──► Silero VAD ──► STT ──► LLM ──► TTS ──► LiveKit ──► speakers
                            (turn-taking)  │         │         │
                        embedded Whisper   │   any OpenAI-   Kokoro-82M (default)
                        Qwen3-ASR 0.6B GGUF│   compatible    Piper / remote server
                        remote server      │   endpoint      (all optional)
```

- **Everything is optional and opt-in.** Only the LiveKit agent core is required; every STT/LLM/TTS engine and model is downloaded only if you choose it, and can be removed from the web UI or setup when you stop using it.
- **GGUF all the way.** STT (Qwen3-ASR 0.6B), the chat LLM (Qwen3 and friends), and TTS (Kokoro 82M) can all run fully offline through a llama.cpp `llama-server` that PorchVoice starts and stops for you.
- **One window.** `start.bat` / `start.sh` starts LiveKit, the agent, any managed model servers, and the UI; closing the window stops all of it.

---

## 1. Requirements

| What | Minimum | Notes |
|---|---|---|
| OS | Windows 10/11 64-bit, Linux (Ubuntu 22.04+, Debian 12+, Fedora, Arch), macOS 12+ | |
| Python | **3.11 or 3.12** | Windows: tick *"Add python.exe to PATH"*. Linux: needs `python3-venv`. |
| RAM / Disk | 8 GB RAM, 2–6 GB free disk | Depends on the engines and models you pick. |
| GPU | Optional | NVIDIA helps Whisper (CUDA) and GGUF models. CPU works for small models. |
| An LLM endpoint | Any OpenAI-compatible API — or a downloaded GGUF model | Local endpoints need no key. |
| Browser | Chrome, Edge, Brave, or Firefox | Mic permission required (auto-granted in the app window). |
| Free ports | 8090, 7880, 7881, 8900, 8899, 50000–50100 | 8900/8899 are used only when the GGUF engines run. |
| Internet | Only for setup downloads, cloud providers, public mode | Runs offline after setup. |

## 2. Quick start

**Windows:** install Python 3.11/3.12 → double-click `setup.bat` → double-click `start.bat` → the app window opens at `http://127.0.0.1:8090`, press play, talk.

**Linux / macOS:**

```bash
./setup.sh     # wizard, menu below
./start.sh     # LiveKit + agent + UI in one window
```

### Setup wizard menu

| Option | What it does | Downloads |
|---|---|---|
| `1` | Python venv + **core** packages | ~150 MB (livekit-agents, aiohttp, dotenv) |
| `2` | LiveKit server binary into `bin/` | ~30 MB |
| `3` | Configuration (LLM, MCP, publish mode). **Your existing settings are shown as the default answer and are kept unless you type something new** — re-running setup never overwrites your config. | — |
| `4` | Embedded Whisper model + GPU test (asks before downloading) | 80 MB – 3 GB |
| `5` | TTS voice: Kokoro (default) or Piper (asks) | ~340 MB / ~60 MB |
| `6` | cloudflared, only for `public` publish mode | ~20 MB |
| `7` | **GGUF ASR**: Qwen3-ASR 0.6B + llama.cpp `llama-server` (asks) | ~1 GB |
| `8` | **GGUF LLM**: pick and download a chat model (asks); auto-detects models already in the folder | 0.4 – 2.5 GB |
| `9` | Optional python modules — install only the engines you want (Kokoro / Whisper / Piper / MCP / CUDA libs) | varies |
| `10` | **Remove** unused modules / models / binaries to free disk space | frees space |
| `0` | Run all steps (each download still asks first) | |

Nothing beyond option `1`+`2` is mandatory. Skip the rest and add engines later from the wizard, the web UI, or — for GGUF models — simply drop any `.gguf` file into the model folder and it is auto-detected.

## 3. Engines: how to pick what runs

All engine settings live in the web UI (gear icon) and `user_data/.env.local`. Changes apply to the **next** conversation; *Settings → Restart LiveKit + agent* applies model-server changes immediately.

### LLM

| Setting | Behavior |
|---|---|
| `LLM_BASE_URL` set | Use that server (Ollama, LM Studio, llama.cpp, textgen, OpenRouter, OpenAI...). `http://` = local, `https://` = cloud. |
| empty + `LLM_ENGINE` empty (auto) | Probe Ollama `:11434/v1`, LM Studio `:1234/v1`, llama.cpp `:8080/v1`, textgen `:5000/v1`; if none answer and a GGUF chat model exists, use it; if a key is set, fall back to OpenAI. |
| `LLM_ENGINE=gguf` | **Managed GGUF LLM.** PorchVoice starts its own `llama-server` child on `LLM_GGUF_PORT` (8899) with the model from the models folder. No Ollama/LM Studio needed, fully offline. |
| `LLM_GGUF_SERVER` set | Use an external llama-server instead of the managed child. |

**GGUF LLM models**: stored in `LLM_GGUF_MODELS_DIR` (default `user_data/models/llm/`). Any `.gguf` you drop in is auto-detected (the dropdown in Settings lists it). The download picker in Settings (or setup menu 8) offers:

| Model | Size | Notes |
|---|---|---|
| Qwen3-0.6B Q8_0 | ~640 MB | default — best small quality |
| Qwen3-0.6B Q4_K_M | ~400 MB | smallest, fastest |
| Qwen3-1.7B Q4_K_M | ~1.1 GB | smarter |
| Qwen3-4B Q4_K_M | ~2.5 GB | much smarter; fits 6 GB GPUs |
| Qwen2.5-0.5B Q4_K_M | ~420 MB | compact fallback |

`LLM_COMPAT_MODE=1` sends minimal requests for servers that reject tool calling (disables tools). Downloads run in the background from the UI with live progress; nothing downloads unless you press the button.

### STT (speech-to-text)

| Setting | Behavior |
|---|---|
| `STT_ENGINE` empty | Remote server if `STT_BASE_URL` is set, else embedded Whisper (if installed). |
| `STT_ENGINE=gguf` | **Qwen3-ASR 0.6B GGUF** via the managed llama-server on port 8900 (external override: `STT_GGUF_SERVER`). Setup menu 7 installs model + binary. |
| `STT_ENGINE=whisper` | Embedded faster-whisper (`WHISPER_MODEL`, GPU auto). |
| `STT_ENGINE=remote` | Any whisper-compatible `POST {url}/audio/transcriptions` server. |

### TTS (text-to-speech)

| Setting | Behavior |
|---|---|
| `TTS_ENGINE=kokoro` (default) | Embedded Kokoro-82M ONNX, voice `KOKORO_VOICE` (`af_heart`). |
| `TTS_ENGINE=piper` | Embedded Piper, voice `TTS_VOICE`. |
| `TTS_ENGINE=remote` | Any OpenAI-compatible `POST {url}/audio/speech` server. |

## 4. Modules: add and remove without bloat

- **Installing**: setup menu `9` (or the interactive prompts of menu `1`) installs optional python packages only when you answer yes. Model files are always an explicit per-model choice.
- **Checking**: *Settings → Advanced → Scan installed modules* lists every optional component (python packages, model files, binaries), what uses it, its disk size, and whether it is currently selected by an engine.
- **Removing**: press *remove* next to an entry (web UI) or use setup menu `10`. Components actively selected by an engine are refused — switch the engine first (the UI says which). Removing a package or model never touches your settings in `.env.local`, so reinstalling restores the same behavior.

## 5. Publish modes

| Mode | Who can connect | Details |
|---|---|---|
| `local` (default) | this PC only | UI on `127.0.0.1:8090`, LiveKit on loopback. Private. |
| `wifi` | devices on your LAN | Binds `0.0.0.0`; token endpoint hands clients `ws://<lan-ip>:7880`. Open the firewall ports. |
| `public` | anyone with the link | Starts a cloudflared quick tunnel (URL changes each run, shown in console + settings). Signaling rides `wss://<tunnel>/lk`; media needs UDP 50000-50100 forwarded or a TURN server. |

**Microphones and http:** browsers only expose the mic on `localhost` or HTTPS. On this PC (`127.0.0.1:8090`) it always works; in `wifi` mode the desktop app window is launched with the LAN origin treated as secure, but **phones on plain http have no mic API — use `public` mode (https tunnel) for them.**

## 6. Microphone troubleshooting

The mic is the most common failure point, so PorchVoice diagnoses it for you:

- **Mic test** — *Settings → Speech → test microphone (5s)*: opens the exact device/constraints used by calls, shows the live input level, and names the device. Level > 2% means the mic works and a silent call is an STT/engine problem, not the mic.
- **Precheck before every call** — pressing play runs the same capture request first and translates every failure into plain language: permission denied (how to fix in the address bar + Windows privacy settings), device missing, mic held by another app (Discord/Zoom/OBS), or insecure context (wifi/phone case above).
- **App window** — when the app opens in its own browser profile (`UI_PROFILE=1`, the default), PorchVoice pre-grants microphone permission to the app origin, so there is no prompt to miss and no silent block after a past "deny".
- **Silence watchdog** — if the mic track is live but 6 seconds pass with zero signal, a toast reminds you to check the input device / OS privacy switch.

If the test passes but the assistant still doesn't react: check the STT line in the startup banner and press *check status* in Settings — the STT section shows the resolved engine and whether its server answers.

## 7. Configuration reference (`user_data/.env.local`)

API keys are stored encrypted (`enc:v1:...`) and masked in the UI. Empty values fall back to the defaults below. The web UI writes the same file.

| Key | Default | Meaning |
|---|---|---|
| `LIVEKIT_URL` | `ws://127.0.0.1:7880` | LiveKit server URL |
| `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` | `devkey` / built-in dev secret | Optional override; applied to tokens, agent worker, and `livekit.yaml` automatically |
| `PUBLISH_MODE` | `local` | `local` / `wifi` / `public` |
| `LLM_ENGINE` | *(empty)* | *(empty)* = auto, `gguf` = managed llama.cpp model, `api` = API servers only |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | *(empty)* | OpenAI-compatible endpoint; empty = auto-detect |
| `LLM_COMPAT_MODE` / `LLM_STREAMING` | `0` / `1` | Minimal payloads / token streaming |
| `LLM_THINKING` / `LLM_SPEAK_THINKING` | `0` / `0` | Show (and optionally speak) the model's thinking |
| `LLM_GGUF_SERVER` | *(empty)* | External llama-server URL; empty = managed child |
| `LLM_GGUF_PORT` | `8899` | Managed GGUF LLM port |
| `LLM_GGUF_MODEL` | *(empty)* | Model filename; empty = first `*.gguf` in the folder |
| `LLM_GGUF_MODELS_DIR` | *(empty)* | Custom model folder (auto-detected); empty = `user_data/models/llm` |
| `LLM_GGUF_NGL` / `LLM_GGUF_CTX` | `99` / `4096` | GPU layers / context size for the managed LLM |
| `STT_ENGINE` | *(empty)* | *(empty)* = auto, `whisper`, `gguf`, `remote` |
| `STT_GGUF_SERVER` / `STT_GGUF_PORT` / `STT_GGUF_MODEL` | *(empty)* / `8900` / `Qwen3-ASR-0.6B-Q8_0.gguf` | GGUF ASR server settings |
| `STT_BASE_URL` / `STT_API_KEY` / `STT_MODEL` | *(empty)* | Remote whisper-compatible server |
| `WHISPER_MODEL` / `WHISPER_DEVICE` / `WHISPER_COMPUTE` | `small` / `auto` / `auto` | Embedded Whisper settings |
| `WHISPER_LANGUAGE` | `en` | `auto` often misfires on short sentences — set it |
| `TTS_ENGINE` / `KOKORO_VOICE` / `TTS_VOICE` / `TTS_SPEED` | `kokoro` / `af_heart` / `en_US-amy-medium.onnx` / `1.1` | TTS engine settings |
| `MCP_URL` / `MCP_AUTH_TOKEN` / `MCP_TIMEOUT` | *(empty)* / *(empty)* / `5` | MCP tools: one URL, optional bearer |
| `VAD_MIN_SILENCE` / `VAD_ACTIVATION_THRESHOLD` / `ENDPOINTING_DELAY` | `0.55` / `0.45` / `0.7` | Turn-taking feel |
| `ECHO_GUARD` / `ECHO_GUARD_TAIL` | `1` / `0.8` | Drops the assistant's own voice from the mic |
| `CAP_ECHO_CANCEL` / `CAP_NOISE_SUPPRESS` / `CAP_AUTO_GAIN` | `1` | Browser capture processing (try Noise suppress Off if STT is inaccurate) |
| `UI_WINDOW` / `UI_PROFILE` / `UI_BROWSER` / `AUTO_OPEN` | `1` / `1` / *(empty)* / `1` | App window behavior |
| `CLOSE_WITH_WINDOW` | `1` | Closing the window stops everything |
| `PUBLIC_PORT` | `8090` | Web UI/API port |

## 8. Architecture

```
start.sh / start.bat
  └─ python/server.py            one-window supervisor + web UI/API (aiohttp)
       ├─ bin/livekit-server     child, auto-restart, yaml synced to your key/secret
       ├─ python/agent.py        child, livekit-agents 1.8.8 worker
       │    ├─ AgentSession(stt=, llm=, tts=, vad=, tools=[MCP...])
       │    ├─ Assistant(Agent) with @function_tool built-ins (time, calculate)
       │    └─ TranscriptHub: transcript + tool-call topics, .jsonl chat log
       ├─ bin/llama-server       child on :8900 when STT_ENGINE=gguf (Qwen3-ASR)
       ├─ bin/llama-server       child on :8899 when LLM_ENGINE=gguf (chat model)
       ├─ cloudflared            child, only in public mode
       └─ python/web/*           single-page UI, /lk WS+HTTP proxy for tunnels
```

**Where things live:** settings in `user_data/.env.local` · chat history in `user_data/chat/*.jsonl` · models in `user_data/models/{whisper,kokoro,piper,gguf-asr,llm}` · binaries in `bin/`. All of it is user data — safe to keep across updates.

**Web API** (everything the UI uses, usable from scripts): `/health`, `/token`, `/api/config` (GET/POST), `/api/check`, `/api/models/{stt,tts,llm}` (GET) + `/api/models/llm` (POST: `select`/`download`/`remove`/`set_dir`), `/api/modules` (GET) + `/api/modules/remove` (POST), `/api/llm/models`, `/api/browsers`, `/api/publish`, `/api/mcp/status`, `/api/restart`, `/api/chat/*`.

## 9. Troubleshooting

- **Setup warns `python-dotenv could not parse statement`** — old versions wrote header lines without `#`. PorchVoice repairs the file automatically at startup.
- **`api_secret is required` / token 500 / agent restart loop** — an empty `LIVEKIT_API_KEY`/`LIVEKIT_API_SECRET` line used to wipe the defaults. Empty values no longer override defaults, credentials are passed explicitly to the worker, and `livekit.yaml` is re-synced. A fatal config error stops the restart loop (so does a run of fast crashes); fix the setting and press *Settings → Restart*.
- **Mic seems dead** — run the Mic test (section 6). Permission denied → address-bar icon → Allow; held by another app → close it; phone over wifi http → use `public` mode.
- **No reply from the assistant** — LLM errors appear as red bubbles in the conversation panel (401 bad key, 402/429 quota/rate, unreachable). *Settings → check status* tests LiveKit, LLM, STT, TTS, and MCP in one click and shows the resolved endpoints.
- **GGUF engine does not answer** — check the `stt-gguf` / `llm-gguf` lines in the console: missing model files or `llama-server` binary say exactly which setup menu to run, or point `*_GGUF_SERVER` at a server you run yourself.
- **Downloaded model not detected** — it must sit in `LLM_GGUF_MODELS_DIR` (default `user_data/models/llm/`) and not start with `mmproj` (those are ASR projection files). The Detected models dropdown refreshes from disk.
- **STT inaccurate** — set `WHISPER_LANGUAGE` explicitly, try a bigger model, or turn Noise suppress / Auto gain Off in Settings → Speech. Try the GGUF ASR engine — Qwen3-ASR handles names and jargon differently than Whisper.
- **MCP tools not firing** — *test connection* in settings lists what the server exposes; `LLM_COMPAT_MODE=1` disables all tool calling.
- **Public mode: connects but no audio remotely** — forward UDP 50000–50100 or set up TURN; the tunnel carries only signaling and the UI.
- **Port already in use** — another PorchVoice window is open, or set `PUBLIC_PORT`. The GGUF ports are `STT_GGUF_PORT` / `LLM_GGUF_PORT`.
- **Freeing disk space** — *Settings → Advanced → Scan installed modules* shows sizes; remove what you don't use, reinstall anytime via setup menu 9 or re-downloading the model.

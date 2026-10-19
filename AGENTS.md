# AGENTS.md

Rules for humans and AI agents changing PorchVoice. Read fully before editing.

## Code style (enforced in review)

1. **No comments unless necessary.** Code must read on its own. Delete comments
   that narrate *what* the next line does, restate the function name, or mark
   sections. Keep a comment only when the *why* is genuinely non-obvious:
   protocol quirks, workarounds for upstream bugs, tricky math, or a decision a
   future reader would otherwise "fix" back into a bug. Docstrings follow the
   same rule: one short line when a public function's contract is not obvious;
   `Args:` blocks only when parameter meaning is not obvious.
2. **Naming: short whole words.** Identifiers use complete words kept short.
   `cfg`, `room`, `msg`, `token`, `voice`, `probe` are good. Single letters
   (`x`, `e2`), vowel-stripped abbreviations (`btn` in Python, `calc_ttl_sc`),
   and giant compounds (`theCurrentlySelectedProviderDescriptor`) are bad.
   `btn`/`cfg`/`sel` are accepted where they are already the house style and
   unambiguous in context. Env keys stay UPPER_SNAKE. JS keeps camelCase, CSS
   keeps lowercase-hyphen, Python keeps snake_case.
3. No commented-out code. Delete it; git remembers.
4. Prefer editing the existing module over adding a parallel one. New backend
   engine modules follow the `local_*.py` / `remote_*.py` pattern and expose
   `create_*` factories.

## Project layout

```
python/server.py       aiohttp supervisor + web API (routes in build_app)
python/agent.py        livekit-agents worker: AgentSession, TranscriptHub, tools
python/config.py       .env.local loading, provider resolution (local-first)
python/providers.py    build_llm / build_stt / build_tts factories
python/local_llm.py    compat LLM adapter (minimal servers)
python/local_stt.py    embedded faster-whisper
python/local_tts.py    embedded Piper
python/local_kokoro.py embedded Kokoro (default TTS engine)
python/remote_stt.py   OpenAI-compatible /audio/transcriptions client
python/remote_tts.py   OpenAI-compatible /audio/speech client
python/keyvault.py     at-rest encryption for API keys
python/mcp_client.py   MCP toolset + probe
python/setup_wizard.py setup.bat / setup.sh menu
python/web/            single-page UI (no build step, no framework)
tests/e2e_smoke.py     full smoke suite (config, providers, server, MCP, worker)
```

## Behaviors that must not regress

- Local-first: empty provider URL auto-detects local servers before cloud;
  `http://` = local, `https://` = cloud.
- Setup and the wizard never overwrite existing settings: current values in
  `user_data/.env.local` are kept and shown as the prompt defaults; unknown
  keys survive `write_env`.
- Model downloads are opt-in: no python package or model file is fetched
  without an explicit user choice (setup prompt or UI button). The module
  manager refuses to remove a component an engine currently selects.
- `user_data/.env.local` is the single settings store. API keys in it are
  encrypted (`enc:v1:...` via keyvault); plaintext reads stay transparent
  through `config.load_config`.
- Chat log lives in `user_data/chat/*.jsonl`, fsynced per record; the UI
  replays it through `/api/chat/session`.
- Secrets never go to the browser raw: `/api/config` returns `__saved__`
  sentinels; `/api/reveal` is the only plaintext path.
- Errors from LLM/STT/TTS providers must reach the chat panel (never die
  silently in logs only).

## Commands

```bash
./setup.sh                # or setup.bat: venv, LiveKit, config, models, voice
./start.sh                # or start.bat: run everything, open http://127.0.0.1:8090
python tests/e2e_smoke.py # full smoke suite, must pass before any release/patch
```

Run the smoke suite from the repo root with the project venv active. New
features need a matching section in `tests/e2e_smoke.py`; fixes need a
regression check there.

## Patch hygiene

- Keep diffs minimal: no drive-by reformatting, no dependency bumps unless the
  task needs them.
- The web UI has no bundler: keep JS ES6-in-browser compatible, no imports.
- Update `README.md` when config keys, endpoints, or setup steps change.

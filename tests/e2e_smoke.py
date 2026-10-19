import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import aiohttp
from aiohttp import web

RESULTS = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond), detail))
    print(("  PASS  " if cond else "  FAIL  ") + name + (" :: " + detail if detail and not cond else ""))
    return bool(cond)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _fake_llm_factory(hit: dict):
    def probe(url: str, key: str = "", timeout: float = 2.0) -> bool:
        hit[url] = True
        return url.endswith("11434/v1")
    return probe


def section_config() -> None:
    print("[1] config auto-detect")
    import config

    check("classify http->local", config.classify_endpoint("http://x/v1") == "local")
    check("classify https->cloud", config.classify_endpoint("https://x/v1") == "cloud")

    cfg = {"STT_ENGINE": "gguf"}
    sel = config.resolve_stt(cfg)
    check("stt gguf auto url", sel["engine"] == "gguf"
          and sel["base_url"] == "http://127.0.0.1:8900/v1", str(sel))
    cfg = {"STT_ENGINE": "gguf", "STT_GGUF_SERVER": "http://127.0.0.1:9999/v1"}
    sel = config.resolve_stt(cfg)
    check("stt gguf explicit server", sel["base_url"].endswith(":9999/v1"), str(sel))

    cfg = {"LLM_BASE_URL": "https://api.groq.com/openai/v1", "LLM_API_KEY": "k"}
    sel = config.resolve_llm(cfg)
    check("llm explicit cloud", sel["kind"] == "cloud" and sel["base_url"].startswith("https://"))

    cfg = {"LLM_BASE_URL": "http://127.0.0.1:9999/v1", "LLM_API_KEY": ""}
    sel = config.resolve_llm(cfg)
    check("llm explicit local", sel["kind"] == "local")

    cfg = {"LLM_BASE_URL": "", "LLM_API_KEY": "", "OPENAI_API_KEY": "sk-test"}
    os.environ["OPENAI_API_KEY"] = "sk-test"
    sel = config.resolve_llm(cfg, probe=lambda u, key="", timeout=2.0: False)
    check("llm cloud fallback on key", sel["kind"] == "cloud" and "openai.com" in sel["base_url"])

    cfg = {"LLM_BASE_URL": "", "LLM_API_KEY": "", "OPENAI_API_KEY": ""}
    os.environ.pop("OPENAI_API_KEY", None)
    sel = config.resolve_llm(cfg, probe=lambda u, key="", timeout=2.0: False)
    check("llm none without key", sel["kind"] == "none")

    hit = {}
    cfg = {"LLM_BASE_URL": "", "LLM_API_KEY": ""}
    sel = config.resolve_llm(cfg, probe=_fake_llm_factory(hit))
    check("llm local-first probe", sel["kind"] == "local"
          and sel["base_url"].endswith("11434/v1"), str(sel))
    check("llm probe stops at first hit", set(hit) == {"http://127.0.0.1:11434/v1"}, str(hit))

    cfg = {"TEXTGEN_BASE_URL": "http://127.0.0.1:5000/v1",
           "TEXTGEN_API_KEY": "sk-1234", "TEXTGEN_MODEL": "m"}
    cfg = config.load_config.__wrapped__(cfg) if hasattr(config.load_config, "__wrapped__") else cfg
    merged = dict(config.CONFIG_DEFAULTS)
    merged.update(cfg)
    for old, new in config.TEXTGEN_ALIASES.items():
        if merged.get(old) and not merged.get(new):
            merged[new] = merged[old]
    check("legacy TEXTGEN alias", merged["LLM_BASE_URL"] == "http://127.0.0.1:5000/v1"
          and merged["LLM_API_KEY"] == "sk-1234")

    cfg = {"STT_BASE_URL": "http://127.0.0.1:9000/v1"}
    sel = config.resolve_stt(cfg)
    check("stt remote local", sel["kind"] == "local" and sel["base_url"].endswith("/v1"))
    cfg = {"STT_BASE_URL": ""}
    sel = config.resolve_stt(cfg)
    check("stt embedded default", sel["source"] == "embedded")
    cfg = {"TTS_BASE_URL": "https://api.openai.com/v1", "TTS_API_KEY": "k"}
    sel = config.resolve_tts(cfg)
    check("tts remote cloud", sel["kind"] == "cloud")
    cfg = {"TTS_BASE_URL": "", "TTS_VOICE": "missing.onnx", "TTS_API_KEY": ""}
    sel = config.resolve_tts(cfg)
    check("tts embedded default", sel["source"] == "embedded")
    cfg = {"MCP_URL": "http://127.0.0.1:9801/mcp", "MCP_AUTH_TOKEN": "t"}
    sel = config.resolve_mcp(cfg)
    check("mcp enabled", sel["enabled"] and sel["kind"] == "local")
    cfg = {"MCP_URL": ""}
    check("mcp disabled default", not config.resolve_mcp(cfg)["enabled"])

    for mode, host in (("local", "127.0.0.1"), ("wifi", "0.0.0.0"), ("public", "0.0.0.0")):
        cfg = {"PUBLISH_MODE": mode}
        info = config.resolve_publish(cfg)
        check("publish " + mode, info["http_host"] == host, str(info))


def section_providers() -> None:
    print("[2] provider factories")
    import providers

    llm = providers.build_llm({"LLM_BASE_URL": "http://127.0.0.1:1234/v1",
                               "LLM_API_KEY": "", "LLM_MODEL": "test-model",
                               "LLM_COMPAT_MODE": "0"})
    check("openai-plugin LLM builds (local, no key)", llm is not None)

    llm = providers.build_llm({"LLM_BASE_URL": "http://127.0.0.1:1234/v1",
                               "LLM_API_KEY": "x", "LLM_MODEL": "m",
                               "LLM_COMPAT_MODE": "1"})
    check("compat adapter builds", type(llm).__name__ == "TextgenLLM")

    stt = providers.build_stt({"STT_BASE_URL": "http://127.0.0.1:9000/v1",
                               "STT_MODEL": "whisper-1",
                               "WHISPER_LANGUAGE": "en"})
    check("remote STT builds", type(stt).__name__ == "RemoteSTT")

    tts = providers.build_tts({"TTS_BASE_URL": "https://api.openai.com/v1",
                               "TTS_API_KEY": "k", "TTS_MODEL": "tts-1",
                               "TTS_VOICE": "alloy", "TTS_SPEED": "1.0"})
    check("remote TTS builds", type(tts).__name__ == "RemoteTTS")


def section_env_file() -> None:
    print("[1d] env file: empty values keep defaults, junk lines repaired")
    import config
    import server as srv
    import tempfile

    old_env = config.ENV_FILE
    old_srv_env = srv.ENV_FILE
    tmp = Path(tempfile.mkdtemp()) / ".env.local"
    try:
        tmp.write_text(
            "PorchVoice settings\n"
            "Applies to the NEXT conversation.\n"
            "LIVEKIT_API_SECRET=\n"
            "LLM_BASE_URL=http://127.0.0.1:1234/v1\n",
            encoding="utf-8")
        config.ENV_FILE = tmp
        srv.ENV_FILE = tmp
        cfg = config.load_config()
        check("empty LIVEKIT_API_SECRET keeps default",
              cfg.get("LIVEKIT_API_SECRET") == config.CONFIG_DEFAULTS["LIVEKIT_API_SECRET"],
              str(cfg.get("LIVEKIT_API_SECRET")))
        check("non-empty value still applies",
              cfg.get("LLM_BASE_URL") == "http://127.0.0.1:1234/v1")
        srv.repair_env_file()
        repaired = tmp.read_text(encoding="utf-8")
        check("junk header lines commented out",
              repaired.startswith("# PorchVoice settings\n# Applies"), repaired[:80])
        check("KEY=value lines untouched", "LIVEKIT_API_SECRET=\n" in repaired)
    finally:
        config.ENV_FILE = old_env
        srv.ENV_FILE = old_srv_env
        tmp.unlink(missing_ok=True)
        tmp.parent.rmdir()


def section_keyvault() -> None:
    print("[1b] keyvault at-rest encryption")
    import keyvault

    plain = "sk-or-v1-abc123secret"
    enc = keyvault.encrypt_value(plain)
    check("encrypt wraps with prefix", enc.startswith("enc:v1:") and plain not in enc)
    check("decrypt roundtrip", keyvault.decrypt_value(enc) == plain)
    check("plaintext passthrough", keyvault.decrypt_value(plain) == plain)
    check("empty passthrough", keyvault.encrypt_value("") == "")
    tampered = enc[:-4] + ("AAAA" if enc[-4:] != "AAAA" else "BBBB")
    check("tamper detected", keyvault.decrypt_value(tampered) == "")
    check("secret keys list", "LLM_API_KEY" in keyvault.SECRET_KEYS)


def friendly_llm_error_cases() -> None:
    print("[1c] friendly LLM error mapping")
    import agent

    check("401 -> key hint", "API key" in agent.friendly_llm_error("Error code: 401 - Invalid API key"))
    check("402 -> usage limit hint", "usage limit" in agent.friendly_llm_error("HTTP 402 Insufficient Credits"))
    check("429 -> rate limit hint", "rate limit" in agent.friendly_llm_error("Rate limit exceeded: free-models-per-day"))
    check("conn refused -> reachability hint",
          "Cannot reach" in agent.friendly_llm_error("Connection refused by provider"))
    check("unknown passthrough", "weird failure" in agent.friendly_llm_error("weird failure"))


async def _boot_app(mode: str, port: int, tunnel_host: str = "") -> web.AppRunner:
    import server as srv

    os.environ["PUBLISH_MODE"] = mode
    os.environ["LIVEKIT_URL"] = "ws://127.0.0.1:" + str(LK_PORT)
    if tunnel_host:
        srv.TUNNEL["url"] = "https://" + tunnel_host
    else:
        srv.TUNNEL["url"] = ""
    app = srv.build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    return runner


async def section_server() -> None:
    print("[3] publish modes + /lk proxy")
    runner = await _boot_app("local", free_port())
    base = "http://127.0.0.1:" + str(runner.addresses[0][1])
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(base + "/health") as r:
                h = await r.json()
                check("health 200", r.status == 200)
                check("health has llm/publish/mcp",
                      "llm" in h and "publish" in h and "mcp" in h)
            async with s.get(base + "/token?identity=t1") as r:
                d = await r.json()
                check("token local mode url", d["url"] == "ws://127.0.0.1:" + str(LK_PORT), d["url"])
            async with s.get(base + "/api/publish") as r:
                d = await r.json()
                check("publish endpoint local", d["mode"] == "local")
    finally:
        await runner.cleanup()

    runner = await _boot_app("wifi", free_port())
    base = "http://127.0.0.1:" + str(runner.addresses[0][1])
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(base + "/token?identity=t2") as r:
                d = await r.json()
                import config
                check("token wifi mode url", d["url"] == "ws://" + config.detect_lan_ip() + ":7880", d["url"])
    finally:
        await runner.cleanup()

    tunnel_host = "random-name.trycloudflare.com"
    runner = await _boot_app("public", free_port(), tunnel_host)
    base = "http://127.0.0.1:" + str(runner.addresses[0][1])
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(base + "/token?identity=t3",
                             headers={"Host": tunnel_host}) as r:
                d = await r.json()
                check("token via tunnel -> wss /lk",
                      d["url"] == "wss://" + tunnel_host + "/lk", d["url"])
            async with s.get(base + "/token?identity=t4") as r:
                d = await r.json()
                import config
                check("token public direct -> lan url",
                      d["url"] == "ws://" + config.detect_lan_ip() + ":7880", d["url"])
    finally:
        await runner.cleanup()

    up_runner = await _fake_livekit_upstream()
    main_runner = await _boot_app("local", free_port())
    base = "http://127.0.0.1:" + str(main_runner.addresses[0][1])
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(base + "/lk/rtc/validate?access_token=t") as r:
                body = await r.text()
                check("/lk HTTP proxy hits upstream", r.status == 200 and "validate-ok" in body, body)
            async with s.ws_connect(base + "/lk/rtc?access_token=t") as ws:
                await ws.send_str("ping-through-proxy")
                msg = await asyncio.wait_for(ws.receive(), timeout=5)
                check("/lk WS proxy echo", msg.type == aiohttp.WSMsgType.TEXT
                      and msg.data == "echo:ping-through-proxy", str(msg.data))
    finally:
        await main_runner.cleanup()
        await up_runner.cleanup()
    os.environ.pop("PUBLISH_MODE", None)


async def _fake_livekit_upstream() -> web.AppRunner:
    from aiohttp import WSMsgType

    async def validate(request):
        return web.json_response({"validate-ok": True})

    async def rtc_ws(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                await ws.send_str("echo:" + msg.data)
            elif msg.type == WSMsgType.ERROR:
                break
        return ws

    app = web.Application()
    app.router.add_get("/rtc/validate", validate)
    app.router.add_get("/rtc", rtc_ws)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", LK_PORT)
    await site.start()
    return runner


async def section_config_api() -> None:
    print("[3b] config API: secrets encrypted at rest, masked responses")
    import config
    import server as srv
    import keyvault

    backup = None
    if srv.ENV_FILE.exists():
        backup = srv.ENV_FILE.read_bytes()
    try:
        runner = await _boot_app("local", free_port())
        base = "http://127.0.0.1:" + str(runner.addresses[0][1])
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(base + "/api/config", json={
                        "config": {"LLM_API_KEY": "sk-or-v1-plain-key-xyz",
                                   "LLM_BASE_URL": "https://openrouter.ai/api/v1"}}) as r:
                    d = await r.json()
                    check("config post ok", r.status == 200 and d.get("ok"), str(d))
                    check("config post masks key in response",
                          d["config"]["LLM_API_KEY"] == srv.SECRET_SENTINEL, str(d["config"]["LLM_API_KEY"]))
            raw = srv.ENV_FILE.read_text(encoding="utf-8")
            check("key encrypted at rest", "enc:v1:" in raw and "sk-or-v1-plain-key-xyz" not in raw)
            async with aiohttp.ClientSession() as s:
                async with s.get(base + "/api/config") as r:
                    d = await r.json()
                    check("config get masks key",
                          d["config"]["LLM_API_KEY"] == srv.SECRET_SENTINEL)
                async with s.post(base + "/api/reveal", json={"key": "LLM_API_KEY"}) as r:
                    d = await r.json()
                    check("reveal returns plaintext", d.get("value") == "sk-or-v1-plain-key-xyz", str(d))
                async with s.post(base + "/api/reveal", json={"key": "NOT_A_SECRET"}) as r:
                    check("reveal rejects unknown key", r.status == 400)
                async with s.post(base + "/api/config", json={
                        "config": {"LLM_API_KEY": srv.SECRET_SENTINEL,
                                   "LLM_BASE_URL": "https://openrouter.ai/api/v1"}}) as r:
                    check("sentinel keeps stored key", r.status == 200)
            stored = ""
            for line in srv.ENV_FILE.read_text(encoding="utf-8").splitlines():
                if line.startswith("LLM_API_KEY="):
                    stored = line.partition("=")[2]
            check("sentinel did not clobber key",
                  keyvault.decrypt_value(stored) == "sk-or-v1-plain-key-xyz")
            raw_env = srv.ENV_FILE.read_text(encoding="utf-8")
            check("env header lines are comments",
                  raw_env.startswith("# "), raw_env[:40])
            async with aiohttp.ClientSession() as s:
                async with s.post(base + "/api/config", json={
                        "config": {"LIVEKIT_API_SECRET": "",
                                   "LIVEKIT_API_KEY": "",
                                   "STT_ENGINE": "gguf"}}) as r:
                    check("empty default-backed keys skipped",
                          r.status == 200, str(r.status))
            raw_env = srv.ENV_FILE.read_text(encoding="utf-8")
            empty_lk = [l for l in raw_env.splitlines()
                        if l.startswith(("LIVEKIT_API_SECRET=", "LIVEKIT_API_KEY="))]
            check("no empty LIVEKIT key/secret lines", not empty_lk, str(empty_lk))
            check("STT_ENGINE=gguf stored", "STT_ENGINE=gguf" in raw_env)
            check("config post keeps defaults effective",
                  config.load_config().get("LIVEKIT_API_SECRET")
                  == config.CONFIG_DEFAULTS["LIVEKIT_API_SECRET"])
            cfg = config.load_config()
            check("load_config decrypts transparently", cfg.get("LLM_API_KEY") == "sk-or-v1-plain-key-xyz")
            async with aiohttp.ClientSession() as s:
                async with s.get(base + "/api/check") as r:
                    d = await r.json()
                    check("api/check has stt/tts sections",
                          "stt" in d and "tts" in d, str(list(d)))
                    check("api/check llm status string", "llm" in d and isinstance(d["llm"]["status"], str))
                    check("api/check tts engine detected", d["tts"].get("engine") in ("kokoro", "piper"), str(d["tts"]))
        finally:
            await runner.cleanup()
    finally:
        if backup is not None:
            srv.ENV_FILE.write_bytes(backup)
        elif srv.ENV_FILE.exists():
            srv.ENV_FILE.unlink()


def section_worker_boot() -> None:
    print("[4] agent worker boot (dry-run against a dead LiveKit port)")
    py = str(ROOT / ".venv" / "bin" / "python")
    env = dict(os.environ)
    env["LIVEKIT_URL"] = "ws://127.0.0.1:59999"
    env["LIVEKIT_API_KEY"] = "devkey"
    env["LIVEKIT_API_SECRET"] = "porchvoice-secret-1234567890abcdef"
    env["LLM_BASE_URL"] = "http://127.0.0.1:1234/v1"
    env["LOG_LEVEL"] = "ERROR"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PUBLISH_MODE"] = "local"
    proc = subprocess.Popen(
        [py, "-u", str(ROOT / "python" / "agent.py"), "start"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, env=env, cwd=str(ROOT))
    time.sleep(8.0)
    alive = proc.poll() is None
    check("worker process stays alive", alive)
    proc.terminate()
    try:
        out, _ = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
    check("worker ran preflight", "PREFLIGHT" in out, out[-400:])
    check("worker preflight resolves LiveKit credentials",
          "LiveKit API key/secret missing" not in out, out[-400:])
    check("no import crash in worker", "Traceback" not in out
          or "Connection" in out or "connection" in out, out[-400:])


def _wait_http(url: str, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                return True
        except urllib.error.HTTPError:
            return True
        except Exception:
            time.sleep(0.3)
    return False


def _wait_port(port: int, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.3)
    return False


async def section_mcp_live() -> None:
    print("[5] LIVE MCP test (real server, streamable HTTP + bearer auth)")
    from mcp_client import call_mcp_tool, probe_mcp

    port_open = free_port()
    port_auth = free_port()
    py = str(ROOT / ".venv" / "bin" / "python")
    p1 = subprocess.Popen(
        [py, str(ROOT / "tests" / "mcp_test_server.py"),
         "--port", str(port_open)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    p2 = subprocess.Popen(
        [py, str(ROOT / "tests" / "mcp_test_server.py"),
         "--port", str(port_auth), "--token", "secret-token-123"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ok1 = _wait_port(port_open)
        ok2 = _wait_port(port_auth)
        check("mcp servers up", ok1 and ok2)
        await asyncio.sleep(1.0)

        r = await probe_mcp("http://127.0.0.1:" + str(port_open) + "/mcp")
        check("mcp list_tools", r["ok"] and set(["add", "echo", "room_greeting"])
              <= set(r["tools"]), str(r))

        r = await call_mcp_tool(
            "http://127.0.0.1:" + str(port_open) + "/mcp",
            "add", {"a": 2, "b": 3})
        check("mcp call_tool add(2,3)=5", r["ok"] and r["texts"] and "5" in r["texts"][0], str(r))

        r = await probe_mcp("http://127.0.0.1:" + str(port_auth) + "/mcp")
        check("mcp rejects missing bearer", not r["ok"], str(r))

        r = await probe_mcp("http://127.0.0.1:" + str(port_auth) + "/mcp",
                            token="secret-token-123")
        check("mcp accepts correct bearer", r["ok"], str(r))

        r = await call_mcp_tool(
            "http://127.0.0.1:" + str(port_auth) + "/mcp",
            "echo", {"text": "porchvoice"}, token="secret-token-123")
        check("mcp call_tool echo with bearer",
              r["ok"] and r["texts"] and r["texts"][0] == "porchvoice", str(r))
    finally:
        p1.terminate()
        p2.terminate()


def section_llm_gguf() -> None:
    print("[1e] LLM GGUF routing (engine select, custom dir, auto-detect)")
    import config

    cfg = dict(config.CONFIG_DEFAULTS)
    cfg.update({"LLM_ENGINE": "gguf"})
    sel = config.resolve_llm(cfg)
    check("llm engine gguf managed url",
          sel["base_url"] == "http://127.0.0.1:8899/v1" and sel["engine"] == "gguf",
          str(sel))
    cfg.update({"LLM_ENGINE": "gguf", "LLM_GGUF_SERVER": "http://127.0.0.1:8080/v1"})
    sel = config.resolve_llm(cfg)
    check("llm gguf external server", sel["base_url"].endswith(":8080/v1")
          and sel["source"] == "config", str(sel))

    import tempfile
    tmpdir = Path(tempfile.mkdtemp())
    try:
        cfg = dict(config.CONFIG_DEFAULTS)
        cfg.update({"LLM_ENGINE": "", "LLM_GGUF_MODELS_DIR": str(tmpdir),
                    "LLM_GGUF_MODEL": ""})
        check("no model -> no gguf autodetect",
              config.llm_gguf_model_path(cfg) is None)
        mmproj = tmpdir / "mmproj-x.gguf"
        mmproj.write_bytes(b"x")
        check("mmproj files are skipped",
              config.llm_gguf_model_path(cfg) is None)
        model = tmpdir / "My-Chat-Q4_K_M.gguf"
        model.write_bytes(b"y")
        check("auto-detect picks first chat gguf",
              config.llm_gguf_model_path(cfg) == model)
        cfg.update({"LLM_GGUF_MODEL": "does-not-exist.gguf"})
        check("named-but-missing model -> None",
              config.llm_gguf_model_path(cfg) is None)
        cfg.update({"LLM_GGUF_MODEL": "My-Chat-Q4_K_M.gguf"})
        check("named model found", config.llm_gguf_model_path(cfg) == model)
        sel = config.resolve_llm(cfg, probe=lambda u, key="", timeout=2.0: False)
        check("auto resolve skips gguf when server not up",
              sel["kind"] == "none", str(sel))
        sel = config.resolve_llm(cfg, probe=lambda u, key="", timeout=2.0: "8899" in u)
        check("auto resolve uses managed gguf when up",
              sel["engine"] == "gguf" and sel["source"] == "auto:managed-gguf",
              str(sel))
        sel = config.resolve_llm(dict(cfg, LLM_BASE_URL="http://127.0.0.1:1234/v1"),
                                 probe=lambda u, key="", timeout=2.0: False)
        check("explicit base_url wins over gguf",
              sel["source"] == "config" and sel["base_url"].endswith(":1234/v1"),
              str(sel))
    finally:
        mmproj.unlink(missing_ok=True)
        model.unlink(missing_ok=True)
        tmpdir.rmdir()


def section_setup_preserve() -> None:
    print("[1f] setup wizard: env preservation + current values as defaults")
    import tempfile

    import setup_wizard as sw

    old_env = sw.ENV_FILE
    old_dry = sw.DRY_RUN
    tmp = Path(tempfile.mkdtemp()) / ".env.local"
    try:
        tmp.write_text(
            "LLM_BASE_URL=http://old:1234/v1\n"
            "UI_BROWSER=brave\n"
            "WHISPER_BEAM=3\n",
            encoding="utf-8")
        sw.ENV_FILE = tmp
        sw.DRY_RUN = False
        existing = sw.read_env()
        check("read_env picks up existing values",
              existing.get("LLM_BASE_URL") == "http://old:1234/v1"
              and existing.get("UI_BROWSER") == "brave")
        sw.write_env({"PUBLISH_MODE": "wifi"})
        out = tmp.read_text(encoding="utf-8")
        check("write_env keeps existing LLM_BASE_URL",
              "LLM_BASE_URL=http://old:1234/v1" in out)
        check("write_env keeps unknown keys (UI_BROWSER, WHISPER_BEAM)",
              "UI_BROWSER=brave" in out and "WHISPER_BEAM=3" in out)
        check("write_env applies the update", "PUBLISH_MODE=wifi" in out)
        check("write_env writes commented headers", out.startswith("# PorchVoice"))
        sw.DRY_RUN = True
        sw.write_env({"LLM_BASE_URL": "http://new:1/v1"})
        check("dry-run leaves file untouched",
              "LLM_BASE_URL=http://old:1234/v1" in tmp.read_text(encoding="utf-8"))
    finally:
        sw.ENV_FILE = old_env
        sw.DRY_RUN = old_dry
        tmp.unlink(missing_ok=True)
        tmp.parent.rmdir()


async def section_transcript_stream() -> None:
    print("[1g] transcript cleanup + chat history + streaming display")
    import tempfile

    from remote_stt import clean_transcript

    cases = [
        ("<|im_start|>user<|im_end|><|im_start|>assistant\n hello there "
         "<|im_end|>", "hello there"),
        ("<|im_start|>user\n what time is it<|im_end|>"
         "<|im_start|>assistant\n", "what time is it"),
        ("hello there", "hello there"),
        ("<|audio_start|>hello world<|audio_end|>", "hello world"),
        ("user: what is the weather", "what is the weather"),
        ('"quoted speech"', "quoted speech"),
        ("assistant manager needed", "assistant manager needed"),
        ("<|im_start|>assistant\n The assistant manager spoke <|im_end|>",
         "The assistant manager spoke"),
    ]
    for raw, want in cases:
        got = clean_transcript(raw)
        check("qwen format stripped: " + raw[:34].replace("\n", "\\n"),
              got == want, f"got {got!r} want {want!r}")

    from sup import chatstore, paths as sup_paths

    tmp = Path(tempfile.mkdtemp())
    try:
        real_chat_dir = sup_paths.CHAT_DIR
        sup_paths.CHAT_DIR = tmp
        old_f = tmp / "20260101-000000-testroom.jsonl"
        old_f.write_text(json.dumps({"who": "user", "text": "older chat",
                                     "ts": "t", "id": "o1"}) + "\n",
                         encoding="utf-8")
        os.utime(old_f, (1_000_000_000, 1_000_000_000))
        cur_f = tmp / "20260202-000000-testroom.jsonl"
        cur_f.write_text(json.dumps({"who": "user", "text": "current q",
                                     "ts": "t", "id": "c1"}) + "\n"
                         + json.dumps({"who": "bot", "text": "current a",
                                       "ts": "t", "id": "c2"}) + "\n",
                         encoding="utf-8")
        cleared = chatstore.clear_history("testroom")
        check("clear empties only the current session",
              cleared == [cur_f.name] and cur_f.read_text() == ""
              and "older chat" in old_f.read_text(), str(cleared))
        foreign = tmp / "evil-notes.jsonl"
        foreign.write_text("keep me", encoding="utf-8")
        os.utime(foreign, (1_000_000_000, 1_000_000_000))
        chatstore.clear_history("testroom", wipe_all=True)
        check("wipe_all clears sessions but skips foreign names",
              foreign.read_text() == "keep me", foreign.read_text())
        old_f.write_text(json.dumps({"who": "user", "text": "older chat",
                                     "ts": "t", "id": "o1"}) + "\n",
                         encoding="utf-8")
        cur_f.write_text("{}", encoding="utf-8")
        os.utime(old_f, (1_000_000_000, 1_000_000_000))
        sessions = chatstore.list_sessions()
        check("list_sessions reads turns",
              any(s["file"] == old_f.name and s["turns"] == 1
                  and s["first"] == "older chat" for s in sessions), str(sessions))
    finally:
        sup_paths.CHAT_DIR = real_chat_dir
        import shutil as _sh2
        _sh2.rmtree(tmp, ignore_errors=True)

    import agent as agent_mod

    class _FakePub:
        def __init__(self):
            self.sent = []

        async def publish_data(self, data, reliable=False, topic=""):
            self.sent.append((topic, json.loads(data)))

    class _FakeRoom:
        def __init__(self):
            self.local_participant = _FakePub()

    async def _hub_checks():
        room = _FakeRoom()
        hub = agent_mod.TranscriptHub(room)
        hub.PARTIAL_MIN_GAP = 0
        await asyncio.sleep(0)
        hub.bot("hel", "m1", final=False)
        hub.bot("hello wor", "m1", final=False)
        await asyncio.sleep(0.01)
        partials = [p for t, p in room.local_participant.sent
                    if t == "transcript" and not p.get("final")]
        check("bot text streams as partials",
              len(partials) >= 2 and partials[-1]["text"] == "hello wor",
              str(partials))
        check("partial adopts the streaming bubble id",
              hub.adopt_pending("x") == "m1", hub.adopt_pending("x"))
        hub.bot("hello world", "m1", final=True, think="")
        await asyncio.sleep(0.01)
        finals = [p for t, p in room.local_participant.sent
                  if t == "transcript" and p.get("final")]
        check("final publishes once under same id",
              len(finals) == 1 and finals[0]["text"] == "hello world"
              and finals[0]["id"] == "m1", str(finals))
        hub.bot("hello world", "m2", final=True)
        await asyncio.sleep(0.01)
        finals = [p for t, p in room.local_participant.sent
                  if t == "transcript" and p.get("final")]
        check("duplicate final is suppressed (one bubble)",
              len(finals) == 1, str(finals))
        hub.user("hi there")
        await asyncio.sleep(0.01)
        users = [p for t, p in room.local_participant.sent
                 if t == "transcript" and p.get("who") == "user"]
        check("user text still publishes instantly",
              len(users) == 1 and users[0]["final"] is True, str(users))

    await _hub_checks()


async def section_modules_api() -> None:
    print("[3c] modules + models/llm API (list, select, remove, in-use guard)")
    import tempfile

    import config
    import server as srv

    old_dir = config.ROOT
    old_env = srv.ENV_FILE
    old_cfg_env = config.ENV_FILE
    tmp = Path(tempfile.mkdtemp())
    try:
        modeldir = tmp / "llm"
        modeldir.mkdir()
        envp = tmp / ".env.local"
        envp.write_text("STT_ENGINE=whisper\n"
                        "LLM_GGUF_MODELS_DIR=" + str(modeldir) + "\n",
                        encoding="utf-8")
        srv.ENV_FILE = envp
        config.ENV_FILE = envp
        m1 = modeldir / "Test-Model-Q8_0.gguf"
        m1.write_bytes(b"z" * 2048)

        runner = await _boot_app("local", free_port())
        base = "http://127.0.0.1:" + str(runner.addresses[0][1])
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(base + "/api/models/llm") as r:
                    d = await r.json()
                    check("models/llm lists detected model",
                          any(m["name"] == m1.name for m in d["models"]), str(d["models"]))
                    check("models/llm has presets catalog",
                          len(d["presets"]) >= 3 and all("repo" in p for p in d["presets"]))
                async with s.post(base + "/api/models/llm", json={
                        "action": "select", "name": m1.name}) as r:
                    d = await r.json()
                    check("select model ok", r.status == 200 and d.get("ok"), str(d))
                check("select wrote LLM_ENGINE=gguf",
                      "LLM_ENGINE=gguf" in envp.read_text(encoding="utf-8"))
                async with s.post(base + "/api/models/llm", json={
                        "action": "select", "name": "../escape.gguf"}) as r:
                    check("select rejects path traversal", r.status == 400)
                async with s.post(base + "/api/modules/remove",
                                  json={"id": "llm-gguf"}) as r:
                    d = await r.json()
                    check("remove blocked while LLM engine is gguf",
                          r.status == 400 and "in use" in d.get("message", ""), str(d))
                envp.write_text("STT_ENGINE=whisper\nLLM_ENGINE=api\n"
                                "LLM_GGUF_MODELS_DIR=" + str(modeldir) + "\n",
                                encoding="utf-8")
                async with s.post(base + "/api/modules/remove",
                                  json={"id": "llm-gguf"}) as r:
                    d = await r.json()
                    check("remove works when engine not gguf",
                          r.status == 200 and d.get("ok"), str(d))
                check("model file really gone", not m1.exists())
                async with s.post(base + "/api/models/llm", json={
                        "action": "remove", "name": m1.name}) as r:
                    check("remove of missing file is a clean 400",
                          r.status == 400, str(r.status))
                async with s.get(base + "/api/modules") as r:
                    d = await r.json()
                    ids = [m["id"] for m in d["modules"]]
                    check("modules list has packages and files",
                          "whisper-stt" in ids and "llm-gguf" in ids, str(ids))
                    check("livekit-server flagged in use",
                          next(m for m in d["modules"]
                               if m["id"] == "livekit-server")["in_use"] != "")
                async with s.post(base + "/api/modules/remove",
                                  json={"id": "livekit-server"}) as r:
                    check("livekit-server removal refused", r.status == 400)
                async with s.post(base + "/api/modules/remove",
                                  json={"id": "no-such-module"}) as r:
                    check("unknown module rejected", r.status == 400)
        finally:
            await runner.cleanup()
    finally:
        srv.ENV_FILE = old_env
        config.ENV_FILE = old_cfg_env
        import shutil as _sh
        _sh.rmtree(tmp, ignore_errors=True)


def section_supervisor_entry() -> None:
    print("[5] server.py entry point (start.bat silent-death regression)")
    import shutil
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    try:
        shutil.copytree(ROOT / "python", tmp / "python",
                        ignore=shutil.ignore_patterns("__pycache__"))
        p = subprocess.run(
            [sys.executable, "-u", str(tmp / "python" / "server.py")],
            capture_output=True, text=True, timeout=180, cwd=str(tmp))
        out = p.stdout + p.stderr
        # temp ROOT has no .venv, so a correct boot reaches main() and dies
        # loudly there; the lost __main__ guard exited 0 with no output
        check("python server.py runs main() instead of exiting silently",
              p.returncode != 0 and ".venv missing" in out,
              f"rc={p.returncode} out={out[:200]!r}")
        check("facade import as module still does not boot the supervisor",
              subprocess.run(
                  [sys.executable, "-c",
                   "import sys; sys.path.insert(0, r'" + str(tmp / "python")
                   + "'); import server; print('IMPORT_ONLY')"],
                  capture_output=True, text=True, timeout=180,
                  cwd=str(tmp)).stdout.count("IMPORT_ONLY") == 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def main_async() -> None:
    global LK_PORT
    LK_PORT = free_port()
    section_config()
    section_keyvault()
    friendly_llm_error_cases()
    section_env_file()
    section_llm_gguf()
    section_setup_preserve()
    await section_transcript_stream()
    section_providers()
    await section_server()
    await section_config_api()
    await section_modules_api()
    await section_mcp_live()
    section_worker_boot()
    section_supervisor_entry()


def main() -> None:
    asyncio.run(main_async())
    failed = [r for r in RESULTS if not r[1]]
    print("=" * 60)
    print(f"E2E SMOKE: {len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    if failed:
        print("FAILED:")
        for name, _, detail in failed:
            print("  -", name, (":: " + detail if detail else ""))
        sys.exit(1)
    print("ALL E2E CHECKS PASSED")


if __name__ == "__main__":
    main()

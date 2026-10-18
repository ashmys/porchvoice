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


async def main_async() -> None:
    global LK_PORT
    LK_PORT = free_port()
    section_config()
    section_providers()
    await section_server()
    await section_mcp_live()
    section_worker_boot()


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

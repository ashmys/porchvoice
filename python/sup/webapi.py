"""aiohttp web app: static UI, /token, config + models + modules + chat API."""
from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import urllib.error
import urllib.request
from pathlib import Path

import aiohttp
from aiohttp import WSMsgType, web
from livekit import api as lk_api

from . import chatstore, paths, state
from config import CONFIG_DEFAULTS, detect_lan_ip, llm_gguf_dir, \
    llm_gguf_model_path, llm_gguf_url, load_config, resolve_llm, \
    resolve_mcp, resolve_stt, resolve_tts
from .downloads import dl_job_snapshot, hf_resolve_file, jobs_running, \
    start_download_job
from .envstore import env_dict, mask_secrets, setting, write_env_keys
from .ggufsrv import current_mode, find_llama_server, LLM_GGUF_CATALOG
from keyvault import SECRET_KEYS, encrypt_value, is_encrypted
from .logx import log
from .modelist import check_gpu, check_llm, check_stt_provider, \
    check_tts_provider, count_local_models, list_stt_models, list_tts_models
from .modmgr import llm_preset_local, llm_preset_status, list_modules, \
    remove_module
from .procs import livekit_target, tcp_alive
from .tunnel import tunnel_info
from .uiopen import detect_browsers


def client_livekit_url(request: web.Request) -> str:
    mode = current_mode()
    req_host = (request.headers.get("Host") or "").split(":")[0].lower()
    forwarded_proto = (request.headers.get("X-Forwarded-Proto") or "").lower()
    tunnel_host = ""
    if state.TUNNEL.get("url"):
        tunnel_host = state.TUNNEL["url"].split("//", 1)[1].split("/")[0].lower()
    if mode == "public" and tunnel_host and (
            req_host == tunnel_host or forwarded_proto == "https"):
        return f"wss://{tunnel_host}/lk"
    if mode == "public" and forwarded_proto == "https":
        return f"wss://{req_host}/lk"
    if mode in ("wifi", "public"):
        return "ws://" + detect_lan_ip() + ":7880"
    return setting("LIVEKIT_URL", "ws://127.0.0.1:7880")


async def index(_request: web.Request) -> web.FileResponse:
    return web.FileResponse(paths.WEB_DIR / "index.html")


async def lib(_request: web.Request) -> web.FileResponse:
    return web.FileResponse(paths.WEB_DIR / "livekit-client.js")


async def favicon(_request: web.Request) -> web.Response:
    return web.Response(status=204)


async def token(request: web.Request) -> web.Response:
    identity = (request.query.get("identity")
                or f"user-{os.urandom(2).hex()}").strip()
    if not re.fullmatch(r"[\w-]{1,64}", identity):
        return web.json_response({"error": "bad identity"}, status=400)
    room = (request.query.get("room") or "").strip()
    if room:
        if not re.fullmatch(r"[\w-]{1,64}", room):
            return web.json_response({"error": "bad room name"}, status=400)
    else:
        room = f"voice-{os.urandom(4).hex()}"
    key = setting("LIVEKIT_API_KEY", "devkey")
    secret = setting("LIVEKIT_API_SECRET", "")
    if not key or not secret:
        log("token REFUSED: LIVEKIT_API_KEY/SECRET missing - check "
            "user_data/.env.local")
        return web.json_response(
            {"error": "server has no LIVEKIT_API_KEY/SECRET configured"},
            status=500)
    tk = (
        lk_api.AccessToken(key, secret)
        .with_identity(identity)
        .with_name(identity)
        .with_grants(lk_api.VideoGrants(room_join=True, room=room))
    )
    jwt = tk.to_jwt()
    client_url = client_livekit_url(request)
    log(f"token issued identity={identity} room={room} url={client_url}")
    return web.json_response({"url": client_url, "token": jwt,
                              "room": room, "identity": identity})


async def api_heartbeat(_request: web.Request) -> web.Response:
    state._LAST_BEAT = state.monotonic()
    state._CLOSE_PENDING_AT = 0.0
    return web.Response(status=204)


async def api_window_closing(_request: web.Request) -> web.Response:
    state._CLOSE_PENDING_AT = state.monotonic()
    state._CLOSE_CHECK.set()
    return web.Response(status=204)


async def api_browsers(_request: web.Request) -> web.Response:
    return web.json_response({
        "browsers": detect_browsers(),
        "chosen": setting("UI_BROWSER", ""),
    })


async def api_publish(_request: web.Request) -> web.Response:
    return web.json_response(tunnel_info())


async def api_mcp_status(_request: web.Request) -> web.Response:
    from mcp_client import probe_mcp

    sel = resolve_mcp(load_config())
    if not sel["enabled"]:
        return web.json_response({"enabled": False, "ok": False,
                                  "url": "", "tools": [],
                                  "error": "MCP_URL not configured"})
    result = await probe_mcp(sel["url"], sel["token"], sel["timeout"])
    result["enabled"] = True
    result["url"] = sel["url"]
    result["kind"] = sel["kind"]
    return web.json_response(result)


async def api_models_stt(_request: web.Request) -> web.Response:
    return web.json_response(list_stt_models())


async def api_models_tts(_request: web.Request) -> web.Response:
    return web.json_response(list_tts_models())


async def api_models_llm(_request: web.Request) -> web.Response:
    cfg = load_config()
    base = llm_gguf_dir(cfg)
    models = []
    selected = (cfg.get("LLM_GGUF_MODEL", "") or "").strip()
    auto_pick = llm_gguf_model_path(cfg)
    if base.is_dir():
        for f in sorted(base.glob("*.gguf")):
            if f.name.lower().startswith("mmproj"):
                continue
            try:
                size_mb = round(f.stat().st_size / 1e6)
            except OSError:
                size_mb = 0
            models.append({"name": f.name, "size_mb": size_mb,
                           "selected": bool(selected and f.name == selected)
                                       or (not selected and auto_pick
                                           and f.name == auto_pick.name)})
    sel = resolve_llm(cfg)
    return web.json_response({
        "dir": str(base),
        "models": models,
        "presets": llm_preset_status(),
        "server": llm_gguf_url(cfg),
        "engine": (cfg.get("LLM_ENGINE", "") or "").strip().lower(),
        "binary": find_llama_server(),
        "resolve": {"base_url": sel["base_url"], "kind": sel["kind"],
                    "engine": sel.get("engine", ""), "source": sel["source"]},
        "downloads": dl_job_snapshot(),
        "busy": not jobs_running(),
    })


async def api_models_llm_post(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "invalid json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid json"}, status=400)
    action = str(body.get("action") or "")
    name = str(body.get("name") or "")
    if action == "select":
        if not name:
            return web.json_response({"error": "missing model name"}, status=400)
        base = llm_gguf_dir(load_config())
        if not (base / name).is_file() or not name.endswith(".gguf"):
            return web.json_response({"error": "unknown model file"}, status=400)
        write_env_keys({"LLM_GGUF_MODEL": name, "LLM_ENGINE": "gguf"})
        return web.json_response({"ok": True, "selected": name,
                                  "note": "restart the agent (Settings -> "
                                          "Restart) or start the next "
                                          "conversation"})
    if action == "download":
        preset = next((p for p in LLM_GGUF_CATALOG if p[0] == name), None)
        if preset is None:
            return web.json_response({"error": "unknown preset"}, status=400)
        local = llm_preset_local(preset)
        if local:
            write_env_keys({"LLM_GGUF_MODEL": local})
            log(f"[llm-gguf] {local} is already downloaded - not downloading "
                "again")
            return web.json_response({"ok": True, "file": local, "already": True,
                                      "message": f"'{local}' is already "
                                                 "downloaded - selected it "
                                                 "instead"})
        pid, repo, pattern, _mb, _note = preset
        if not jobs_running():
            return web.json_response({"error": "another download is running"},
                                     status=409)
        try:
            url, real_name = await asyncio.to_thread(hf_resolve_file, repo,
                                                     pattern)
        except Exception as exc:
            return web.json_response(
                {"error": f"could not resolve {repo}: {str(exc)[:140]}"},
                status=502)
        dest = llm_gguf_dir(load_config()) / real_name
        res = start_download_job(f"llm:{real_name}", url, dest,
                                 min_bytes=50_000_000)
        if not res.get("ok"):
            return web.json_response({"error": res.get("error")}, status=409)
        write_env_keys({"LLM_GGUF_MODEL": real_name})
        log(f"[llm-gguf] downloading {real_name} from {repo} in the background")
        return web.json_response({"ok": True, "file": real_name})
    if action == "remove":
        ok, msg = await asyncio.to_thread(remove_module, "llm-gguf", name)
        return web.json_response({"ok": ok, "message": msg},
                                 status=200 if ok else 400)
    if action == "set_dir":
        d = name.strip()
        if d and not Path(d).is_dir():
            return web.json_response({"error": "folder does not exist"},
                                     status=400)
        write_env_keys({"LLM_GGUF_MODELS_DIR": d})
        return web.json_response({"ok": True, "dir": d})
    return web.json_response({"error": "unknown action"}, status=400)


async def api_modules(_request: web.Request) -> web.Response:
    return web.json_response({"modules": list_modules(),
                              "downloads": dl_job_snapshot()})


async def api_modules_remove(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "invalid json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid json"}, status=400)
    mod_id = str(body.get("id") or "")
    target = str(body.get("target") or "")
    ok, msg = await asyncio.to_thread(remove_module, mod_id, target)
    status = 200 if ok else 400
    return web.json_response({"ok": ok, "message": msg}, status=status)


async def api_llm_models(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    cfg = load_config()
    base = str(body.get("url") or cfg.get("LLM_BASE_URL", "") or "").strip()
    if "key" in body and body.get("key") is not None:
        key = str(body.get("key")).strip()
    else:
        key = (cfg.get("LLM_API_KEY", "")
               or os.environ.get("OPENAI_API_KEY", "") or "").strip()
    if not base and key:
        base = "https://api.openai.com/v1"
    if not re.match(r"^https?://", base):
        return web.json_response({
            "error": "base URL must start with http:// or https://",
            "models": []})
    url = base.rstrip("/") + "/models"
    headers = {"Authorization": "Bearer " + key} if key else {}

    def _fetch():
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    try:
        data = await asyncio.to_thread(_fetch)
    except urllib.error.HTTPError as e:
        why = "auth failed (check the API key)" if e.code in (401, 403) \
            else f"HTTP {e.code}"
        return web.json_response({"error": why, "models": []})
    except Exception as exc:
        return web.json_response({"error": str(exc)[:160], "models": []})
    if isinstance(data, dict):
        items = data.get("data") or data.get("models") or []
    elif isinstance(data, list):
        items = data
    else:
        items = []
    models = []
    for m in items:
        if isinstance(m, dict):
            mid = m.get("id") or m.get("name") or m.get("model")
            if mid:
                models.append(str(mid))
        elif isinstance(m, str):
            models.append(m)
    models = sorted(set(models))
    return web.json_response({"base": base, "count": len(models),
                              "models": models})


async def api_config_get(_request: web.Request) -> web.Response:
    return web.json_response({"config": mask_secrets(env_dict()),
                              "secret_keys": list(SECRET_KEYS)})


async def api_config_post(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "invalid json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid json"}, status=400)
    updates = body.get("config")
    if not isinstance(updates, dict):
        return web.json_response({"error": "missing config object"}, status=400)
    current = env_dict()
    allowed = set(current) | {
        "LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET",
        "PUBLISH_MODE", "PUBLIC_PORT",
        "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL",
        "LLM_COMPAT_MODE", "LLM_STREAMING",
        "LLM_THINKING", "LLM_SPEAK_THINKING",
        "LLM_ENGINE", "LLM_GGUF_SERVER", "LLM_GGUF_PORT",
        "LLM_GGUF_MODEL", "LLM_GGUF_MODELS_DIR", "LLM_GGUF_NGL", "LLM_GGUF_CTX",
        "STT_BASE_URL", "STT_API_KEY", "STT_MODEL", "STT_ENGINE",
        "STT_GGUF_SERVER", "STT_GGUF_PORT", "STT_GGUF_MODEL",
        "TTS_BASE_URL", "TTS_API_KEY", "TTS_MODEL",
        "TTS_ENGINE", "TTS_VOICE", "TTS_SPEED", "KOKORO_VOICE",
        "MCP_URL", "MCP_AUTH_TOKEN", "MCP_TIMEOUT",
        "WHISPER_MODEL", "WHISPER_DEVICE", "WHISPER_COMPUTE", "WHISPER_LANGUAGE",
        "WHISPER_BEAM", "WHISPER_VAD_FILTER", "WHISPER_TEMPERATURE",
        "WHISPER_MIN_LOGPROB", "WHISPER_MAX_COMPRATIO",
        "WHISPER_INITIAL_PROMPT",
        "CAP_ECHO_CANCEL", "CAP_NOISE_SUPPRESS", "CAP_AUTO_GAIN",
        "VAD_MIN_SILENCE", "VAD_ACTIVATION_THRESHOLD",
        "ENDPOINTING_DELAY", "ECHO_GUARD", "ECHO_GUARD_TAIL", "LOG_LEVEL",
        "AUTO_OPEN", "UI_WINDOW", "CLOSE_WITH_WINDOW",
        "UI_BROWSER", "UI_PROFILE",
    }
    for k, v in updates.items():
        if k not in allowed or not isinstance(v, str):
            continue
        v = v.strip()
        if k in SECRET_KEYS and v == paths.SECRET_SENTINEL:
            continue
        if not v and str(CONFIG_DEFAULTS.get(k, "") or ""):
            continue
        if k in SECRET_KEYS and v and not is_encrypted(v):
            v = encrypt_value(v)
        current[k] = v
    lines = ["# PorchVoice settings (saved from the web UI)",
             "# Applies to the NEXT conversation after you click Save."]
    for k, v in sorted(current.items()):
        lines.append(f"{k}={v}")
    try:
        state.ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
        state.ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        log(f"config save FAILED: {exc}")
        return web.json_response({"error": f"could not write "
                                           f"{state.ENV_FILE.name}: {exc}"},
                                 status=500)
    log("config saved (applies to the next conversation)")
    return web.json_response({"ok": True, "config": mask_secrets(current)})


async def api_reveal(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "invalid json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid json"}, status=400)
    key = str(body.get("key") or "")
    if key not in SECRET_KEYS:
        return web.json_response({"error": "unknown secret"}, status=400)
    value = load_config().get(key, "")
    return web.json_response({"key": key, "value": value, "set": bool(value)})


async def api_chat_sessions(_request: web.Request) -> web.Response:
    return web.json_response({"sessions": chatstore.list_sessions()})


async def api_chat_session(request: web.Request) -> web.Response:
    name = request.query.get("file", "")
    room = request.query.get("room", "")
    f = None
    if room:
        if not re.fullmatch(r"[\w-]{1,64}", room or ""):
            return web.json_response({"error": "bad room name"}, status=400)
        f = chatstore.latest_session_for_room(room)
        if f is None:
            return web.json_response({"file": "", "messages": []})
    else:
        if not re.fullmatch(r"[\w.-]+\.jsonl", name or ""):
            return web.json_response({"error": "bad file name"}, status=400)
        f = paths.CHAT_DIR / name
    if not f.is_file():
        return web.json_response({"error": "not found"}, status=404)
    try:
        msgs = chatstore.read_messages(f)
    except OSError as exc:
        return web.json_response({"error": str(exc)}, status=500)
    return web.json_response({"file": f.name, "messages": msgs})


async def api_chat_clear(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "invalid json"}, status=400)
    room = (body.get("room") or "").strip() if isinstance(body, dict) else ""
    wipe_all = isinstance(body, dict) and body.get("all") is True
    if not re.fullmatch(r"[\w-]{1,64}", room or ""):
        return web.json_response({"error": "bad room name"}, status=400)
    cleared = await asyncio.to_thread(chatstore.clear_history, room, wipe_all)
    return web.json_response({"ok": True, "cleared": cleared})


async def api_check(_request: web.Request) -> web.Response:
    lk_host, lk_port = livekit_target()
    lk_up = await asyncio.to_thread(tcp_alive, lk_host, lk_port)
    llm_state = await asyncio.to_thread(check_llm)
    gpu_state = await asyncio.to_thread(check_gpu)
    try:
        kids = {name: ("up" if p.poll() is None else f"exit {p.returncode}")
                for name, p in state.CHILDREN.items()}
    except Exception:
        kids = {}
    lib_ok = (paths.WEB_DIR / "livekit-client.js").is_file()
    env_ok = state.ENV_FILE.is_file()
    chat_ok, chat_why = True, ""
    try:
        paths.CHAT_DIR.mkdir(parents=True, exist_ok=True)
        probe = paths.CHAT_DIR / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        chat_ok, chat_why = False, str(exc)[:160]
    n_logs = 0
    try:
        n_logs = sum(1 for _ in paths.CHAT_DIR.glob("*.jsonl")) \
            if paths.CHAT_DIR.is_dir() else 0
    except OSError:
        pass
    sel = resolve_llm(load_config())
    cfg = load_config()
    stt_state = await asyncio.to_thread(check_stt_provider, cfg)
    tts_state = await asyncio.to_thread(check_tts_provider, cfg)
    return web.json_response({
        "livekit": {"status": "up" if lk_up else "down",
                    "url": setting("LIVEKIT_URL")},
        "llm": {"status": llm_state, "url": sel["base_url"], "kind": sel["kind"]},
        "stt": stt_state,
        "tts": tts_state,
        "publish": tunnel_info(),
        "gpu": gpu_state,
        "children": kids,
        "lib_file": {"exists": lib_ok},
        "env_file": {"exists": env_ok},
        "chat_log": {"writable": chat_ok, "error": chat_why, "files": n_logs},
    })


async def api_restart(_request: web.Request) -> web.Response:
    if not state.CHILDREN:
        return web.json_response({"error": "nothing is running"}, status=409)
    names = list(state.CHILDREN.keys())
    for name in names:
        state._CRASHES.pop(name, None)
        state._RESTART[name] = 1
        try:
            state.CHILDREN[name].terminate()
        except OSError:
            pass
    log(f"restart requested from the UI - respawning: {', '.join(names)}")
    return web.json_response({"ok": True, "restarting": names})


async def health(_request: web.Request) -> web.Response:
    lk_host, lk_port = livekit_target()
    lk_up = await asyncio.to_thread(tcp_alive, lk_host, lk_port)
    llm_state = await asyncio.to_thread(check_llm)
    gpu_state = await asyncio.to_thread(check_gpu)
    try:
        kids = {name: ("up" if p.poll() is None else f"exit {p.returncode}")
                for name, p in state.CHILDREN.items()}
    except Exception:
        kids = {}
    lib_ok = (paths.WEB_DIR / "livekit-client.js").is_file()
    sel = resolve_llm(load_config())
    mcp_sel = resolve_mcp(load_config())
    from .envstore import venv_python

    return web.json_response({
        "ui": "ok",
        "python": platform.python_version(),
        "livekit": {"status": "up" if lk_up else "down",
                    "url": setting("LIVEKIT_URL")},
        "llm": {"status": llm_state, "url": sel["base_url"],
                "kind": sel["kind"]},
        "mcp": {"enabled": mcp_sel["enabled"], "url": mcp_sel["url"],
                "kind": mcp_sel["kind"]},
        "publish": tunnel_info(),
        "gpu": gpu_state,
        "models": count_local_models(),
        "children": kids,
        "venv": venv_python().exists(),
        "lib_file": {"exists": lib_ok},
    })


async def lk_proxy(request: web.Request) -> web.StreamResponse:
    suffix = request.match_info.get("suffix", "")
    if (request.headers.get("Upgrade", "") or "").lower() == "websocket":
        return await lk_ws_proxy(request, suffix)
    return await lk_http_proxy(request, suffix)


def _upstream_base() -> str:
    url = setting("LIVEKIT_URL", "ws://127.0.0.1:7880")
    return url.replace("wss://", "https://").replace("ws://", "http://") \
        .rstrip("/")


async def lk_http_proxy(request: web.Request, suffix: str) -> web.Response:
    qs = ("?" + request.query_string) if request.query_string else ""
    target = f"{_upstream_base()}/{suffix}{qs}"
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in paths.HOP_BY_HOP}
    body = await request.read()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.request(
                    request.method, target, headers=headers, data=body,
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=15)) as up:
                payload = await up.read()
                out_headers = {k: v for k, v in up.headers.items()
                               if k.lower() not in paths.HOP_BY_HOP}
                return web.Response(status=up.status, body=payload,
                                    headers=out_headers)
    except Exception as exc:
        return web.json_response({"error": f"lk proxy failed: {exc}"}, status=502)


async def lk_ws_proxy(request: web.Request, suffix: str) -> web.StreamResponse:
    qs = ("?" + request.query_string) if request.query_string else ""
    target = (_upstream_base().replace("http://", "ws://")
              .replace("https://", "wss://")) + f"/{suffix}{qs}"
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in paths.HOP_BY_HOP
               and k.lower() not in ("sec-websocket-key", "sec-websocket-version",
                                     "sec-websocket-extensions",
                                     "sec-websocket-protocol")}
    ws_out = web.WebSocketResponse()
    await ws_out.prepare(request)
    session = aiohttp.ClientSession()
    ws_up = None
    try:
        try:
            ws_up = await session.ws_connect(target, headers=headers)
        except Exception as exc:
            log(f"lk ws proxy upstream connect failed ({exc})")
            await ws_out.close()
            return ws_out

        async def pump(src, dst, name):
            try:
                async for msg in src:
                    if msg.type == WSMsgType.TEXT:
                        await dst.send_str(msg.data)
                    elif msg.type == WSMsgType.BINARY:
                        await dst.send_bytes(msg.data)
                    elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING,
                                      WSMsgType.CLOSED, WSMsgType.ERROR):
                        break
            except Exception:
                pass
            finally:
                try:
                    await dst.close()
                except Exception:
                    pass

        await asyncio.gather(
            pump(ws_up, ws_out, "upstream->client"),
            pump(ws_out, ws_up, "client->upstream"),
            return_exceptions=True,
        )
        return ws_out
    finally:
        if ws_up is not None and not ws_up.closed:
            await ws_up.close()
        await session.close()


@web.middleware
async def log_and_nocache(request: web.Request, handler):
    status = 500
    try:
        resp = await handler(request)
        status = resp.status
    except web.HTTPException as e:
        status = e.status
        raise
    finally:
        if request.path not in ("/api/heartbeat", "/api/window/closing"):
            log(f"{request.method} {request.path_qs} -> {status}")
        try:
            resp.headers["Cache-Control"] = "no-store"
        except UnboundLocalError:
            pass
    return resp


def build_app() -> web.Application:
    app = web.Application(middlewares=[log_and_nocache])
    app.router.add_get("/", index)
    app.router.add_get("/livekit-client.js", lib)
    app.router.add_static("/static/", path=str(paths.WEB_DIR))
    app.router.add_get("/health", health)
    app.router.add_get("/token", token)
    app.router.add_get("/api/config", api_config_get)
    app.router.add_post("/api/config", api_config_post)
    app.router.add_post("/api/reveal", api_reveal)
    app.router.add_get("/api/models/stt", api_models_stt)
    app.router.add_get("/api/models/tts", api_models_tts)
    app.router.add_get("/api/models/llm", api_models_llm)
    app.router.add_post("/api/models/llm", api_models_llm_post)
    app.router.add_get("/api/modules", api_modules)
    app.router.add_post("/api/modules/remove", api_modules_remove)
    app.router.add_get("/api/browsers", api_browsers)
    app.router.add_post("/api/llm/models", api_llm_models)
    app.router.add_get("/api/chat/sessions", api_chat_sessions)
    app.router.add_get("/api/chat/session", api_chat_session)
    app.router.add_post("/api/chat/clear", api_chat_clear)
    app.router.add_get("/api/check", api_check)
    app.router.add_post("/api/restart", api_restart)
    app.router.add_post("/api/heartbeat", api_heartbeat)
    app.router.add_post("/api/window/closing", api_window_closing)
    app.router.add_get("/api/publish", api_publish)
    app.router.add_get("/api/mcp/status", api_mcp_status)
    app.router.add_route("*", "/lk/{suffix:.*}", lk_proxy)
    app.router.add_get("/favicon.ico", favicon)
    return app

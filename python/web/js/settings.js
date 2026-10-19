"use strict";


function addOpt(sel, value, label) {
  const o = document.createElement("option");
  o.value = value; o.textContent = label;
  sel.appendChild(o);
}

function ensureOpt(sel, val) {
  if (!sel) return;
  const has = Array.from(sel.options).some((o) => o.value === val);
  if (!has && val) addOpt(sel, val, val);
}

function cfgEls() {
  const out = {};
  for (const el of document.querySelectorAll("[id^=cfg-]")) out[el.id.slice(4)] = el;
  return out;
}

function updateRange(key) {
  const el = $("cfg-" + key), lab = $("val-" + key);
  if (el && lab && el.type === "range") lab.textContent = el.value;
}
document.addEventListener("input", (ev) => {
  if (ev.target.id && ev.target.id.startsWith("cfg-")) updateRange(ev.target.id.slice(4));
});


const PROF_KEY = "providerProfiles";
const PROF_FIELDS = ["LLM_BASE_URL", "LLM_MODEL"];
const PROF_KEY_FIELD = "LLM_API_KEY";
let activeProfile = "";

const SECRET_IDS = ["cfg-LLM_API_KEY", "cfg-STT_API_KEY", "cfg-TTS_API_KEY",
                    "cfg-MCP_AUTH_TOKEN", "cfg-LIVEKIT_API_SECRET"];

function decorateSecretInputs() {
  for (const id of SECRET_IDS) {
    const el = $(id);
    if (!el || el.parentElement.classList.contains("keywrap")) continue;
    const wrap = document.createElement("div");
    wrap.className = "keywrap";
    el.parentNode.insertBefore(wrap, el);
    wrap.appendChild(el);
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "eyebtn";
    btn.textContent = "show";
    btn.addEventListener("click", async () => {
      if (btn.textContent === "show") {
        try {
          const d = await fetchJson("/api/reveal", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ key: id.slice(4) }),
          });
          el.type = "text";
          el.value = d.value || "";
          btn.textContent = "hide";
          if (!d.set) toast("No value saved for this field yet");
        } catch (e) {
          toast("Reveal failed: " + (e && e.message ? e.message : e));
        }
      } else {
        el.type = "password";
        el.value = el.value ? SECRET_SENTINEL : "";
        btn.textContent = "show";
      }
    });
    wrap.appendChild(btn);
  }
}

function loadProfiles() {
  try { return JSON.parse(localStorage.getItem(PROF_KEY) || "{}") || {}; }
  catch (e) { return {}; }
}
function storeProfiles(p) {
  try { localStorage.setItem(PROF_KEY, JSON.stringify(p)); } catch (e) {}
}
function renderProfileList() {
  const sel = $("profileselect");
  if (!sel) return;
  const profiles = loadProfiles();
  const keep = activeProfile;
  sel.innerHTML = '<option value="">pick a saved profile...</option>';
  Object.keys(profiles).sort().forEach((name) => {
    const o = document.createElement("option");
    o.value = name;
    o.textContent = name;
    sel.appendChild(o);
  });
  if (keep && profiles[keep]) sel.value = keep;
  else sel.value = "";
}
function loadProfile(name) {
  const profiles = loadProfiles();
  const p = profiles[name];
  if (!p) return false;
  PROF_FIELDS.forEach((k) => {
    const el = $("cfg-" + k);
    if (el && typeof p[k] === "string") el.value = p[k];
  });
  const keyEl = $("cfg-" + PROF_KEY_FIELD);
  if (keyEl) keyEl.value = p[PROF_KEY_FIELD] === "" ? "" : SECRET_SENTINEL;
  activeProfile = name;
  try { localStorage.setItem("lastProfile", name); } catch (e) {}
  const nameEl = $("profilename");
  if (nameEl) nameEl.value = name;
  scheduleAutoSave();
  refreshLlmModels();
  toast("Profile '" + name + "' loaded");
  return true;
}
function saveProfile(name, silent) {
  name = (name || "").trim();
  if (!name) { if (!silent) toast("Type a name for the profile first"); return; }
  const profiles = loadProfiles();
  profiles[name] = {};
  PROF_FIELDS.forEach((k) => {
    const el = $("cfg-" + k);
    profiles[name][k] = el ? el.value : "";
  });
  const keyEl = $("cfg-" + PROF_KEY_FIELD);
  profiles[name][PROF_KEY_FIELD] = (keyEl && keyEl.value) ? SECRET_SENTINEL : "";
  storeProfiles(profiles);
  activeProfile = name;
  try { localStorage.setItem("lastProfile", name); } catch (e) {}
  renderProfileList();
  if (!silent) toast("Profile '" + name + "' saved");
}
function deleteProfile(name) {
  name = (name || $("profileselect").value || activeProfile || "").trim();
  if (!name) { toast("Pick a profile to delete"); return; }
  const profiles = loadProfiles();
  if (!profiles[name]) { toast("No profile named '" + name + "'"); return; }
  delete profiles[name];
  storeProfiles(profiles);
  if (activeProfile === name) activeProfile = "";
  renderProfileList();
  toast("Profile '" + name + "' deleted");
}

$("profileselect").addEventListener("change", () => {
  const name = $("profileselect").value;
  if (name) loadProfile(name);
});
$("profsave").addEventListener("click", () => saveProfile($("profilename").value));
$("profdelete").addEventListener("click", () => deleteProfile(""));


const SECRET_SENTINEL = "__saved__";

decorateSecretInputs();

let autoSaveTimer = 0;
let autoSaving = false;

function scheduleAutoSave() {
  clearTimeout(autoSaveTimer);
  autoSaveTimer = setTimeout(() => { saveSettings(true); }, 800);
}

function markActiveProfileFromValues() {
  const vals = PROF_FIELDS.map((k) => { const el = $("cfg-" + k); return el ? el.value : ""; });
  const keyVal = ($("cfg-" + PROF_KEY_FIELD) || {}).value || "";
  const profiles = loadProfiles();
  let match = "";
  for (const [name, p] of Object.entries(profiles)) {
    const keyOk = (p[PROF_KEY_FIELD] || "") === (keyVal ? SECRET_SENTINEL : "");
    if (keyOk && PROF_FIELDS.every((k, i) => (p[k] || "") === vals[i])) { match = name; break; }
  }
  activeProfile = match;
  return match;
}

async function saveSettings(silent) {
  const msg = $("savemsg");
  const config = {};
  for (const [k, el] of Object.entries(cfgEls())) config[k] = el.value;
  if (autoSaving) return;
  autoSaving = true;
  try {
    const r = await fetch("/api/config", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ config }),
    });
    if (!r.ok) {
      let why = "HTTP " + r.status;
      try { const j = await r.json(); if (j.error) why = j.error; } catch (e) {}
      throw new Error(why);
    }
    if (!silent) {
      msg.style.color = "var(--ok)";
      msg.textContent = "Saved - applies to your NEXT conversation.";
      toast("Settings saved");
    } else {
      const prof = markActiveProfileFromValues();
      if (prof) saveProfile(prof, true);
      msg.style.color = "var(--ok)";
      msg.textContent = "Auto-saved " + new Date().toLocaleTimeString() +
        " - applies to your NEXT conversation.";
    }
    checkHealth();
  } catch (e) {
    if (!silent) {
      msg.style.color = "var(--warn)";
      msg.textContent = "Save failed: " + (e && e.message ? e.message : e);
    } else {
      console.warn("auto-save failed", e);
    }
  } finally {
    autoSaving = false;
  }
}


document.addEventListener("change", (ev) => {
  const id = ev.target && ev.target.id;
  if (!id) return;
  if (id.startsWith("cfg-")) scheduleAutoSave();
});
document.addEventListener("input", (ev) => {
  const id = ev.target && ev.target.id;
  if (!id) return;
  if (id.startsWith("cfg-") &&
      (ev.target.tagName === "INPUT" || ev.target.tagName === "TEXTAREA")) {
    scheduleAutoSave();
  }
});


async function fillMicDevices() {
  const sel = $("micdevice");
  if (!sel || !navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return;
  try {
    const devs = await navigator.mediaDevices.enumerateDevices();
    const cur = (() => { try { return localStorage.getItem("micDeviceId") || ""; } catch (e) { return ""; } })();
    sel.innerHTML = '<option value="">System default</option>';
    let found = false;
    devs.forEach((d) => {
      if (d.kind !== "audioinput") return;
      const label = d.label || ("microphone " + sel.options.length);
      addOpt(sel, d.deviceId, label);
      if (d.deviceId && d.deviceId === cur) found = true;
    });
    if (found) sel.value = cur;
  } catch (e) {
    console.warn("enumerateDevices failed", e);
  }
}

$("micdevice").addEventListener("change", () => {
  try { localStorage.setItem("micDeviceId", $("micdevice").value || ""); } catch (e) {}
  toast("Microphone saved - applies to the next call");
});

async function fillBrowserPicker(cfg) {
  const sel = $("cfg-UI_BROWSER");
  if (!sel) return;
  let browsers = [];
  try {
    const d = await fetchJson("/api/browsers");
    browsers = d.browsers || [];
  } catch (e) {
    console.warn("browser list failed", e);
  }

  const keep = sel.value;
  sel.innerHTML = '<option value="">System default (auto)</option>';
  browsers.forEach((b) => {
    if (!b.id || b.id === "custom") return;
    const label = b.name + (b.default ? " \u2014 default" : "") +
      (b.chromium ? "" : " (normal window)");
    if (!Array.from(sel.options).some((o) => o.value === b.id)) {
      addOpt(sel, b.id, label);
    }
  });
  const saved = cfg.UI_BROWSER || "";
  if (saved) {
    ensureOpt(sel, saved);
    sel.value = saved;
  } else if (keep) {
    sel.value = keep;
  }
}

const KOKORO_VOICES = [
  "af_heart", "af_alloy", "af_aoede", "af_bella", "af_jessica", "af_kore",
  "af_nicole", "af_nova", "af_river", "af_sarah", "af_sky",
  "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam", "am_michael",
  "am_onyx", "am_puck", "am_santa",
  "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
  "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
];

async function loadConfigForm() {
  const [cfgR, sttR, ttsR] = await Promise.all([
    fetchJson("/api/config"),
    fetchJson("/api/models/stt"),
    fetchJson("/api/models/tts"),
  ]);
  const cfg = cfgR.config || {};
  fillBrowserPicker(cfg);
  renderGgufBox(sttR && sttR.gguf);
  for (const [k, el] of Object.entries(cfgEls())) {
    if (cfg[k] !== undefined && cfg[k] !== "") {
      if (el.tagName === "SELECT") ensureOpt(el, cfg[k]);
      el.value = cfg[k];
    } else if (el.tagName === "SELECT" && !el.value) {

    }
    if (el.type === "range") updateRange(k);
  }
  const sel = $("cfg-WHISPER_MODEL");
  sel.innerHTML = "";
  (sttR.local_folders || []).forEach((m) => addOpt(sel, m.name, m.name + " (folder, " + m.size_mb + " MB)"));
  (sttR.presets || []).forEach((m) => addOpt(sel, m.name, m.name + " (" + m.size_mb + " MB)" + (m.local ? " \u2713" : "")));
  if (cfg.WHISPER_MODEL) {
    ensureOpt(sel, cfg.WHISPER_MODEL);
    sel.value = cfg.WHISPER_MODEL;
  }
  const ksel = $("cfg-KOKORO_VOICE");
  if (ksel) {
    ksel.innerHTML = "";
    const kvoices = ttsR.kokoro_voices && ttsR.kokoro_voices.length ? ttsR.kokoro_voices : KOKORO_VOICES;
    kvoices.forEach((v) => addOpt(ksel, v, v));
    const kcur = cfg.KOKORO_VOICE || "af_heart";
    ensureOpt(ksel, kcur);
    ksel.value = kcur;
  }
  const vsel = $("cfg-TTS_VOICE");
  vsel.innerHTML = "";
  (ttsR.voices || []).forEach((v) => addOpt(vsel, v.name, v.name + (v.local ? " \u2713" : " (download needed)")));
  if (cfg.TTS_VOICE) {
    ensureOpt(vsel, cfg.TTS_VOICE);
    vsel.value = cfg.TTS_VOICE;
  }
  renderProfileList();
  const last = (() => { try { return localStorage.getItem("lastProfile") || ""; } catch (e) { return ""; } })();
  if (last && loadProfiles()[last]) loadProfile(last);
  else markActiveProfileFromValues();
  updateLlmEngineState();
  refreshLlmModels();
  refreshLlmGguf();
}

let modelFetchTimer = 0;
let modelFetchStartedAt = 0;

let lastAutoPick = "";


async function refreshLlmModels() {
  const sel = $("llmmodels"), hint = $("modelhint");
  if (!sel) return;
  if (llmEngineManaged()) { updateLlmEngineState(); return; }
  const url = ($("cfg-LLM_BASE_URL").value || "").trim();
  const keyEl = $("cfg-LLM_API_KEY");
  const rawKey = (keyEl ? keyEl.value : "").trim();
  const key = rawKey === SECRET_SENTINEL ? null : rawKey;
  modelFetchStartedAt = Date.now();
  sel.innerHTML = '<option value="">loading...</option>';
  hint.textContent = "Fetching model list from " + (url || "(auto-detect)") + " ...";
  try {
    const r = await fetch("/api/llm/models", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, key }),
    });
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    const list = d.models || [];
    sel.innerHTML = "";
    if (!list.length) {
      addOpt(sel, "", "(no models returned)");
      hint.textContent = "The provider returned no models - type the model id manually above.";
      return;
    }
    list.forEach((id) => addOpt(sel, id, id));
    const cur = ($("cfg-LLM_MODEL").value || "").trim();
    const userEdited = modelEditAt > modelFetchStartedAt;
    let pick = "";
    if (cur && list.indexOf(cur) >= 0) {
      pick = cur;
    } else if (cur && userEdited) {


      hint.textContent = "'" + cur + "' is not in the provider's list (" +
        list.length + " models) - keeping your entry.";
      return;
    } else if (cur && cur !== lastAutoPick) {

      hint.textContent = "'" + cur + "' is not in the provider's list (" +
        list.length + " models) - keeping it. Pick below to change.";
      return;
    } else {
      pick = list.find((m) => m.indexOf(":free") >= 0)
        || (list.indexOf("openrouter/auto") >= 0 ? "openrouter/auto" : "")
        || list[0];
    }
    lastAutoPick = pick;
    sel.value = pick;
    if (pick !== cur) {
      $("cfg-LLM_MODEL").value = pick;
      scheduleAutoSave();
      hint.textContent = "Auto-selected '" + pick + "' (" + list.length + " models found). Pick another or type your own.";
    } else {
      hint.textContent = "Using your last model '" + cur + "' (" + list.length + " available).";
    }
  } catch (e) {
    sel.innerHTML = '<option value="">(unavailable)</option>';
    hint.textContent = "Could not list models (" + (e && e.message ? e.message : e) + ") - type the model id manually above.";
  }
}

$("refreshmodels").addEventListener("click", refreshLlmModels);

// ---------- LLM engine field locking (managed llama.cpp) ----------

const LLM_MANAGED_IDS = ["cfg-LLM_BASE_URL", "cfg-LLM_API_KEY", "cfg-LLM_MODEL",
                         "llmmodels", "refreshmodels",
                         "profileselect", "profilename", "profsave", "profdelete"];

function llmEngineManaged() {
  const el = $("cfg-LLM_ENGINE");
  return !!el && el.value === "gguf";
}

function updateLlmEngineState() {
  const managed = llmEngineManaged();
  for (const id of LLM_MANAGED_IDS) {
    const el = $(id);
    if (!el) continue;
    el.disabled = managed;
    const field = el.closest(".field");
    if (field) field.classList.toggle("disabled", managed);
  }
  const hint = $("modelhint");
  if (hint && managed) {
    hint.textContent = "The built-in llama.cpp server manages the model - "
      + "URL / API key / model picker are not used. Pick the model in the "
      + "GGUF section below (or switch the engine back to Auto / API).";
  }
}

$("cfg-LLM_ENGINE").addEventListener("change", updateLlmEngineState);

$("llmmodels").addEventListener("change", () => {
  const v = $("llmmodels").value;
  if (v) {
    $("cfg-LLM_MODEL").value = v;
    scheduleAutoSave();
    $("modelhint").textContent = "Picked '" + v + "' - saved automatically.";
  }
});
let modelEditAt = 0;
["cfg-LLM_BASE_URL", "cfg-LLM_API_KEY"].forEach((id) => {
  $(id).addEventListener("change", () => {
    clearTimeout(modelFetchTimer);
    modelFetchTimer = setTimeout(refreshLlmModels, 500);
  });
});
$("cfg-LLM_MODEL").addEventListener("input", () => { modelEditAt = Date.now(); });

async function refreshMcpStatus() {
  const box = $("mcpbox");
  if (!box) return;
  const url = ($("cfg-MCP_URL").value || "").trim();
  if (!url) { box.textContent = "not configured"; return; }
  box.textContent = "probing " + url + " ...";
  try {
    const d = await fetchJson("/api/mcp/status");
    if (d.ok) {
      box.textContent = "connected - " + d.tools.length + " tool(s): " +
        (d.tools.slice(0, 8).join(", ") || "(none)") +
        (d.tools.length > 8 ? ", ..." : "");
    } else {
      box.textContent = "unreachable - " + (d.error || "unknown error");
    }
  } catch (e) {
    box.textContent = "probe failed - " + (e && e.message ? e.message : e);
  }
}
$("mcptest").addEventListener("click", refreshMcpStatus);

function renderGgufBox(gg) {
  const box = $("ggufbox");
  if (!box) return;
  if (!gg) { box.textContent = "status unavailable"; return; }
  const files = gg.ready
    ? "model files ready (" + gg.model + ")"
    : "model files MISSING (run setup step 7)";
  const bin = gg.binary ? "llama-server found" : "llama-server not found";
  const mode = gg.engine === "gguf" ? "SELECTED as STT engine" : "not selected";
  box.textContent = files + " \u00b7 " + bin + " \u00b7 " + mode + " \u00b7 " + gg.server;
}

async function refreshProviderStatus() {
  const box = $("providerbox");
  if (!box) return;
  box.textContent = "checking providers...";
  const parts = [];
  try {
    const c = await fetchJson("/api/check");
    const lk = c.livekit || {};
    parts.push("livekit " + (lk.status === "up" ? "up" : "DOWN"));
    const llm = String(c.llm && c.llm.status || "");
    parts.push("llm " + (llm.startsWith("up") ? llm : llm || "down"));
    const stt = c.stt || {};
    parts.push("stt " + (stt.status === "up" ? "up (" + stt.detail + ")"
                          : stt.status === "down" ? "DOWN - " + stt.detail
                          : stt.status + " (" + stt.detail + ")"));
    if (stt.engine === "gguf" && stt.status === "down") {
      parts.push("gguf hint: run setup step 7 or set STT_GGUF_SERVER");
    }
    const tts = c.tts || {};
    parts.push("tts " + (tts.status === "up" ? "up (" + tts.detail + ")"
                          : tts.status === "down" ? "DOWN - " + tts.detail
                          : tts.status + " (" + tts.detail + ")"));
    box.textContent = parts.join(" \u00b7 ");
  } catch (e) {
    box.textContent = "status check failed - " + (e && e.message ? e.message : e);
    return;
  }
  try {
    const url = ($("cfg-MCP_URL").value || "").trim();
    if (!url) { parts.push("mcp not configured"); box.textContent = parts.join(" \u00b7 "); return; }
    const m = await fetchJson("/api/mcp/status");
    parts.push(m.ok ? "mcp connected (" + (m.tools || []).length + " tools)"
                    : "mcp unreachable - " + (m.error || "?"));
    box.textContent = parts.join(" \u00b7 ");
  } catch (e) {
    parts.push("mcp probe failed");
    box.textContent = parts.join(" \u00b7 ");
  }
}
$("providercheck").addEventListener("click", refreshProviderStatus);

async function refreshPublishBox() {
  const box = $("pubbox");
  if (!box) return;
  try {
    const d = await fetchJson("/api/publish");
    let text = "mode: " + d.mode + " \u2014 " + d.local_url;
    if (d.mode === "wifi") text += " \u2014 share " + d.lan_url;
    if (d.mode === "public") {
      text += d.url ? " \u2014 public " + d.url : " \u2014 tunnel starting (needs cloudflared)";
    }
    box.textContent = text;
  } catch (e) {
    box.textContent = "status unavailable";
  }
}

$("savebtn").addEventListener("click", () => saveSettings(false));


$("restartbtn").addEventListener("click", () => openModal("restartsheet"));
$("restartno").addEventListener("click", () => closeModal("restartsheet"));
$("restartno2").addEventListener("click", () => closeModal("restartsheet"));
$("restartyes").addEventListener("click", async () => {
  closeModal("restartsheet");
  const btn = $("restartbtn");
  btn.disabled = true;
  toast("Restarting LiveKit + agent...");
  try {
    const r = await fetch("/api/restart", { method: "POST", body: "{}" });
    if (!r.ok) {
      let why = "HTTP " + r.status;
      try { const j = await r.json(); if (j.error) why = j.error; } catch (e) {}
      throw new Error(why);
    }

    const deadline = Date.now() + 90000;
    let back = false;
    while (Date.now() < deadline) {
      await new Promise((res) => setTimeout(res, 2000));
      try {
        const h = await (await fetch("/health")).json();
        if (h.children && h.children.agent === "up") { back = true; break; }
      } catch (e) {  }
    }
    toast(back ? "Restart complete - everything is back up"
               : "Restart sent, but the agent has not come back yet - watch the console");
    checkHealth();
  } catch (e) {
    toast("Restart failed: " + (e && e.message ? e.message : e));
  } finally {
    btn.disabled = false;
  }
});


// ---------- GGUF chat model manager (llama.cpp) ----------

let llmGgufPoll = 0;

function fmtPct(done, total) {
  if (!total) return done ? (done / 1e6).toFixed(0) + " MB" : "";
  return (done / 1e6).toFixed(0) + " / " + (total / 1e6).toFixed(0) + " MB (" +
    Math.min(100, Math.round(done * 100 / total)) + "%)";
}

async function refreshLlmGguf() {
  const sel = $("llmggufmodels"), psel = $("llmggufpresets"), box = $("llmggufbox");
  if (!sel) return;
  try {
    const d = await fetchJson("/api/models/llm");
    sel.innerHTML = "";
    if (!d.models.length) {
      addOpt(sel, "", "(no GGUF models found - download one below)");
    }
    (d.models || []).forEach((m) => {
      addOpt(sel, m.name,
        m.name + " (" + m.size_mb + " MB)" + (m.selected ? "  \u2713 selected" : ""));
    });
    const cur = (d.models || []).find((m) => m.selected);
    if (cur) sel.value = cur.name;
    psel.innerHTML = '<option value="">Pick a model to download...</option>';
    (d.presets || []).forEach((p) => {
      addOpt(psel, p.id, p.id + " (~" + p.size_mb + " MB)" +
        (p.local ? "  \u2713 downloaded" : ""));
    });
    const jobs = d.downloads || {};
    const running = Object.entries(jobs).find(([, j]) => j.status === "running");
    const errJob = Object.entries(jobs).find(([, j]) => j.status === "error");
    let status = "engine: " + (d.engine === "gguf" ? "GGUF selected" :
      "auto (GGUF used when no other LLM answers)") +
      " \u00b7 " + d.dir;
    if (running) status = "downloading " + running[0].replace("llm:", "") +
      " \u2014 " + fmtPct(running[1].done, running[1].total);
    else if (errJob) status = "download FAILED: " + (errJob[1].error || "unknown");
    if (d.binary) status += " \u00b7 llama-server found";
    else status += " \u00b7 llama-server MISSING (setup menu 7 downloads it)";
    if (d.resolve && d.resolve.engine === "gguf") {
      status += " \u00b7 ACTIVE: " + d.resolve.source;
    }
    box.textContent = status;
    clearTimeout(llmGgufPoll);
    if (running) llmGgufPoll = setTimeout(refreshLlmGguf, 1500);
  } catch (e) {
    box.textContent = "status unavailable - " + (e && e.message ? e.message : e);
  }
}

$("llmggufrefresh").addEventListener("click", refreshLlmGguf);

$("llmggufmodels").addEventListener("change", async () => {
  const name = $("llmggufmodels").value;
  if (!name) return;
  try {
    const r = await fetch("/api/models/llm", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "select", name }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || "HTTP " + r.status);
    toast("Model '" + name + "' selected - applies to the next conversation");
    $("llmggufbox").textContent = "selected " + name +
      " - restart (Settings -> Restart) or start a new conversation";
    if ($("cfg-LLM_ENGINE")) { $("cfg-LLM_ENGINE").value = "gguf"; scheduleAutoSave(); }
  } catch (e) {
    toast("Select failed: " + (e && e.message ? e.message : e));
  }
});

$("llmggufdownload").addEventListener("click", async () => {
  const id = $("llmggufpresets").value;
  if (!id) { toast("Pick a model to download first"); return; }
  const btn = $("llmggufdownload");
  btn.disabled = true;
  try {
    const r = await fetch("/api/models/llm", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "download", name: id }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || "HTTP " + r.status);
    if (d.already) {
      toast(d.message || ("Already downloaded: " + (d.file || id)));
      refreshLlmGguf();
      return;
    }
    toast("Downloading " + id + " in the background...");
    refreshLlmGguf();
  } catch (e) {
    toast("Download failed: " + (e && e.message ? e.message : e));
  } finally {
    btn.disabled = false;
  }
});

$("llmggufremove").addEventListener("click", async () => {
  const name = $("llmggufmodels").value;
  if (!name) { toast("No model selected"); return; }
  openModal("confirmremove");
  $("confirmremovetext").textContent =
    "Delete " + name + " from disk? You can re-download it later.";
  const yes = $("confirmremoveyes");
  const handler = async () => {
    yes.removeEventListener("click", handler);
    closeModal("confirmremove");
    try {
      const r = await fetch("/api/models/llm", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "remove", name }),
      });
      const d = await r.json();
      toast(d.ok ? "Removed " + name : "Remove failed: " + d.message);
      refreshLlmGguf();
    } catch (e) {
      toast("Remove failed: " + (e && e.message ? e.message : e));
    }
  };
  yes.addEventListener("click", handler);
});

// ---------- Microphone test ----------

$("mictest").addEventListener("click", async () => {
  const box = $("mictestbox"), btn = $("mictest");
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    box.textContent = "BLOCKED: navigator.mediaDevices is unavailable. " +
      "This page is not a secure context - use http://127.0.0.1:8090 on this " +
      "PC, or public mode (https tunnel) for phones over wifi.";
    return;
  }
  btn.disabled = true;
  const dev = (() => { try { return localStorage.getItem("micDeviceId") || ""; } catch (e) { return ""; } })();
  const caps = {
    echoCancellation: ($("cfg-CAP_ECHO_CANCEL") || {}).value !== "0",
    noiseSuppression: ($("cfg-CAP_NOISE_SUPPRESS") || {}).value !== "0",
    autoGainControl: ($("cfg-CAP_AUTO_GAIN") || {}).value !== "0",
  };
  box.textContent = "requesting microphone...";
  let stream = null;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: Object.assign(caps, dev ? { deviceId: { exact: dev } } : {}),
    });
  } catch (e) {
    box.textContent = "FAILED: " + micErrorText(e);
    btn.disabled = false;
    return;
  }
  let peak = 0;
  try {
    const ctx = getCtx("mic");
    const src = ctx.createMediaStreamSource(stream);
    const an = ctx.createAnalyser();
    an.fftSize = 256;
    src.connect(an);
    const buf = new Uint8Array(an.fftSize);
    const t0 = Date.now();
    await new Promise((done) => {
      const tick = () => {
        an.getByteTimeDomainData(buf);
        for (let i = 0; i < buf.length; i++) {
          peak = Math.max(peak, Math.abs(buf[i] - 128) / 128);
        }
        if (Date.now() - t0 >= 5000) { done(); return; }
        box.textContent = "listening... speak now! level: " +
          Math.round(peak * 100) + "%";
        setTimeout(tick, 120);
      };
      tick();
    });
    const track = stream.getTracks()[0];
    const label = track && track.label ? track.label : "(default device)";
    if (peak > 0.02) {
      box.textContent = "OK - mic works. peak " + Math.round(peak * 100) +
        "% on: " + label + ". If calls still don't hear you, the issue is " +
        "the STT engine, not the mic.";
    } else {
      box.textContent = "Mic opened (" + label + ") but 5s of SILENCE was " +
        "captured. Check: correct input device in system sound settings, " +
        "Windows Settings > Privacy > Microphone (allow desktop apps), " +
        "and the physical mute switch.";
    }
  } catch (e) {
    box.textContent = "test error: " + (e && e.message ? e.message : e);
  } finally {
    try { stream && stream.getTracks().forEach((t) => t.stop()); } catch (e) {}
    btn.disabled = false;
  }
});

// ---------- Module manager (see + remove optional components) ----------

async function scanModules() {
  const list = $("modlist");
  list.textContent = "scanning...";
  let d;
  try {
    d = await fetchJson("/api/modules");
  } catch (e) {
    list.textContent = "scan failed - " + (e && e.message ? e.message : e);
    return;
  }
  list.innerHTML = "";
  if (!d.modules || !d.modules.length) {
    list.textContent = "no modules found";
    return;
  }
  d.modules.forEach((m) => {
    const row = document.createElement("div");
    row.className = "modrow" + (m.installed ? "" : " modoff");
    const st = document.createElement("span");
    st.className = "modstate " + (m.installed ? "on" : "off");
    st.textContent = m.installed ? "installed" : "not installed";
    const name = document.createElement("span");
    name.className = "modname";
    name.textContent = m.name;
    const meta = document.createElement("span");
    meta.className = "modmeta";
    const size = m.size_mb ? " ~" + m.size_mb + " MB" : "";
    meta.textContent = m.uses + (size ? " \u00b7" + size : "") +
      (m.in_use ? " \u00b7 IN USE: " + m.in_use : "") +
      (m.installed ? "" : " \u00b7 reinstall: " + m.reinstall);
    row.appendChild(st); row.appendChild(name); row.appendChild(meta);
    if (m.installed && m.id !== "livekit-server") {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "modbtn";
      btn.textContent = "remove";
      if (m.in_use) btn.title = "In use - switch the engine in Settings first";
      btn.addEventListener("click", () => removeModuleConfirm(m));
      row.appendChild(btn);
    }
    list.appendChild(row);
  });
}

function removeModuleConfirm(m) {
  openModal("confirmremove");
  $("confirmremovetext").textContent = m.in_use
    ? m.name + " is IN USE (" + m.in_use + "). Removing it will break that "
      + "engine until you reinstall or switch. Remove anyway? (the server "
      + "refuses if it would break the active engine)"
    : "Remove " + m.name + " from this installation? Your settings in "
      + ".env.local are kept; you can reinstall via " + m.reinstall + ".";
  const yes = $("confirmremoveyes");
  const handler = async () => {
    yes.removeEventListener("click", handler);
    closeModal("confirmremove");
    $("modlist").textContent = "removing " + m.name + "...";
    try {
      const r = await fetch("/api/modules/remove", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: m.id }),
      });
      const d = await r.json();
      toast(d.ok ? d.message : "Remove failed: " + d.message);
    } catch (e) {
      toast("Remove failed: " + (e && e.message ? e.message : e));
    }
    scanModules();
  };
  yes.addEventListener("click", handler);
}

$("modscan").addEventListener("click", scanModules);

$("confirmremoveno").addEventListener("click", () => closeModal("confirmremove"));
$("confirmremoveno2").addEventListener("click", () => closeModal("confirmremove"));

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
  refreshLlmModels();
}

let modelFetchTimer = 0;
let modelFetchStartedAt = 0;

let lastAutoPick = "";


async function refreshLlmModels() {
  const sel = $("llmmodels"), hint = $("modelhint");
  if (!sel) return;
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

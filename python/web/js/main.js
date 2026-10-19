"use strict";


async function checkHealth() {
  try {
    const r = await fetch("/health");
    if (!r.ok) throw new Error("HTTP " + r.status);
    const h = await r.json();
    const lk = !!(h.livekit && h.livekit.status === "up");
    const llmUp = !!(h.llm && String(h.llm.status || "").startsWith("up"));
    const gpu = String(h.gpu || "").startsWith("YES");
    const sttN = h.models ? Number(h.models.stt) || 0 : 0;
    const ttsN = h.models ? Number(h.models.tts) || 0 : 0;
    const lib = !!(h.lib_file && h.lib_file.exists);
    const ag = !!(h.children && h.children.agent === "up");
    const pub = h.publish || {};
    const el = $("stchips");
    el.innerHTML = chip("livekit", lk, lk ? "up" : "down") + " &middot; " +
      chip("llm", llmUp, llmUp ? (h.llm.kind || "up") : "down") + " &middot; " +
      chip("gpu", gpu, gpu ? "yes" : "no") + " &middot; " +
      chip("stt", sttN > 0, sttN) + " &middot; " +
      chip("tts", ttsN > 0, ttsN) + " &middot; " +
      chip("agent", ag, ag ? "up" : "down") +
      (h.mcp && h.mcp.enabled ? " &middot; " + chip("mcp", true, "on") : "") +
      (pub.mode && pub.mode !== "local" ? " &middot; " + chip("pub", true, pub.mode) : "") +
      (lib ? "" : " &middot; lib missing");
    srvErr = !lk || !ag;
    srvDegraded = !srvErr && !llmUp;
  } catch (e) {
    $("stchips").innerHTML = "server unreachable";
    srvErr = true;
    srvDegraded = false;
  }
}

async function precheck() {
  const fatal = [], warn = [];
  if (!window.LivekitClient) fatal.push("livekit-client.js failed to load - Ctrl+F5 or re-clone the repo");
  let h = null;
  try {
    const r = await fetch("/health");
    if (!r.ok) throw new Error("HTTP " + r.status);
    h = await r.json();
  } catch (e) {
    fatal.push("backend unreachable (" + e.message + ")");
  }
  if (h) {
    if (h.venv === false) fatal.push(".venv missing - run setup.sh (or setup.bat on Windows)");
    if (h.lib_file && !h.lib_file.exists) fatal.push("livekit-client.js missing on server - re-clone the repo");
    if (h.livekit && h.livekit.status !== "up") fatal.push("LiveKit server is down - restart start.sh / start.bat");
    if (h.children && h.children.agent && h.children.agent !== "up") warn.push("agent worker: " + h.children.agent);
    if (h.llm && !String(h.llm.status || "").startsWith("up")) warn.push("LLM provider not resolved yet - set LLM_BASE_URL or an API key in settings");
    if (h.models) {
      if (!Number(h.models.stt)) warn.push("no STT model downloaded yet - run setup (menu 4/7/9) or check settings");
      if (!Number(h.models.tts)) warn.push("no TTS voice downloaded yet - run setup (menu 5) or check settings");
    }
  }
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    fatal.push("microphone API unavailable: this page is NOT a secure context "
      + "(plain http from another device). Use http://127.0.0.1:8090 on this "
      + "PC, or public mode (https) for phones on wifi.");
    return { fatal, warn };
  }
  if (!fatal.length) {
    let caps = { echoCancellation: true, noiseSuppression: true, autoGainControl: true };
    try {
      const cr = await fetch("/api/config");
      if (cr.ok) {
        const c = ((await cr.json()).config) || {};
        caps = {
          echoCancellation: c.CAP_ECHO_CANCEL !== "0",
          noiseSuppression: c.CAP_NOISE_SUPPRESS !== "0",
          autoGainControl: c.CAP_AUTO_GAIN !== "0",
        };
      }
    } catch (e) {}
    let dev;
    try { dev = localStorage.getItem("micDeviceId") || ""; } catch (e) { dev = ""; }
    try {
      const s = await navigator.mediaDevices.getUserMedia({
        audio: Object.assign(caps, dev ? { deviceId: { exact: dev } } : {}),
      });
      s.getTracks().forEach((t) => t.stop());
    } catch (e) {
      const name = e && e.name || "";
      if (name === "NotFoundError" || name === "DevicesNotFoundError") {
        fatal.push("no microphone found on this machine (or the saved input "
          + "device is gone) - pick another input device in Settings > Speech");
      } else if (name === "NotAllowedError" || name === "SecurityError") {
        fatal.push("microphone permission denied. Click the padlock / sliders "
          + "icon at the left of the address bar, set Microphone to Allow, "
          + "then press play again. (Windows Settings > Privacy > Microphone "
          + "must allow desktop apps too.)");
      } else if (name === "NotReadableError" || name === "TrackStartError") {
        fatal.push("the microphone is held by another app (Discord, Zoom, "
          + "OBS, another browser window). Close it or pick another input "
          + "device in Settings > Speech.");
      } else {
        fatal.push(micErrorText(e));
      }
    }
  }
  return { fatal, warn };
}


const sideEl = $("side");
sideOpen = (() => {
  try {
    const saved = localStorage.getItem("panelOpen");
    if (saved === "1" || saved === "0") return saved === "1";
  } catch (e) {}
  return innerWidth >= 1100;
})();

function applySide() {
  sideEl.classList.toggle("hidden", !sideOpen);
  document.body.classList.toggle("panel-open", sideOpen);
  const tab = $("sidetab");
  tab.classList.toggle("open", sideOpen);
  tab.title = sideOpen ? "Hide conversation" : "Show conversation";
  tab.setAttribute("aria-expanded", String(sideOpen));
  try { localStorage.setItem("panelOpen", sideOpen ? "1" : "0"); } catch (e) {}
}

$("sidetab").addEventListener("click", () => {
  sideOpen = !sideOpen;
  applySide();
  if (sideOpen) chatList.scrollBottom();
});


$("gearbtn").addEventListener("click", async () => {
  openModal("settingsheet");
  fillMicDevices();
  try {
    await loadConfigForm();
  } catch (e) {
    toast("Could not load settings: " + (e && e.message ? e.message : e));
  }
  if (typeof refreshPublishBox === "function") refreshPublishBox();
  if (typeof refreshMcpStatus === "function") refreshMcpStatus();
});
$("closegear").addEventListener("click", () => closeModal("settingsheet"));
document.querySelectorAll(".modal").forEach((m) => {


  let downOnBackdrop = false;
  m.addEventListener("mousedown", (ev) => { downOnBackdrop = ev.target === m; });
  m.addEventListener("click", (ev) => {
    if (ev.target === m && downOnBackdrop) closeModal(m.id);
  });
});
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") document.querySelectorAll(".modal.open").forEach((m) => closeModal(m.id));
});


$("settingsheet").addEventListener("modalclose", () => {
  if (typeof saveSettings === "function") saveSettings(true);
});

document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b === btn));
    document.querySelectorAll(".tabpage").forEach((p) => p.classList.toggle("active", p.id === btn.dataset.tab));
  });
});


addEventListener("resize", () => { try { fitCanvas(); applySide(); } catch (e) {} });
addEventListener("orientationchange", () => setTimeout(() => { fitCanvas(); applySide(); }, 250));


function beat() {
  try {
    fetch("/api/heartbeat", { method: "POST", body: "1", keepalive: true }).catch(() => {});
  } catch (e) {}
}
addEventListener("pagehide", () => {
  try {
    if (navigator.sendBeacon) {
      navigator.sendBeacon("/api/window/closing", new Blob(["1"], { type: "text/plain" }));
    }
  } catch (e) {}
});


fitCanvas();
applySide();
draw();
beat();
setInterval(beat, 2500);
checkHealth();
setInterval(checkHealth, 15000);
preloadSavedHistory();

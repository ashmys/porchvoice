"use strict";


const $ = (id) => document.getElementById(id);
const LK = window.LivekitClient;


let room = null, live = false, msgCounter = 0;
let viewingFile = null, currentRoom = "";
let agentCtx = null, micCtx = null, agentAn = null, micAn = null;
let agentData = null, micData = null, rafId = 0;
let micStream = null, stateTimer = 0;
let lastEcho = { text: "", at: 0 };
let micPubOk = false, micSilenceSince = 0, micWarned = false;
let srvErr = false, srvDegraded = false, stCache = "\t\t\t";
let sideOpen = false;

const ICON_PLAY = '<svg viewBox="0 0 24 24" width="34" height="34" fill="currentColor" aria-hidden="true"><path d="M8 5.5v13l10.5-6.5z"/></svg>';
const ICON_STOP = '<svg viewBox="0 0 24 24" width="30" height="30" fill="currentColor" aria-hidden="true"><rect x="6.5" y="6.5" width="11" height="11" rx="2"/></svg>';

function toast(text, ms) {
  const t = $("toast");
  t.textContent = text;
  t.classList.add("show");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.remove("show"), ms || 2600);
}

function setState(text, cls) {
  $("state").textContent = text;
  $("state").className = cls || "";
}

function pulseState(text) {
  if (!live) return;
  setState(text, text === "speaking" ? "speaking" : "");
  clearTimeout(stateTimer);
  stateTimer = setTimeout(() => { if (live) setState("listening"); }, 3000);
}

function micErrorText(e) {
  const n = (e && e.name) || "";
  if (n === "NotAllowedError" || n === "SecurityError") return "microphone permission denied - allow mic in the browser address bar";
  if (n === "NotFoundError" || n === "DevicesNotFoundError") return "no microphone found";
  if (n === "NotReadableError" || n === "TrackStartError") return "microphone is busy (another app is using it)";
  return "microphone error: " + ((e && e.message) || n || "unknown");
}

function chip(name, ok, val) {
  return name + " <b class=\"" + (ok ? "ok" : "bad2") + "\">" + val + "</b>";
}

function openModal(id) { $(id).classList.add("open"); }
function closeModal(id) {
  $(id).classList.remove("open");
  try { $(id).dispatchEvent(new CustomEvent("modalclose")); } catch (e) {}
}

async function fetchJson(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error(url + " -> HTTP " + r.status);
  return r.json();
}

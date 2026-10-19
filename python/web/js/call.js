"use strict";


function micCaps(c) {
  return {
    echoCancellation: c.CAP_ECHO_CANCEL !== "0",
    noiseSuppression: c.CAP_NOISE_SUPPRESS !== "0",
    autoGainControl: c.CAP_AUTO_GAIN !== "0",
  };
}

function micDeviceConstraint() {
  try {
    const dev = localStorage.getItem("micDeviceId") || "";
    return dev ? { exact: dev } : undefined;
  } catch (e) { return undefined; }
}

async function connect() {
  if (!window.LivekitClient) throw new Error("livekit-client.js missing");
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    throw new Error("this page is not a secure context, so the microphone API "
      + "is unavailable. Open http://127.0.0.1:8090 on this PC, or use public "
      + "mode (https tunnel) when calling from another device over wifi.");
  }

  const wantRoom = savedRoom();
  const qs = wantRoom ? "&room=" + encodeURIComponent(wantRoom) : "";
  const r = await fetch("/token?identity=user-" + Math.random().toString(16).slice(2, 6) + qs);
  if (!r.ok) {
    let why = "HTTP " + r.status;
    try { const j = await r.json(); if (j.error) why = j.error; } catch (e) {}
    throw new Error("token request failed: " + why);
  }
  const d = await r.json();
  if (!d.url || !d.token) throw new Error(d.error || "bad token response");
  if ((d.room || "") !== wantRoom) resetTranscript();
  currentRoom = d.room || "";
  storeRoom(currentRoom);


  let caps = { echoCancellation: true, noiseSuppression: true, autoGainControl: true };
  try {
    const cr = await fetch("/api/config");
    if (cr.ok) {
      const c = ((await cr.json()).config) || {};
      caps = micCaps(c);
    }
  } catch (e) {}
  const dev = micDeviceConstraint();
  room = new LK.Room({
    adaptiveStream: false, dynacast: false,
    audioCaptureDefaults: {
      echoCancellation: caps.echoCancellation,
      noiseSuppression: caps.noiseSuppression,
      autoGainControl: caps.autoGainControl,
      deviceId: dev,
    },
  });
  room.on(LK.RoomEvent.DataReceived, (payload, participant, kind, topic) => {
    if (topic === "tool-call") {
      try {
        const m = JSON.parse(new TextDecoder().decode(payload));
        if (m && m.name) {
          addToolNote(m.name, m.arguments, m.output, m.error === true);
          toast("tool: " + m.name, 1800);
        }
      } catch (e) {
        console.warn("bad tool packet", e);
      }
      return;
    }
    if (topic !== "transcript") return;
    try {
      const m = JSON.parse(new TextDecoder().decode(payload));
      if (m && typeof m.text === "string") {
        addBubble(m.who, m.text, m.id, m.final !== false,
                  typeof m.think === "string" ? m.think : undefined,
                  m.think_final === true);
      } else if (m && typeof m.think === "string") {

        addBubble("bot", "", m.id, false, m.think, m.think_final === true);
      }
    } catch (e) {
      console.warn("bad transcript packet", e);
    }
  });
  room.on(LK.RoomEvent.TrackSubscribed, (track) => {
    if (track.kind === "audio") {
      try {
        const el = track.attach();
        el.style.display = "none";
        document.body.appendChild(el);
      } catch (e) {
        console.warn("attach failed", e);
      }
      hookAgentAudio(track);
    }
  });
  room.on(LK.RoomEvent.MediaDevicesError, (e) => {
    toast(micErrorText(e), 4000);
  });
  room.on(LK.RoomEvent.Disconnected, () => {
    if (live) { live = false; setLiveUi(false); toast("Call ended"); }
  });
  try {
    await room.prepareConnection(d.url, d.token);
  } catch (e) {
    console.warn("prepareConnection skipped", e);
  }
  // open the mic BEFORE the room connect: any permission / device error
  // surfaces with a precise message instead of failing silently inside the
  // LiveKit publish path
  try {
    const probe = await navigator.mediaDevices.getUserMedia({
      audio: Object.assign({}, caps, dev ? { deviceId: dev } : {}),
    });
    probe.getTracks().forEach((t) => t.stop());
  } catch (e) {
    throw new Error("microphone blocked before connecting: " + micErrorText(e));
  }
  await room.connect(d.url, d.token);
  try {
    await room.localParticipant.setMicrophoneEnabled(true);
  } catch (e) {
    throw new Error(micErrorText(e));
  }
  const pub = room.localParticipant.getTrackPublication(LK.Track.Source.Microphone);
  if (!pub || !pub.track || !pub.track.mediaStreamTrack) {
    throw new Error("microphone track was not published - stop and start the call again");
  }
  const mst = pub.track.mediaStreamTrack;
  if (mst.readyState !== "live" || pub.track.isMuted) {
    throw new Error("the microphone track ended immediately (" + mst.readyState
      + "). Another app may be holding the mic, or Windows blocked it - "
      + "check Settings > Privacy > Microphone and the input device.");
  }
  micPubOk = true;
  micWarned = false;
  micSilenceSince = 0;
  try {
    micStream = new MediaStream([pub.track.mediaStreamTrack]);
    hookMicAudio(micStream);
  } catch (e) {
    console.warn("mic analyser failed", e);
  }
}

async function startCall() {
  $("callbtn").disabled = true;
  setState("checking");
  let pc;
  try {
    pc = await precheck();
  } catch (e) {
    pc = { fatal: [String(e && e.message || e)], warn: [] };
  }
  if (pc.fatal.length) {
    setState("blocked");
    toast("Cannot start: " + pc.fatal[0] + (pc.fatal.length > 1 ? " (+" + (pc.fatal.length - 1) + " more, see console)" : ""), 4200);
    console.warn("precheck fatal:", pc.fatal, "warnings:", pc.warn);
    $("callbtn").disabled = false;
    return;
  }
  pc.warn.forEach((w) => toast(w, 3600));
  setState("connecting");
  try {
    await connect();
    live = true;
    setLiveUi(true);
    setState("listening");
    toast("Connected - just talk (pause to hand over the turn)");
  } catch (e) {
    console.error(e);
    setState("ready");
    micPubOk = false;
    toast("Could not connect: " + (e && e.message ? e.message : e), 4000);
  }
  $("callbtn").disabled = false;
}

async function stopCall() {
  live = false;
  micPubOk = false;
  micWarned = false;
  clearTimeout(stateTimer);
  try { if (room) await room.disconnect(); } catch (e) { console.warn("disconnect", e); }
  cleanupAudio();
  setLiveUi(false);
  setState("ready");
}

function setLiveUi(on) {
  const b = $("callbtn");
  b.classList.toggle("live", on);
  b.innerHTML = on ? ICON_STOP : ICON_PLAY;
  b.title = on ? "Stop the call" : "Start the call";
  if (!on) { setState("ready"); }
}

$("callbtn").addEventListener("click", () => {
  try { (live ? stopCall() : startCall()); } catch (e) { console.error(e); toast("Button error: " + e.message); }
});

$("chatform").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const input = $("chatinput");
  const text = (input.value || "").trim();
  if (!text) return;
  if (!room || !live) { toast("Start a call first"); return; }
  input.value = "";
  const localId = "loc" + (++msgCounter);
  addBubble("user", text, localId, true);
  lastEcho = { text, at: Date.now() };
  try {
    await room.localParticipant.publishData(
      new TextEncoder().encode(JSON.stringify({ text })),
      { reliable: true, topic: "chat-input" });
  } catch (e) {
    toast("Send failed: " + (e && e.message ? e.message : e));
  }
});

import {
  copy,
  clockText,
  memoryFor,
  shortLine,
  deadlineNote,
  exampleBrief,
} from "./room-core.js";
import { RealtimeFeed } from "./director.js";
import { createPresenterSurface } from "./design-system/components.js";
const $ = (id) => document.getElementById(id),
  el = (tag, text, host) => {
    const n = document.createElement(tag);
    n.textContent = text;
    if (host) host.append(n);
    return n;
  };
const presenterSurface = createPresenterSurface($("hostScreen"));
// Retain isolation for an old test URL, without restoring its spoken controls.
const micTest = new URLSearchParams(location.search).get("mic-test") === "voice-controls";
let config,
  showMaterials = null,
  researchPacketChosen = false,
  researchPacketName = "",
  researchPacketImportError = "",
  researchPacketLoadSequence = 0,
  researchPacketLoading = false,
  brief = null,
  approved = false,
  messages = [],
  researchShelf = [],
  memory = null,
  view = "prep",
  cueLog = [],
  review = null,
  monitorPreview = false;
let live = {
  id: null,
  epoch: 0,
  stream: null,
  tick: null,
  offset: 0,
  start: 0,
  paused: false,
  starting: false,
  showId: null,
  actions: [],
  offline: true,
  inputKind: null,
  nativeThrough: 0,
  rehearsal: false,
  confidence: null,
  realtimeStatus: "off",
  researchPacket: "",
};
let voice = { stream: null, rec: null, timer: null },
  researchBusy = false,
  prepBusy = false;
const now = () =>
  live.id && !live.starting
    ? live.inputKind === "obs" ? live.nativeThrough : live.offset + (performance.now() - live.start) / 1000
    : live.offset;
const notice = (s) => ($("notice").textContent = s);
const selectedOffline = () => $("inputKind").value === "rehearsal" || !$("liveTwitchActions").checked;
const currentOffline = () => live.id || live.paused || live.starting ? live.offline : selectedOffline();
const publicActionWarning = "This brief includes public Twitch actions. Review them before a live show, or start an offline test.";
const director = new RealtimeFeed(api, {
  status: state => {
    if (!live.id) return;
    live.realtimeStatus = state.status;
    if (live.inputKind === "obs" && Number.isFinite(state.through))
      live.nativeThrough = Math.max(live.nativeThrough, state.through);
    const line = state.confidence?.line;
    const previousLine = live.confidence?.line;
    live.confidence = typeof line === "string" && line.trim() && line.length <= 240
      ? { line, kind: state.confidence.kind || "editorial" } : null;
    if (live.confidence && line !== previousLine) {
      cueLog.push({ id: "realtime-" + Date.now(), ...live.confidence, shown_at: now(), feedback: "unrated" });
      cueLog = cueLog.slice(-100);
    }
    if (state.memory) memory = state.memory;
    if (state.actions) live.actions = state.actions;
    const publicCard = state.public?.graphic;
    $("onAirAwareness").textContent = publicCard
      ? `On air · ${publicCard.attribution || "Producer"}: ${shortLine(publicCard.line)}` : "";
    $("onAirAwareness").hidden = !publicCard;
    $("liveStatus").textContent = state.status === "waiting"
      ? "Waiting for OBS. Start Recording in OBS when ready."
      : state.status === "connecting" ? "Connecting Producer…"
      : live.inputKind === "obs" ? "Producer is following the OBS picture and audio mix."
      : "Producer is following the selected feed live.";
    $("sourceStatus").textContent = live.inputKind === "obs"
      ? state.status === "waiting" ? "Waiting for the local OBS feed."
        : state.status === "connecting" ? "OBS input connected · connecting Producer…" : "OBS picture and full audio mix · Realtime"
      : "Selected " + live.inputKind + " feed · Realtime";
    draw();
  },
  fail: message => {
    if (!live.id) return;
    void stop(true);
    notice(message);
  },
  finished: () => { if (live.id) void stop(false); },
});
async function api(path, body) {
  const r = await fetch(
    path,
    body === undefined
      ? { cache: "no-store" }
      : {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Desk": config.csrf,
          },
          body: JSON.stringify(body),
        },
  );
  const data = await r.json();
  if (!r.ok) {
    const error = Error(data.error || "The producer could not complete that request.");
    error.status = r.status;
    throw error;
  }
  return data;
}
function save() {
  if (micTest) return;
  try {
    localStorage.setItem(
      "cue-room-v2",
      JSON.stringify({
        brief,
        approved,
        messages: messages.slice(-24),
        researchShelf,
        ...(researchPacketChosen ? { researchPacket: $("researchPacket").value, researchPacketName } : {}),
        review,
      }),
    );
  } catch {
    notice("Browser storage is unavailable. Export show notes before closing.");
  }
}
function load() {
  if (micTest) return;
  try {
    const s = JSON.parse(localStorage.getItem("cue-room-v2"));
    if (s && typeof s.researchPacket === "string") {
      $("researchPacket").value = s.researchPacket;
      researchPacketChosen = true;
      researchPacketName = typeof s.researchPacketName === "string" ? s.researchPacketName : "";
    }
    if (s?.brief?.intentions?.length) {
      brief = s.brief;
      approved = !!s.approved;
      messages = (s.messages || []).slice(-24);
      researchShelf = (s.researchShelf || []).slice(-12);
      review = s.review;
      memory = memoryFor(brief);
    }
  } catch {}
}
const packetLength = value => [...value].length;
function packetError() {
  return researchPacketImportError || (packetLength($("researchPacket").value) > 32000
    ? "Keep the complete research packet within 32,000 characters. Nothing has been shortened." : "");
}
function packetEditable() {
  return !live.id && !live.paused && !live.starting && !prepBusy;
}
function updatePacketStatus() {
  const error = packetError(), value = $("researchPacket").value;
  $("researchPacket").setAttribute("aria-invalid", String(!!error));
  $("researchPacketStatus").setAttribute("role", error ? "alert" : "status");
  $("researchPacketStatus").textContent = researchPacketLoading ? "Reading research packet…" : error || (value.trim()
    ? `${researchPacketName || "Research packet"} · ${packetLength(value).toLocaleString()} characters ready`
    : "No research packet selected.");
  $("researchPacketSummary").textContent = error ? "Research packet · needs attention"
    : value.trim() ? "Research packet · ready" : "Research packet (optional)";
  updateSourceDisclosure();
}
function updateSourceDisclosure() {
  if (!config) return;
  const demo = $("inputKind").value === "rehearsal";
  const obs = $("inputKind").value === "obs";
  $("obsInputHelp").hidden = !obs;
  $("sourceDisclosure").textContent = (demo
    ? "Local recording with visible simulated chat. OpenAI reads the programme audio and images, including chat shown in the picture. Twitch actions are simulated; nothing connects to Twitch or starts a broadcast."
    : obs ? "Start prepares the local OBS input. When OBS sends the picture and audio mix, Producer sends that feed to OpenAI. "
      + (selectedOffline() ? "Twitch actions are simulated. " : "Live Twitch actions may post to your connected channel. ")
      + "This does not start a Twitch broadcast. The confidence monitor stays private."
    : "Start sends this selected feed to OpenAI. Confirm you can authorize its processing. The monitor is private.")
    + ((showMaterials?.image_count > 0 || showMaterials?.notes_available)
      ? " Your show-folder images and notes are also processed by OpenAI." : "")
    + ($("researchPacket").value.trim() ? " Your research packet is also sent to OpenAI for this show." : "");
}
$("researchPacket").oninput = () => {
  if (!packetEditable()) return;
  researchPacketLoadSequence++;
  researchPacketLoading = false;
  researchPacketChosen = true;
  researchPacketName = "";
  researchPacketImportError = "";
  updatePacketStatus();
  save();
  draw();
};
$("clearResearchPacket").onclick = () => {
  if (!packetEditable()) return;
  researchPacketLoadSequence++;
  researchPacketLoading = false;
  $("researchPacket").value = "";
  $("researchPacketFile").value = "";
  researchPacketChosen = true;
  researchPacketName = researchPacketImportError = "";
  updatePacketStatus();
  save();
  draw();
};
async function readResearchPacket(files) {
  if (!packetEditable() || !files.length) return;
  const sequence = ++researchPacketLoadSequence;
  researchPacketLoading = true;
  updatePacketStatus();
  draw();
  try {
    if (files.length !== 1) throw Error("Choose one complete research packet file.");
    const file = files[0];
    if (!/\.(md|txt)$/i.test(file.name)) throw Error("Choose a Markdown (.md) or text (.txt) research packet.");
    if (file.size > 128000) throw Error("Choose a research packet file up to 128 KB and 32,000 characters.");
    const value = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(await file.arrayBuffer());
    if (packetLength(value) > 32000) throw Error("Keep the complete research packet within 32,000 characters. Nothing has been shortened.");
    if (sequence !== researchPacketLoadSequence || !packetEditable()) return;
    $("researchPacket").value = value;
    researchPacketChosen = true;
    researchPacketName = file.name;
    researchPacketImportError = "";
    save();
  } catch (error) {
    if (sequence !== researchPacketLoadSequence || !packetEditable()) return;
    researchPacketImportError = error instanceof TypeError
      ? "This file is not valid UTF-8 text. Export it as UTF-8 Markdown or text, then try again." : error.message;
  }
  researchPacketLoading = false;
  updatePacketStatus();
  draw();
}
$("researchPacketFile").onchange = () => readResearchPacket($("researchPacketFile").files);
$("researchPacketDrop").ondragover = event => {
  event.preventDefault();
  if (packetEditable()) $("researchPacketDrop").dataset.dragging = "true";
};
$("researchPacketDrop").ondragleave = () => delete $("researchPacketDrop").dataset.dragging;
$("researchPacketDrop").ondrop = event => {
  event.preventDefault();
  delete $("researchPacketDrop").dataset.dragging;
  void readResearchPacket(event.dataTransfer.files);
};
function setView(next) {
  view = next;
  for (const v of ["prep", "monitor", "review"]) {
    $(v + "View").hidden = v !== next;
    $(v === "monitor" ? "monitorTab" : v + "Tab").removeAttribute(
      "aria-current",
    );
  }
  $(next === "monitor" ? "monitorTab" : next + "Tab").setAttribute(
    "aria-current",
    "page",
  );
  $("researchPrepSection").hidden = next !== "prep";
  document.body.classList.remove("presenting");
  monitorPreview = false;
  if (next === "monitor") draw();
  if (next === "review") drawReview();
}
for (const v of ["prep", "monitor", "review"])
  $(v === "monitor" ? "monitorTab" : v + "Tab").onclick = () => setView(v);
function conversation() {
  const root = $("conversation");
  root.replaceChildren();
  if (!messages.length)
    el(
      "p",
      "What would you like your audience to leave with?",
      root,
    ).className = "producerMessage";
  for (const m of messages)
    el("p", m.text, root).className =
      m.role === "user" ? "hostMessage" : "producerMessage";
  root.scrollTop = root.scrollHeight;
}
function showBrief() {
  const root = $("briefReadback");
  root.replaceChildren();
  $("briefEmpty").hidden = !!brief;
  $("approve").hidden = !brief;
  $("editDetails").hidden = !brief;
  $("briefState").textContent = approved ? "Reviewed" : "Not reviewed";
  if (!brief) return;
  el("h3", brief.title, root);
  el("p", brief.goal, root);
  el("small", brief.audience, root);
  for (const i of brief.intentions) {
    const row = el("div", i.text, root);
    row.className = "intent";
    el(
      "small",
      (i.priority === "essential" ? "Protect this" : "Optional") +
        " · " +
        i.kind,
      row,
    );
  }
  if (brief.production) {
    el("p", brief.production.preferences, root).className = "muted";
    for (const p of brief.production.actions || []) {
      const card = el("div", "", root);
      card.className = "intent";
      el("strong", p.kind + ": " + p.text, card);
      el("p", "When: " + p.when, card);
      if (p.kind === "poll")
        el(
          "small",
          p.choices.join(" / ") + " · " + p.duration + " seconds",
          card,
        );
    }
  }
  el(
    "p",
    brief.timing.mode === "hard"
      ? "Hard stop · " + brief.timing.minutes + " minutes"
      : "Flexible timing",
    root,
  );
  $("editTitle").value = brief.title;
  $("editAudience").value = brief.audience;
  $("editGoal").value = brief.goal;
  $("timingMode").value = brief.timing.mode;
  $("timingMinutes").value = brief.timing.minutes;
  $("editRecovery").value = brief.recovery;
  const edit = $("editIntentions");
  edit.replaceChildren();
  brief.intentions.forEach((i) => {
    const row = el("div", "", edit);
    row.className = "intent";
    const input = el("input", "", row);
    input.value = i.text;
    input.maxLength = 160;
    input.setAttribute("aria-label", "Intention " + i.id);
    input.dataset.intent = i.id;
    const select = el("select", "", row);
    select.setAttribute("aria-label", "Priority " + i.id);
    select.dataset.priority = i.id;
    for (const v of ["essential", "optional"]) {
      const o = el("option", v, select);
      o.value = v;
    }
    select.value = i.priority;
  });
  renderResearch();
}
$("notesFile").onchange = async () => {
  const file = $("notesFile").files[0];
  if (!file) return;
  if (file.size > 80000) {
    notice("Choose a shorter notes file (up to 80 KB).");
    return;
  }
  const text = await file.text();
  if (text.length > 20000) {
    notice("Keep the show notes under 20,000 characters.");
    return;
  }
  $("thought").value = text;
  notice("");
};
$("prepForm").onsubmit = async (e) => {
  e.preventDefault();
  if (prepBusy || live.id || live.starting || live.paused) return;
  if (researchPacketLoading) return;
  if (packetError()) {
    $("researchPacketSection").open = true;
    $("researchPacket").focus();
    updatePacketStatus();
    return;
  }
  const notes = $("thought").value.trim();
  if (!notes) {
    notice("Add your show notes first.");
    return;
  }
  prepBusy = true;
  draw();
  $("prepStatus").textContent = "Reading your notes…";
  try {
    const answer = await api("/api/prepare", { notes, runtime: "realtime" });
    brief = answer.brief;
    messages = [{ role: "user", text: notes }];
    // Direct notes Start is the selected demo UX. Real public writes still need a reviewed plan.
    if (
      !selectedOffline() &&
      brief.production?.actions?.length
    ) {
      $("prepStatus").textContent = publicActionWarning;
      document.querySelector(".briefPanel").classList.remove("legacyPrep");
      approved = false;
      showBrief();
      return;
    }
    approved = true;
    memory = memoryFor(brief);
    save();
    $("prepStatus").textContent = "";
    prepBusy = false;
    setView("monitor");
    await $("startShow").onclick();
    for (const request of brief.research || []) void runResearch(request);
  } catch (e) {
    $("prepStatus").textContent = e.message;
  } finally {
    prepBusy = false;
    draw();
  }
};
$("example").onclick = () => {
  if (live.id || live.paused || live.starting) return;
  brief = copy(exampleBrief);
  approved = false;
  memory = memoryFor(brief);
  messages = [
    {
      role: "assistant",
      text: "This is an example brief to explore the controls. Adjust it to your actual show before starting.",
    },
  ];
  conversation();
  showBrief();
  save();
};
$("editForm").onsubmit = (e) => {
  e.preventDefault();
  if (live.id || live.paused || live.starting) return;
  brief.title = $("editTitle").value.trim();
  brief.goal = $("editGoal").value.trim();
  brief.audience = $("editAudience").value.trim();
  brief.recovery = $("editRecovery").value.trim();
  brief.timing = {
    mode: $("timingMode").value,
    minutes: Number($("timingMinutes").value),
  };
  for (const i of brief.intentions) {
    const edited = document
      .querySelector(`[data-intent="${i.id}"]`)
      .value.trim();
    if (edited !== i.text) i.next_action = edited;
    i.text = edited;
    i.priority = document.querySelector(`[data-priority="${i.id}"]`).value;
  }
  approved = false;
  memory = memoryFor(brief);
  showBrief();
  save();
  notice("Edits saved. Review and use this brief when ready.");
};
$("approve").onclick = async () => {
  if (live.id || live.paused || live.starting || prepBusy) return;
  approved = true;
  memory = memoryFor(brief);
  showBrief();
  save();
  setView("monitor");
  for (const request of brief.research || []) {
    if (!researchShelf.some((r) => r.question === request.question))
      await runResearch(request);
  }
};
async function toBase64(blob) {
  return await new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(",")[1]);
    r.onerror = () => reject(Error("Audio could not be read."));
    r.readAsDataURL(blob);
  });
}
function releaseVoice() {
  clearTimeout(voice.timer);
  for (const t of voice.stream?.getTracks() || []) t.stop();
  voice.stream = null;
  $("voice").textContent = "Use microphone";
}
$("voice").onclick = async () => {
  if (voice.rec?.state === "recording") {
    voice.rec.stop();
    return;
  }
  if (prepBusy || live.id) return;
  try {
    voice.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const chunks = [];
    voice.rec = new MediaRecorder(voice.stream, {
      mimeType: "audio/webm;codecs=opus",
    });
    voice.rec.ondataavailable = (e) => {
      if (e.data.size) chunks.push(e.data);
    };
    voice.rec.onstop = async () => {
      releaseVoice();
      $("voice").disabled = true;
      $("voiceState").textContent = "Transcribing with OpenAI…";
      try {
        const data = await api("/api/voice", {
          audio: await toBase64(new Blob(chunks)),
        });
        $("thought").value = [$("thought").value, data.text]
          .filter(Boolean)
          .join("\n");
        $("voiceState").textContent = "Review the words, then send.";
      } catch (e) {
        $("voiceState").textContent = e.message;
      } finally {
        $("voice").disabled = false;
      }
    };
    voice.rec.start();
    $("voice").textContent = "Finish recording";
    $("voiceState").textContent =
      "Recording this microphone · up to 60 seconds";
    voice.timer = setTimeout(() => {
      if (voice.rec.state === "recording") voice.rec.stop();
    }, 60000);
  } catch (e) {
    releaseVoice();
    $("voiceState").textContent = e.message;
  }
};
function memoryList(root, m, editable = false) {
  root.replaceChildren();
  if (!m) {
    el("p", "Your show memory will be here after you prepare.", root);
    return;
  }
  for (const i of m.items) {
    const row = el("div", "", root);
    row.className = "intent";
    row.dataset.state = i.status;
    el("strong", i.text, row);
    el(
      "small",
      i.priority +
        " · " +
        {
          open: "Not covered",
          started: "Started",
          covered: "Covered",
          uncertain: "Needs confirmation",
          skipped: "Skipped today",
        }[i.status],
      row,
    );
    if (i.evidence) el("p", i.evidence, row).className = "evidence";
    if (editable) {
      const actions = el("div", "", row);
      actions.className = "row";
      for (const [label, state] of [
        ["Covered", "covered"],
        ["Skip today", "skipped"],
        ["Reopen", "open"],
      ]) {
        const b = el("button", label, actions);
        b.onclick = () => correct(i.id, state);
      }
    }
  }
}
async function correct(id, state) {
  try {
    if (live.id) {
      const data = await api("/api/intention", {
        id: live.id,
        intention_id: id,
        status: state,
      });
      memory = data.memory;
    } else {
      const i = memory.items.find((i) => i.id === id);
      Object.assign(i, {
        status: state,
        locked: state !== "open",
        evidence: "Confirmed by presenter",
      });
    }
    drawMemory();
    draw();
  } catch (e) {
    notice(e.message);
  }
}
function drawMemory() {
  memoryList($("memoryList"), memory, true);
}
function draw() {
  const time = now(),
    active = !!live.id,
    showOpen = active || live.paused || live.starting,
    offline = showOpen ? live.offline : selectedOffline();
  $("showName").textContent = brief?.title || "Your show";
  $("sessionState").textContent = live.starting
    ? "Connecting"
    : active
      ? live.realtimeStatus === "waiting" ? "Waiting for OBS" : live.realtimeStatus === "connecting" ? "Connecting" : "Listening"
      : live.paused
        ? "Paused"
        : "Ready";
  const hard = brief?.timing?.mode === "hard";
  $("timeLabel").textContent = hard ? "Until hard stop" : "Elapsed";
  $("showClock").textContent = clockText(
    hard ? brief.timing.minutes * 60 - time : time,
  );
  // Both surfaces follow the one director's chosen state. No local editorial
  // arbitration substitutes a question, outline item or old cue for its choice.
  const visibleCue = active ? live.confidence : null;
  const obsWaiting = active && live.inputKind === "obs" && live.realtimeStatus === "waiting";
  const obsConnecting = active && live.inputKind === "obs" && live.realtimeStatus === "connecting";
  presenterSurface.render({
    line: monitorPreview ? "Share the repository" : live.paused ? "Monitoring paused"
      : obsWaiting ? "Start Recording in OBS" : obsConnecting ? "OBS connected. Producer is connecting…" : visibleCue?.line || "",
    kind: visibleCue?.kind || "quiet",
    presence: active && !obsWaiting && !obsConnecting ? "active" : live.paused ? "paused" : "off",
  });
  $("previewLabel").hidden = !monitorPreview;
  const deadline = active ? deadlineNote(brief, time) : "";
  $("deadlineNote").hidden = !deadline;
  $("deadlineNote").textContent = deadline;
  $("focusMode").textContent =
    active || live.paused ? "Show monitor" : "Preview monitor";
  $("startShow").hidden = active;
  $("startShow").disabled =
    !approved || !config?.liveReady || live.starting || prepBusy || researchPacketLoading;
  $("startShow").textContent = live.paused
    ? offline ? "Resume offline test" : "Resume show"
    : offline ? "Start offline test" : "Start show";
  $("sendPrep").textContent = selectedOffline() ? "Start offline test" : "Start show";
  $("pauseShow").hidden = !active;
  $("endShow").hidden = !active && !live.paused && !live.starting;
  $("startDisclosure").hidden = active || live.paused;
  $("liveTwitchActions").disabled = showOpen || $("inputKind").value === "rehearsal";
  $("inputKind").disabled = showOpen;
  $("offlineNotice").hidden = !offline;
  $("offlineSessionNotice").hidden = !offline;
  $("onAirMode").disabled = showOpen;
  $("captionsMode").disabled = showOpen || !$("onAirMode").checked;
  $("sendPrep").disabled = showOpen || prepBusy || researchPacketLoading;
  $("example").disabled = showOpen || prepBusy;
  for (const id of ["researchPacket", "researchPacketFile", "clearResearchPacket"])
    $(id).disabled = showOpen || prepBusy;
  $("approve").disabled = showOpen || prepBusy;
  for (const input of $("editForm").querySelectorAll(
    "input, textarea, select, button",
  ))
    input.disabled = showOpen || prepBusy;
}
$("openDesk").onclick = () => {
  $("deskDrawer").hidden = false;
  drawMemory();
  renderResearch();
};
$("closeDesk").onclick = () => {
  $("deskDrawer").hidden = true;
};
function showMonitor() {
  monitorPreview = !live.id && !live.paused;
  document.body.classList.add("presenting");
  $("hostScreen").tabIndex = -1;
  $("hostScreen").focus({ preventScroll: true });
  draw();
}
$("focusMode").onclick = showMonitor;
$("onAirMode").onchange = draw;
$("liveTwitchActions").onchange = () => {
  if ($("liveTwitchActions").checked) {
    if (brief?.production?.actions?.length) {
      approved = false;
      $("prepStatus").textContent = publicActionWarning;
      document.querySelector(".briefPanel").classList.remove("legacyPrep");
    }
    showBrief();
  }
  updateSourceDisclosure();
  draw();
};
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && document.body.classList.contains("presenting")) {
    e.preventDefault();
    document.body.classList.remove("presenting");
    monitorPreview = false;
    draw();
    $("focusMode").focus();
  }
});
function release() {
  director.stop();
  live.confidence = null;
  live.realtimeStatus = "off";
  clearInterval(live.tick);
  $("onAirAwareness").hidden = true;
  $("onAirAwareness").textContent = "";
  for (const t of live.stream?.getTracks() || []) t.stop();
  live.stream = null;
  $("capturePreview").srcObject = null;
}
async function stop(pause = false) {
  const id = live.id,
    wasPaused = live.paused,
    time = now();
  live.epoch++;
  live.id = null;
  live.starting = false;
  live.offset = time;
  live.paused = pause;
  release();
  try {
    if (id || (!pause && wasPaused && live.showId)) {
      const result = await api("/api/stop", {
        ...(id ? { id } : { show_id: live.showId }),
        reason: pause ? "paused" : "ended",
        through: time,
      });
      if (result.memory) memory = result.memory;
      if (id && result.actions) live.actions = result.actions;
      const acceptedThrough = result.transcript?.through;
      if (Number.isFinite(acceptedThrough)) live.offset = Math.max(time, acceptedThrough);
    }
  } catch (e) {
    notice(e.message);
  }
  $("sourceStatus").textContent = "Capture off.";
  $("liveStatus").textContent = pause
    ? "Paused. Inputs released."
    : "Show ended. Inputs released.";
  if (!pause) {
    review = {
      ended: new Date().toISOString(),
      brief: copy(brief),
      memory: copy(memory),
      cues: copy(cueLog),
      duration: live.offset,
      actions: copy(live.actions),
    };
    save();
    // A subsequent setup starts offline again, even after a reviewed live show.
    $("liveTwitchActions").checked = false;
    updateSourceDisclosure();
    setView("review");
  }
  drawMemory();
  draw();
}
$("startShow").onclick = async () => {
  if (!approved || live.id || live.starting || prepBusy) return;
  if (researchPacketLoading) return;
  if (!live.paused && packetError()) {
    notice(packetError());
    return;
  }
  if (voice.stream) {
    notice("Finish your preparation recording first.");
    return;
  }
  const epoch = ++live.epoch;
  if (!live.paused) {
    live.offline = selectedOffline();
    live.inputKind = $("inputKind").value;
    live.researchPacket = $("researchPacket").value;
  }
  live.starting = true;
  draw();
  notice("");
  try {
    live.rehearsal = live.inputKind === "rehearsal";
    let stream;
    if (live.inputKind === "obs") {
      if (!config.nativeOBS?.configured) throw Error("The local OBS input is not configured. No other source was opened.");
      live.nativeThrough = live.paused ? live.offset : 0;
    } else if (live.rehearsal) {
      if (!config.rehearsal?.ready)
        throw Error("Local rehearsal media is not ready.");
    } else if (live.inputKind === "tab") {
      stream = await navigator.mediaDevices.getDisplayMedia({
        video: { displaySurface: "browser" },
        audio: true,
        preferCurrentTab: false,
      });
      if (
        stream.getVideoTracks()[0]?.getSettings().displaySurface !==
          "browser" ||
        !stream.getAudioTracks().length
      ) {
        stream.getTracks().forEach((t) => t.stop());
        throw Error(
          "Choose a browser tab and enable Share tab audio. No other surface will be captured.",
        );
      }
    } else
      stream = await navigator.mediaDevices.getUserMedia({
        audio: true,
        video: live.inputKind !== "microphone",
      });
    if (epoch !== live.epoch) {
      stream?.getTracks().forEach((t) => t.stop());
      return;
    }
    live.stream = stream;
    if (stream) {
      $("capturePreview").srcObject = stream;
      await $("capturePreview").play();
    }
    if (epoch !== live.epoch) return;
    if (!live.paused) {
      live.offset = 0;
      memory = memoryFor(brief);
      cueLog = [];
      live.showId = crypto.randomUUID();
      live.actions = [];
    }
    const data = await api("/api/start", {
      brief,
      notes: $("thought").value.trim(),
      research_packet: live.researchPacket,
      runtime: "realtime",
      offset: live.offset,
      memory,
      show_id: live.showId,
      research: researchShelf,
      rehearsal: live.rehearsal ? "office-hours" : null,
      input_type: live.inputKind,
      offline: live.offline,
      on_air: $("onAirMode").checked,
      captions: $("onAirMode").checked && $("captionsMode").checked,
    });
    if (epoch !== live.epoch) {
      await api("/api/stop", { id: data.id });
      return;
    }
    live.id = data.id;
    live.showId = data.show_id || live.showId;
    memory = data.memory;
    live.confidence = null;
    live.realtimeStatus = "connecting";
    // Realtime owns both screens, audience judgment and optional subtitle text.
    await director.start(stream, live.id, {
      rehearsal: live.rehearsal,
      nativeOBS: live.inputKind === "obs",
      offset: live.offset,
      duration: config.rehearsal?.duration,
      video: $("capturePreview"),
    });
    if (epoch !== live.epoch) return;
    live.start = performance.now();
    live.paused = false;
    live.starting = false;
    showMonitor();
    if (data.production_issues?.length)
      $("twitchStatus").textContent = data.production_issues.join(" ");
    for (const t of stream?.getTracks() || [])
      t.onended = () => {
        if (epoch === live.epoch) {
          void stop(true);
          notice("The selected input ended. Monitoring is paused.");
        }
      };
    live.tick = setInterval(draw, 250);
    if (live.inputKind !== "obs") $("liveStatus").textContent = "Producer is following the selected feed live.";
    drawMemory();
    draw();
  } catch (e) {
    if (epoch === live.epoch) {
      if (live.id) await stop(true);
      else {
        live.starting = false;
        release();
      }
      notice(e.message);
      draw();
    }
  }
};
$("pauseShow").onclick = () => void stop(true);
$("endShow").onclick = () => void stop(false);
function researchCards(root) {
  root.replaceChildren();
  const requests = (brief?.research || []).filter(
    (q) => !researchShelf.some((r) => r.question === q.question),
  );
  for (const r of requests) {
    const card = el("div", "", root);
    card.className = "researchCard";
    el("strong", r.question, card);
    el("p", r.purpose, card);
    const b = el("button", "Research this", card);
    b.disabled = researchBusy;
    b.onclick = () => void runResearch(r);
  }
  for (const r of researchShelf) {
    const card = el("article", "", root);
    card.className = "researchCard";
    el("strong", r.question, card);
    el("p", r.purpose, card).className = "muted";
    el(
      "small",
      r.status === "ready"
        ? "Sources returned · review before using"
        : "Unverified · no usable source citation",
      card,
    );
    renderResearchText(card, r);
    const links = el("div", "", card);
    links.className = "sourceLinks";
    for (const s of r.sources || []) {
      const a = el("a", s.title, links);
      if (s.url.startsWith("https://")) {
        a.href = s.url;
        a.target = "_blank";
        a.rel = "noopener noreferrer";
      }
    }
  }
  if (!requests.length && !researchShelf.length)
    el("p", "Research requests and findings will stay here.", root).className =
      "muted";
}
function renderResearchText(card, r) {
  const p = el("p", "", card),
    text = r.answer || "";
  const link = (label, url) => {
    const a = el("a", label, p);
    a.href = url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
  };
  const plain = (part) => {
    const re = /\[([^\]]+)\]\((https:\/\/[^\s)]+)\)/g;
    let end = 0;
    for (const match of part.matchAll(re)) {
      p.append(document.createTextNode(part.slice(end, match.index)));
      link(match[1], match[2]);
      end = match.index + match[0].length;
    }
    p.append(
      document.createTextNode(part.slice(end).replace(/cite[^]+/g, "")),
    );
  };
  let end = 0;
  for (const [n, source] of (r.sources || [])
    .filter(
      (s) =>
        Number.isInteger(s.start) &&
        Number.isInteger(s.end) &&
        s.start >= 0 &&
        s.end > s.start &&
        s.end <= text.length &&
        s.url.startsWith("https://"),
    )
    .sort((a, b) => a.start - b.start)
    .entries()) {
    if (source.start < end) continue;
    plain(text.slice(end, source.start));
    const cited = text.slice(source.start, source.end);
    if (!cited.includes("cite") && !cited.startsWith("[")) plain(cited);
    link(" [" + (n + 1) + "]", source.url);
    end = source.end;
  }
  plain(text.slice(end));
}
function renderResearch() {
  for (const id of ["researchPrep", "researchLive", "researchReview"])
    researchCards($(id));
}
async function runResearch(request) {
  if (researchBusy) return;
  researchBusy = true;
  $("researchSubmit").disabled = true;
  notice("Researching. Your monitor will stay quiet when the finding arrives.");
  renderResearch();
  try {
    const r = await api("/api/research", request);
    researchShelf = [
      ...researchShelf.filter((x) => x.question !== r.question),
      r,
    ].slice(-12);
    if (live.id)
      await api("/api/production/research", {
        id: live.id,
        research: researchShelf,
      });
    save();
    notice("Research is ready on the shelf.");
  } catch (e) {
    notice(e.message);
  } finally {
    researchBusy = false;
    $("researchSubmit").disabled = false;
    renderResearch();
  }
}
$("researchForm").onsubmit = (e) => {
  e.preventDefault();
  const question = $("researchQuestion").value.trim(),
    purpose = $("researchPurpose").value.trim() || "Background for this show.";
  if (question) void runResearch({ question, purpose });
};
$("checkTwitch").onclick = async () => {
  try {
    const offline = currentOffline();
    const t = await api("/api/twitch", { offline });
    $("twitchStatus").textContent = offline ? "Simulated Twitch" :
      "@" +
      t.channel +
      " · " +
      (t.online ? "Live" : "Offline") +
      " · " +
      t.title;
  } catch (e) {
    $("twitchStatus").textContent = e.message;
  }
};
function drawReview() {
  memoryList($("reviewMemory"), review?.memory || memory);
  const root = $("reviewCues");
  root.replaceChildren();
  for (const c of review?.cues || []) {
    const row = el("div", "", root);
    row.className = "intent";
    el("p", clockText(c.shown_at) + " · " + c.line, row);
    const select = el("select", "", row);
    select.setAttribute("aria-label", "Feedback for " + c.line);
    for (const [v, label] of [
      ["unrated", "Not rated"],
      ["useful", "Useful"],
      ["distracting", "Distracting"],
      ["late", "Too late"],
      ["wrong", "Wrong"],
    ]) {
      const o = el("option", label, select);
      o.value = v;
    }
    select.value = c.feedback;
    select.onchange = () => {
      c.feedback = select.value;
      save();
    };
  }
  if (!review)
    el(
      "p",
      "End a show to review its cues. Unrated is not the same as unhelpful.",
      root,
    ).className = "muted";
  renderResearch();
}
$("exportReview").onclick = () => {
  const data = { brief, review, research: researchShelf };
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }),
  );
  const a = el("a", "");
  a.href = url;
  a.download = "producer-show-notes.json";
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
};
$("connectChat").onclick = () => notice("Producer reads chat visible in the selected programme picture. No chat API connection is needed.");
function drawProduction() {
  const root = $("productionHistory");
  root.replaceChildren();
  for (const a of live.actions) {
    const row = el("div", "", root);
    el("strong", a.kind + " · " + a.state.replaceAll("_", " "), row);
    el("p", a.text, row);
    el("small", a.detail, row);
  }
}
window.addEventListener("pagehide", () => {
  live.epoch++;
  release();
  releaseVoice();
  if (live.id)
    void fetch("/api/stop", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Desk": config.csrf },
      body: JSON.stringify({ id: live.id }),
      keepalive: true,
    });
});
try {
  config = await api("/api/room");
  $("obsInputOption").hidden = !config.nativeOBS?.configured;
  $("obsInputOption").disabled = !config.nativeOBS?.configured;
  if (config.nativeOBS?.configured) $("inputKind").value = "obs";
  // Subtitle display is a deliberate per-show choice, never a restored default.
  $("captionsMode").checked = false;
  $("liveTwitchActions").checked = false;
  $("onAirMode").checked = true;
  $("chatStatus").textContent = config.audienceInput === "screen"
    ? "Producer reads chat visible in the programme picture. No Twitch chat API."
    : "Historical fixture audience.";
  load();
  $("prepForm").insertBefore($("startDisclosure"), $("sendPrep").parentElement);
  $("thought").value =
    messages.filter((m) => m.role === "user").at(-1)?.text || "";
  if (
    !micTest && new URLSearchParams(location.search).get("rehearsal") === "office-hours"
  ) {
    $("thought").value = config.rehearsal.notes;
    $("inputKind").value = "rehearsal";
    $("onAirMode").checked = true;
    $("captionsMode").checked = false;
  }
  if (micTest) {
    $("inputKind").value = "microphone";
  }
  try {
    showMaterials = await api("/api/show-materials");
    if (!researchPacketChosen && typeof showMaterials.research_packet === "string") {
      $("researchPacket").value = showMaterials.research_packet;
      researchPacketName = typeof showMaterials.research_packet_name === "string" ? showMaterials.research_packet_name : "";
    }
    if (typeof showMaterials.folder === "string" && showMaterials.folder.trim()) {
      const imageCount = Number.isInteger(showMaterials.image_count) && showMaterials.image_count >= 0
        ? showMaterials.image_count : 0;
      $("showMaterialsStatus").textContent = `Show folder: ${imageCount} ${imageCount === 1 ? "image" : "images"} ready`;
      $("showMaterialsStatus").hidden = false;
      $("showFolderPath").textContent = showMaterials.folder;
      $("showFolderPath").hidden = false;
    }
    if (!$("thought").value.trim() && !micTest
      && new URLSearchParams(location.search).get("rehearsal") !== "office-hours"
      && typeof showMaterials.notes === "string")
      $("thought").value = showMaterials.notes.slice(0, 20000);
  } catch (error) {
    // Show notes still work when this helper has no material-folder endpoint.
    showMaterials = null;
    if (error.status !== 404)
      researchPacketImportError = "The show folder could not be loaded. " + error.message;
  }
  $("inputKind").onchange = () => {
    const demo = $("inputKind").value === "rehearsal";
    if (demo) $("liveTwitchActions").checked = false;
    updateSourceDisclosure();
    draw();
  };
  updatePacketStatus();
  $("inputKind").onchange();
  conversation();
  showBrief();
  drawMemory();
  draw();
  drawReview();
  if (new URLSearchParams(location.search).get("preview") === "1") {
    setView("monitor");
    showMonitor();
  }
} catch (e) {
  notice(e.message);
}

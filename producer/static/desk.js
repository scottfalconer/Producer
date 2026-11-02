import { formatClock, schedule, activeCue, parseRundown } from "./desk-core.js";
const $ = (id) => document.getElementById(id);
const video = $("video"),
  cam = $("camera");
let config,
  mode = "replay",
  clock = 0,
  revision = 0,
  rows = [],
  dismissed = new Set(),
  current = null,
  lastRequest = -1,
  latestCut = null;
let draft,
  q = 0,
  reviewing = false;
const fields = ["title", "audience", "goal", "rundown", "context", "tip"];
const questions = [
  "What shall we call the show?",
  "Who is in the room, and what do they already know?",
  "What should someone be able to explain or do when the show ends?",
  "What is the running order? Give each beat minutes and a name.",
  "What news, source or context should be ready? Include a date and link if relevant.",
  "What would you like the producer to remind you of?",
];
const status = (s) => ($("status").textContent = s);
const copy = (x) => structuredClone(x);
async function request(path, body) {
  const response = await fetch(
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
  const data = await response.json();
  if (!response.ok) throw Error(data.error || "Desk request failed.");
  return data;
}
const append = (host, tag, text) => {
  const el = document.createElement(tag);
  el.textContent = text;
  host.append(el);
  return el;
};
const textBrief = (b, field) =>
  field === "rundown"
    ? b.rundown.map((x) => x.minutes + " – " + x.name).join("\n")
    : b[field];
function renderNotes() {
  $("notes").replaceChildren();
  for (const key of fields) {
    append(
      $("notes"),
      "strong",
      {
        goal: "Audience promise",
        title: "Show",
        audience: "Audience",
        context: "Source notes",
        tip: "Presenter tip",
        rundown: "Running order",
      }[key],
    );
    append($("notes"), "p", textBrief(draft, key).replaceAll("\n", " · "));
  }
  $("dialogue").replaceChildren();
  if (q) append($("dialogue"), "p", textBrief(draft, fields[q - 1]));
  $("question").textContent = reviewing
    ? "Your show notes are ready."
    : questions[q];
  $("answer").hidden = reviewing;
  $("answer").value = textBrief(draft, fields[q]);
  $("qCount").textContent = reviewing ? "REVIEW" : q + 1 + " / 6";
  $("answerQ").textContent = reviewing
    ? "Use this brief for live"
    : q === 5
      ? "Review show brief"
      : "Continue";
  $("previousQ").disabled = !reviewing && q === 0;
}
$("answerQ").onclick = () => {
  if (reviewing) {
    try {
      localStorage.setItem("cue-room-brief", JSON.stringify(draft));
    } catch {}
    $("briefMsg").textContent = "Saved on this device.";
    switchMode("live");
    return;
  }
  try {
    const field = fields[q],
      value = $("answer").value.trim();
    if (!value)
      throw Error("Give a short answer, or enter “None” for source notes.");
    draft[field] = field === "rundown" ? parseRundown(value) : value;
    if (q === 5) reviewing = true;
    else q++;
    $("briefMsg").textContent = "";
    renderNotes();
    if (!reviewing) $("answer").focus();
  } catch (e) {
    $("briefMsg").textContent = e.message;
  }
};
$("previousQ").onclick = () => {
  if (reviewing) reviewing = false;
  else q = Math.max(0, q - 1);
  renderNotes();
};
$("download").onclick = () => {
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(draft, null, 2)], { type: "application/json" }),
  );
  const a = document.createElement("a");
  a.download = "show-brief.json";
  a.href = url;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
};
function draw() {
  if (!config || mode === "brief") return;
  const b = mode === "replay" ? config.brief : draft,
    beats = schedule(b),
    out = beats.at(-1).end;
  const planned = beats.findIndex((x) => clock < x.end);
  const idx =
    mode === "live" && live.manual !== null
      ? live.manual
      : planned < 0
        ? beats.length - 1
        : planned;
  const beat = beats[idx];
  $("showTitle").textContent = b.title;
  $("clock").textContent = formatClock(clock);
  $("remaining").textContent = formatClock(out - clock);
  $("beatLeft").textContent = formatClock(beat.end - clock);
  $("pace").textContent =
    mode === "live" && live.manual !== null
      ? clock > beat.end
        ? "OVER PLAN"
        : "HOST MARKED"
      : "PLANNED";
  if ($("rundown").dataset.key !== JSON.stringify([b.rundown, idx])) {
    $("rundown").replaceChildren();
    beats.forEach((x, i) => {
      const li = document.createElement("li");
      li.className = i === idx ? "current" : "";
      append(li, "time", formatClock(x.start));
      append(li, "span", x.name);
      $("rundown").append(li);
    });
    $("rundown").dataset.key = JSON.stringify([b.rundown, idx]);
  }
  let cues = rows;
  if (mode === "live" && live.id) {
    const line =
      idx === beats.length - 1
        ? "Land the takeaway. Thirty seconds to out."
        : "Finish the thought. Next: " + beats[idx + 1].name;
    cues = [
      ...rows,
      {
        id: "time-" + idx,
        kind: "timing",
        line,
        evidence: "Planned beat ends at " + formatClock(beat.end) + ".",
        at: beat.end - 30,
        until: beat.end,
      },
    ];
  }
  current = activeCue(cues, clock, dismissed);
  const kind = current?.kind;
  $("cueCard").dataset.kind = kind || "quiet";
  $("cueKind").textContent = kind
    ? {
        editorial: "EDITORIAL CALL",
        signal: "CHECK SIGNAL",
        timing: "TIME CALL",
      }[kind]
    : mode === "live" && !live.id
      ? live.paused
        ? "PAUSED"
        : "READY"
      : "NO ACTIVE CUE";
  $("cueLine").textContent =
    current?.line ||
    (mode === "live" && !live.id
      ? live.paused
        ? "Show paused."
        : "Ready when you are."
      : "No cue right now.");
  $("evidence").textContent = current
    ? kind === "editorial"
      ? "Heard · “" + current.evidence + "”"
      : current.evidence
    : mode === "live" && !live.id
      ? "Camera and microphone are off."
      : "Keep going. A cue appears here when there is something to act on.";
  $("dismiss").hidden = !current;
}
function history() {
  const h = $("history");
  h.replaceChildren();
  const visible = rows
    .filter((x) => x.at <= clock)
    .slice(-10)
    .reverse();
  if (!visible.length) append(h, "p", "No calls yet.");
  for (const c of visible) {
    if (mode === "replay") {
      const b = append(h, "button", formatClock(c.at) + " · " + c.line);
      b.onclick = () => seek(c.at + 0.02);
    } else append(h, "p", formatClock(c.at) + " · " + c.line);
  }
}
async function refresh(t, force = false) {
  if (mode !== "replay") return;
  clock = t;
  draw();
  $("seek").value = t;
  const bucket = Math.floor(t * 2);
  if (!force && bucket === lastRequest) return;
  lastRequest = bucket;
  const rev = ++revision;
  try {
    const state = await request("/api/replay?at=" + t.toFixed(3));
    if (rev !== revision || mode !== "replay") return;
    rows = state.eligible;
    latestCut = state.observation;
    draw();
    history();
    status(
      (config.models.length ? "OpenAI replay" : "Signal-only replay") +
        " · Retrospective show plan · 45 s windows + measured processing · " +
        (latestCut
          ? "Evidence cutoff " +
            formatClock(latestCut.cutoff ?? latestCut.end) +
            " · " +
            latestCut.outcome
          : "Waiting for the first completed cut."),
    );
  } catch (e) {
    if (rev === revision) {
      rows = [];
      draw();
      status(e.message);
    }
  }
}
function seek(t) {
  revision++;
  rows = [];
  dismissed.clear();
  const at = Math.max(0, Math.min(config.duration, t));
  clock = at;
  draw();
  history();
  video.currentTime = at;
  void refresh(at, true);
}
video.ontimeupdate = () => void refresh(video.currentTime);
video.onseeked = () => void refresh(video.currentTime, true);
video.onplay = () => ($("play").textContent = "Pause replay");
video.onpause = () =>
  ($("play").textContent = video.ended ? "Replay from start" : "Play replay");
video.onended = () => {
  $("play").textContent = "Replay from start";
  void refresh(config.duration, true);
};
$("play").onclick = async () => {
  if (!video.paused) {
    video.pause();
    return;
  }
  if (video.ended) seek(0);
  try {
    await video.play();
  } catch (e) {
    status("Playback unavailable. Check the local review file.");
  }
};
$("seek").oninput = (e) => seek(Number(e.target.value));
$("back").onclick = () => seek(clock - 10);
$("speed").onchange = (e) => (video.playbackRate = Number(e.target.value));
$("nextCue").onclick = async () => {
  const index = $("index");
  index.hidden = !index.hidden;
  if (index.hidden) return;
  index.replaceChildren();
  append(
    index,
    "p",
    "Retrospective index · includes later calls. Select one to review its evidence.",
  );
  try {
    for (const cue of await request("/api/review-index")) {
      const button = append(
        index,
        "button",
        formatClock(cue.at) + " · " + cue.line,
      );
      button.onclick = () => {
        index.hidden = true;
        seek(cue.at + 0.02);
      };
    }
  } catch (e) {
    status(e.message);
  }
};
$("dismiss").onclick = () => {
  if (current) dismissed.add(current.id);
  draw();
};
$("fullscreen").onclick = async () => {
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await document.documentElement.requestFullscreen();
  } catch {
    status("Full screen is unavailable in this browser.");
  }
};
$("twitch").onclick = async () => {
  const button = $("twitch");
  button.disabled = true;
  try {
    const t = await request("/api/twitch", {});
    $("twitchState").textContent =
      "@" +
      t.channel +
      " · " +
      (t.online ? "Live · " + t.viewers + " viewers" : "Offline") +
      " · " +
      t.title +
      " · " +
      t.category +
      " · checked " +
      new Date(t.checked * 1000).toLocaleTimeString();
  } catch (e) {
    $("twitchState").textContent = e.message;
  } finally {
    button.disabled = false;
  }
};
let live = {
  epoch: 0,
  id: null,
  stream: null,
  record: null,
  start: 0,
  offset: 0,
  manual: null,
  paused: false,
  upload: false,
  timer: null,
  interval: null,
  lastCut: 0,
};
const liveTime = () =>
  live.id ? live.offset + (performance.now() - live.start) / 1000 : live.offset;
const b64 = async (blob) =>
  await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",")[1]);
    reader.onerror = () => reject(Error("Media encoding failed."));
    reader.readAsDataURL(blob);
  });
function release() {
  clearTimeout(live.timer);
  clearInterval(live.interval);
  if (live.record?.state === "recording") live.record.stop();
  for (const track of live.stream?.getTracks() || []) track.stop();
  live.stream = null;
  cam.srcObject = null;
}
async function halt(pause = false) {
  const at = liveTime(),
    sid = live.id;
  ++live.epoch;
  live.id = null;
  live.offset = pause ? at : 0;
  live.paused = pause;
  live.upload = false;
  release();
  rows = [];
  dismissed.clear();
  clock = live.offset;
  if (!pause) live.manual = null;
  $("startLive").disabled = true;
  $("pauseLive").disabled = true;
  $("stopLive").disabled = !pause;
  $("takeBeat").disabled = true;
  try {
    if (sid) await request("/api/stop", { id: sid });
  } catch (e) {
    status(e.message);
  }
  $("startLive").disabled = !config?.liveReady;
  $("startLive").textContent = pause
    ? "Resume live with OpenAI"
    : "Start live with OpenAI";
  draw();
  history();
}
function recordCut(epoch) {
  if (epoch !== live.epoch || !live.id) return;
  const rec = new MediaRecorder(new MediaStream(live.stream.getAudioTracks()), {
    mimeType: "audio/webm;codecs=opus",
  });
  live.record = rec;
  const chunks = [],
    start = liveTime();
  rec.ondataavailable = (e) => {
    if (e.data.size) chunks.push(e.data);
  };
  rec.onerror = () => {
    if (epoch === live.epoch) {
      void halt(true);
      status("Recorder interrupted. Live inputs released.");
    }
  };
  rec.onstop = async () => {
    if (epoch !== live.epoch || !live.id) return;
    const canvas = document.createElement("canvas");
    canvas.width = 640;
    canvas.height = Math.max(
      1,
      Math.round((640 * (cam.videoHeight || 360)) / (cam.videoWidth || 640)),
    );
    canvas.getContext("2d").drawImage(cam, 0, 0, canvas.width, canvas.height);
    const frame = canvas.toDataURL("image/jpeg", 0.65).split(",")[1];
    const end = liveTime();
    recordCut(epoch);
    if (live.upload) {
      status("Producer is busy. This cut was skipped.");
      return;
    }
    live.upload = true;
    try {
      const audio = await b64(new Blob(chunks));
      if (epoch !== live.epoch) return;
      const row = await request("/api/window", {
        id: live.id,
        start,
        end,
        audio,
        frame,
      });
      if (epoch !== live.epoch) return;
      const at = liveTime();
      if (row.cue && at - end <= 20)
        rows.push({ id: "live-" + end, at, until: at + 18, ...row.cue });
      rows = rows.slice(-12);
      history();
      draw();
      status(
        "Last cut · " +
          formatClock(end) +
          " · " +
          (at - end > 20 ? "Late result expired" : row.outcome),
      );
    } catch (e) {
      if (epoch === live.epoch) {
        await halt(true);
        status(e.message);
      }
    } finally {
      if (epoch === live.epoch) live.upload = false;
    }
  };
  rec.start();
  live.timer = setTimeout(() => {
    if (rec.state === "recording") rec.stop();
  }, 15000);
}
$("startLive").onclick = async () => {
  const epoch = ++live.epoch;
  $("startLive").disabled = true;
  let sid = null;
  try {
    if (!MediaRecorder.isTypeSupported("audio/webm;codecs=opus"))
      throw Error("Use Chrome with Opus recording support.");
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: true,
      video: true,
    });
    if (epoch !== live.epoch) {
      stream.getTracks().forEach((t) => t.stop());
      return;
    }
    live.stream = stream;
    cam.srcObject = stream;
    await cam.play();
    if (epoch !== live.epoch) return;
    const answer = await request("/api/start", {
      brief: draft,
      offset: live.offset,
    });
    sid = answer.id;
    if (epoch !== live.epoch) {
      await request("/api/stop", { id: sid });
      return;
    }
    live.id = sid;
    live.start = performance.now();
    live.paused = false;
    for (const track of stream.getTracks())
      track.onended = () => {
        if (epoch === live.epoch) {
          void halt(true);
          status("Selected input ended. Show paused.");
        }
      };
    recordCut(epoch);
    live.interval = setInterval(() => {
      clock = liveTime();
      draw();
    }, 250);
    $("pauseLive").disabled = false;
    $("stopLive").disabled = false;
    $("takeBeat").disabled = false;
    status("Private live session · OpenAI receives completed cuts.");
  } catch (e) {
    if (epoch === live.epoch) {
      await halt(true);
      status(e.message);
    }
  }
};
$("pauseLive").onclick = async () => {
  await halt(true);
  status("Paused. Selected inputs released.");
};
$("stopLive").onclick = async () => {
  await halt(false);
  status("Show ended. Selected inputs released.");
};
$("takeBeat").onclick = () => {
  const beats = schedule(draft);
  const planned = Math.max(
    0,
    beats.findIndex((b) => clock < b.end),
  );
  live.manual = Math.min(beats.length - 1, (live.manual ?? planned) + 1);
  draw();
};
window.addEventListener("pagehide", () => {
  if (live.id)
    void fetch("/api/stop", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Desk": config.csrf },
      body: JSON.stringify({ id: live.id }),
      keepalive: true,
    });
  live.epoch++;
  release();
});
async function switchMode(next) {
  if (!config || next === mode) return;
  const before = mode;
  mode = next;
  revision++;
  lastRequest = -1;
  rows = [];
  dismissed.clear();
  latestCut = null;
  video.pause();
  $("index").hidden = true;
  if (before === "live") await halt(false);
  $("brief").hidden = next !== "brief";
  $("desk").hidden = next === "brief";
  for (const [id, key] of [
    ["replayTab", "replay"],
    ["briefTab", "brief"],
    ["liveTab", "live"],
  ]) {
    $(id).removeAttribute("aria-current");
    if (next === key) $(id).setAttribute("aria-current", "page");
  }
  $("replayTools").hidden = next !== "replay";
  $("liveSource").hidden = next !== "live";
  $("presenterTag").textContent =
    next === "live" ? "LIVE VIEW" : "REPLAY PREVIEW";
  $("modeExplanation").textContent =
    next === "live"
      ? "Your private presenter screen. The cue, clocks and rundown below are what you use during the show. Capture begins only when you press Start."
      : "Preview of the private presenter screen, using saved results from your recording. The rundown was reconstructed afterward and cue timing is simulated.";
  $("rundownTitle").textContent =
    next === "live" ? "Your rundown" : "Example rundown";
  $("rundownNote").textContent =
    next === "live"
      ? "From your show brief. Times are planned; use Take next beat to mark your progress."
      : "Reconstructed for this replay. Live uses the brief you review before the show.";
  $("liveControls").hidden = next !== "live";
  video.hidden = next !== "replay";
  cam.hidden = next !== "live";
  $("modeLabel").textContent =
    next === "live" ? "PRIVATE LIVE DESK" : "RECORDING REPLAY · TEST VIEW";
  $("sourceTag").textContent = next === "live" ? "LOCAL" : "REPLAY";
  $("sourceTitle").textContent =
    next === "live" ? "Presenter feed" : "Selected recording";
  $("sourceDetail").textContent =
    next === "live"
      ? "Camera and microphone selected at Start"
      : "Scott’s private review copy";
  if (next === "brief") {
    renderNotes();
    return;
  }
  if (next === "replay") {
    await refresh(video.currentTime, true);
  } else {
    clock = 0;
    draw();
    history();
    $("startLive").disabled = !config.liveReady;
    status(
      config.liveReady
        ? "Ready for an explicit Start."
        : "Live OpenAI processing requires --key-file at launch.",
    );
  }
}
for (const [id, k] of [
  ["replayTab", "replay"],
  ["briefTab", "brief"],
  ["liveTab", "live"],
])
  $(id).onclick = () => void switchMode(k);
try {
  config = await request("/api/desk");
  draft = copy(config.brief);
  try {
    const saved = JSON.parse(localStorage.getItem("cue-room-brief"));
    if (
      saved?.rundown?.length &&
      fields.every((k) => k === "rundown" || typeof saved[k] === "string") &&
      parseRundown(textBrief(saved, "rundown")).length
    )
      draft = saved;
  } catch {}
  $("seek").max = config.duration;
  $("duration").textContent = formatClock(config.duration);
  renderNotes();
  const linked = Number(new URLSearchParams(location.search).get("at") || 0);
  if (Number.isFinite(linked) && linked > 0) {
    seek(linked);
  } else {
    await refresh(0, true);
  }
} catch (e) {
  status(e.message);
}

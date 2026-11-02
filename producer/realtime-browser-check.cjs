const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const http = require("node:http");

// This is an isolated browser fixture: generated media and intercepted APIs only.
(async () => {
  const browser = await chromium.launch({ channel: "chrome", headless: true });
  const files = new Map([["/", "room.html"], ["/on-air", "on-air.html"], ...[
    "room.js", "room-core.js", "room.css", "director.js", "director-worklet.js",
    "on-air.js", "on-air.css", "design-system/tokens.css", "design-system/components.css",
    "design-system/components.js", "design-system/timing.js",
  ].map(name => ["/" + name, name])]);
  // Chromium's AudioWorklet loader can bypass Playwright interception. An
  // isolated static server supplies those bundled scripts without depending on
  // the user's helper; application APIs below remain intercepted.
  const server = http.createServer(async (request, response) => {
    const file = files.get(new URL(request.url, "http://localhost").pathname);
    if (!file) { response.writeHead(404); response.end(); return; }
    response.setHeader("Content-Type", file.endsWith(".js") ? "text/javascript" : file.endsWith(".css") ? "text/css" : "text/html");
    response.end(await fs.readFile(path.join(__dirname, "static", file)));
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const brief = {
    title: "Authored realtime show", goal: "Show one practical example", audience: "Builders", context: "Fictional fixture",
    tip: "Make a useful show", recovery: "Continue the example", research: [], rundown: [], timing: { mode: "flexible", minutes: 30 },
    intentions: [{ id: "example", text: "Show the example", next_action: "Run the example", kind: "demonstrate", priority: "essential" }],
  };
  const errors = [], external = [], allPackets = [];

  async function fixture({ input = "microphone", duration = 20, slow = false, captions = false, startupFailure = false, idleAge = 0 } = {}) {
    const context = await browser.newContext({ viewport: { width: 1280, height: 720 } });
    let active = false, confidence = null, graphic = null, memory, started = false, firstPacketAt = null;
    const requests = [], packets = [], stops = [], prepares = [], startRequests = [];
    await context.addInitScript(() => {
      window.mediaRequests = [];
      navigator.mediaDevices.getDisplayMedia = async () => { throw Error("No actual tab may be captured by this fixture"); };
      navigator.mediaDevices.getUserMedia = async constraints => {
        window.mediaRequests.push(constraints);
        const source = new AudioContext(), tone = source.createOscillator(), destination = source.createMediaStreamDestination();
        tone.connect(destination); tone.start(); await source.resume();
        window.fixtureAudio = source;
        const stream = destination.stream;
        if (constraints.video) {
          const canvas = document.createElement("canvas"); canvas.width = 640; canvas.height = 360;
          const draw = canvas.getContext("2d"); draw.fillStyle = "#244d72"; draw.fillRect(0, 0, 640, 360);
          stream.addTrack(canvas.captureStream(10).getVideoTracks()[0]);
        }
        window.fixtureStream = stream;
        return stream;
      };
    });
    await context.route("**/*", async route => {
      const url = new URL(route.request().url());
      if (url.origin !== origin) { external.push(url.href); return route.abort(); }
      if (!url.pathname.startsWith("/api/")) {
        const file = files.get(url.pathname);
        return route.fulfill({ status: file ? 200 : 404, contentType: file?.endsWith(".js") ? "text/javascript" : file?.endsWith(".css") ? "text/css" : "text/html", body: file ? await fs.readFile(path.join(__dirname, "static", file)) : "" });
      }
      requests.push(url.pathname);
      const data = route.request().method() === "POST" ? route.request().postDataJSON() : {};
      let result;
      if (url.pathname === "/api/room") result = { csrf: "fixture", liveReady: true, audienceSelection: false, rehearsal: { ready: true, duration, notes: "Show a useful example." } };
      else if (url.pathname === "/api/show-materials") result = { folder: "", image_count: 0, notes_available: false, notes: "" };
      else if (url.pathname === "/api/prepare") { prepares.push(data); assert.equal(data.runtime, "realtime"); result = { brief, readback: "", question: "" }; }
      else if (url.pathname === "/api/start") {
        assert.equal(data.runtime, "realtime"); assert.equal(data.notes, "Show a useful example."); assert.equal(data.offline, true);
        memory = data.memory; active = true; started = false; confidence = graphic = null; packets.length = 0;
        startRequests.push(data); result = { id: "fixture-" + startRequests.length, memory, show_id: data.show_id };
      } else if (url.pathname === "/api/director/start") {
        if (startupFailure && startRequests.length === 1)
          return route.fulfill({ status: 503, json: { error: "Authored Realtime startup failure" } });
        started = Date.now(); confidence = { line: "Start with the concrete example", kind: "guidance" };
        graphic = { id: "fixture-title", kind: "title", line: "A practical example", attribution: "Now", until: Date.now() / 1000 + 30 };
        result = { status: "listening", model: "fixture-realtime" };
      } else if (url.pathname === "/api/director/status") {
        assert(started);
        result = { status: active ? "listening" : "stopped", confidence, memory, public: { graphic } };
      } else if (url.pathname === "/api/director/audio") {
        assert(active && started); assert.equal(data.sequence, packets.length);
        if (!firstPacketAt) firstPacketAt = Date.now() - started;
        if (input === "rehearsal") assert.equal(data.audio, undefined);
        else assert.equal(Buffer.from(data.audio, "base64").length, 24000);
        assert.equal(data.source_end, startRequests.at(-1).offset + (data.sequence + 1) * 0.5);
        if (input === "microphone") assert.equal(data.frame, undefined);
        if (data.frame) assert(Buffer.from(data.frame, "base64").subarray(0, 2).equals(Buffer.from([0xff, 0xd8])));
        packets.push({ sequence: data.sequence, source_end: data.source_end, audio_bytes: data.audio ? Buffer.from(data.audio, "base64").length : 0, frame: !!data.frame });
        if (packets.length === 4) {
          confidence = { line: "Explain what this result means", kind: "editorial" };
          graphic = { id: "fixture-question", kind: "question", line: "How could I try this tomorrow?", attribution: "Simulated viewer", awaiting_response: true };
        }
        if (packets.length === 6) confidence = null;
        if (packets.length === 10) graphic = null;
        if (slow && data.sequence === 0) await new Promise(resolve => setTimeout(resolve, 5000));
        result = { accepted: true };
      } else if (url.pathname === "/api/on-air") result = { graphic: active ? graphic : null };
      else if (url.pathname === "/api/stop") {
        active = false; stops.push(data); result = { stopped: true, memory, transcript: { through: data.through } };
      } else throw Error("Old or unexpected API used: " + url.pathname);
      try { return await route.fulfill({ json: result }); } catch (error) { if (active) throw error; }
    });
    const page = await context.newPage();
    page.on("pageerror", error => errors.push(error.message));
    await page.goto(origin + "/?rehearsal=office-hours");
    await page.locator("#thought").fill("Show a useful example.");
    await page.locator("#startDisclosure").evaluate(element => { element.open = true; });
    await page.locator("#inputKind").selectOption(input);
    if (captions) await page.locator("#captionsMode").check();
    await page.locator("#startDisclosure").evaluate(element => { element.open = false; });
    if (idleAge) {
      await page.clock.install();
      await page.clock.runFor(idleAge);
    }
    await page.locator("#sendPrep").click();
    try {
      if (startupFailure)
        await page.waitForFunction(() => document.querySelector("#sessionState").textContent === "Paused", null, { timeout: 10000 });
      else
        await page.waitForFunction(() => document.body.classList.contains("presenting"), null, { timeout: 10000 });
    } catch (error) {
      console.error(JSON.stringify({ fixture_input: input, requests, errors, notice: await page.locator("#notice").textContent(), prep_status: await page.locator("#prepStatus").textContent(), session_state: await page.locator("#sessionState").textContent() }));
      throw error;
    }
    return { context, page, packets, requests, stops, prepares, startRequests, firstPacket: () => firstPacketAt };
  }

  try {
    const { RealtimeFeed } = await import("./static/director.js");
    const coarse = new RealtimeFeed(() => { throw Error("The scheduling fixture cannot call an API"); });
    const availablePackets = [];
    coarse.epoch = 1; coarse.offset = 0; coarse.duration = 2; coarse.sequence = 0;
    coarse.recordingStarted = performance.now() - 1200;
    coarse.enqueue = packet => availablePackets.push(packet.source_end);
    coarse.recordingTick(1);
    coarse.stop();
    assert.deepEqual(availablePackets, [0.5, 1], "Coarser background timer ticks retain every available recording interval");
    const startup = await fixture({ startupFailure: true, idleAge: 25000 });
    assert.equal(startup.stops[0].through, 0, "Connection setup and page age are not observed show time");
    assert.equal(startup.stops[0].reason, "paused");
    assert.equal(startup.packets.length, 0);
    assert.equal(await startup.page.evaluate(() => window.fixtureStream.getTracks().every(track => track.readyState === "ended")), true);
    await startup.page.locator("#startShow").click();
    await startup.page.waitForFunction(() => document.body.classList.contains("presenting"));
    assert.equal(startup.startRequests[1].offset, 0, "Resume after failed startup must preserve the accepted source offset");
    assert.equal(startup.startRequests[0].show_id, startup.startRequests[1].show_id);
    await startup.page.keyboard.press("Escape"); await startup.page.locator("#endShow").click();
    await startup.page.locator("#reviewView").waitFor();
    assert(!startup.requests.some(request => /window|captions|audience/.test(request)));
    await startup.context.close();
    const live = await fixture();
    const output = await live.context.newPage(); output.on("pageerror", error => errors.push(error.message));
    await output.goto(origin + "/on-air");
    await live.page.getByRole("heading", { name: "Start with the concrete example", exact: true }).waitFor();
    assert.equal(await live.page.locator("button:visible,input:visible,textarea:visible,select:visible,a:visible").count(), 0);
    await live.page.getByRole("heading", { name: "Explain what this result means", exact: true }).waitFor();
    await output.getByText("How could I try this tomorrow?", { exact: true }).waitFor();
    await live.page.locator("#hostCue").waitFor({ state: "hidden" });
    assert(await output.getByText("How could I try this tomorrow?", { exact: true }).isVisible(), "A cleared confidence cue must remain quiet even if a public question is on air");
    await output.locator("#graphic").waitFor({ state: "hidden" });
    assert(live.firstPacket() < 1500, "Audio must reach the helper before an eight-second capture boundary");
    assert(live.packets.length >= 8);
    await live.page.keyboard.press("Escape"); await live.page.locator("#pauseShow").click();
    await live.page.waitForFunction(() => document.querySelector("#sessionState").textContent === "Paused");
    const sentAtPause = live.packets.length;
    assert.equal(await live.page.evaluate(() => window.fixtureStream.getTracks().every(track => track.readyState === "ended")), true);
    await live.page.waitForTimeout(700); assert.equal(live.packets.length, sentAtPause);
    await live.page.locator("#startShow").click();
    await live.page.waitForFunction(() => document.body.classList.contains("presenting"));
    assert.equal(live.startRequests[0].show_id, live.startRequests[1].show_id);
    await live.page.keyboard.press("Escape"); await live.page.locator("#endShow").click();
    await live.page.locator("#reviewView").waitFor();
    assert.deepEqual(live.stops.map(stop => stop.reason), ["paused", "ended"]);
    assert(!live.requests.some(request => /window|captions|audience/.test(request)));
    allPackets.push({ input: "generated microphone", first_packet_ms: live.firstPacket(), packets: sentAtPause });
    await live.context.close();

    const camera = await fixture({ input: "camera", captions: true });
    await camera.page.waitForFunction(() => document.querySelector("#hostCueLine").textContent === "Explain what this result means");
    assert(camera.packets.some(packet => packet.frame));
    assert.equal(camera.startRequests[0].captions, true);
    assert(!camera.requests.some(request => /captions/.test(request)), "Optional subtitle display shares the Realtime input, not a second speech connection");
    await camera.page.keyboard.press("Escape"); await camera.page.locator("#endShow").click(); await camera.page.locator("#reviewView").waitFor();
    allPackets.push({ input: "generated camera", packets: camera.packets.length, frames: camera.packets.filter(packet => packet.frame).length });
    await camera.context.close();

    const recording = await fixture({ input: "rehearsal", duration: 2 });
    await recording.page.locator("#reviewView").waitFor();
    assert.deepEqual(recording.packets.map(packet => packet.source_end), [0.5, 1, 1.5, 2]);
    assert(recording.packets.every(packet => packet.audio_bytes === 0));
    assert.deepEqual(await recording.page.evaluate(() => window.mediaRequests), []);
    assert.equal(recording.stops[0].reason, "ended");
    allPackets.push({ input: "prepared recording", packets: recording.packets.length });
    await recording.context.close();

    const slow = await fixture({ slow: true });
    await slow.page.waitForFunction(() => document.querySelector("#sessionState").textContent === "Paused", null, { timeout: 10000 });
    assert.match(await slow.page.locator("#notice").textContent(), /could not be delivered in time/);
    assert.equal(await slow.page.evaluate(() => window.fixtureStream.getTracks().every(track => track.readyState === "ended")), true);
    assert.equal(slow.stops[0].reason, "paused");
    await slow.page.waitForTimeout(2000);
    await slow.context.close();
    assert.deepEqual(errors, []); assert.deepEqual(external, []);
    console.log(JSON.stringify({ continuous_pcm_500ms: allPackets, early_audio_before_eight_seconds: true, one_director_updates_both_screens: true, confidence_clear_has_no_local_fallback: true, selected_camera_frames: true, pause_resume_same_show: true, stop_releases_capture_and_packets: true, bounded_queue_pauses_honestly: true, startup_failure_after_old_page_keeps_offset_zero: true, recording_uses_no_hardware: true, recording_coarse_timer_preserves_intervals: true, subtitles_share_realtime_input: true, old_editorial_or_caption_calls: 0, actual_model_or_twitch_calls: 0, presenter_controls: 0, page_errors: errors }));
  } finally {
    await browser.close();
    // Worklet requests use the real fixture server rather than Playwright's
    // route handler. Close its remaining keep-alive sockets during teardown.
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });

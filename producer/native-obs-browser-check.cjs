const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const http = require("node:http");

// Isolated local fixture. No user devices, OBS process, model or Twitch calls.
(async () => {
  const files = new Map([["/", "room.html"], ["/on-air", "on-air.html"], ...[
    "room.js", "room-core.js", "room.css", "director.js", "on-air.js", "on-air.css",
    "design-system/tokens.css", "design-system/components.css", "design-system/components.js", "design-system/timing.js",
  ].map(name => ["/" + name, name])]);
  const server = http.createServer(async (request, response) => {
    const file = files.get(new URL(request.url, "http://localhost").pathname);
    response.writeHead(file ? 200 : 404, { "Content-Type": file?.endsWith(".js") ? "text/javascript" : file?.endsWith(".css") ? "text/css" : "text/html" });
    response.end(file ? await fs.readFile(path.join(__dirname, "static", file)) : "");
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({ channel: "chrome", headless: true });
  const brief = {
    title: "OBS fixture", goal: "Explain the example", audience: "Builders", context: "Authored local fixture",
    tip: "Show the result", recovery: "Continue the example", research: [], rundown: [], timing: { mode: "flexible", minutes: 30 },
    intentions: [{ id: "example", text: "Show the example", next_action: "Run it", kind: "demonstrate", priority: "essential" }],
  };
  const errors = [], external = [];
  async function fixture(configured, expectedOffline = true) {
    const context = await browser.newContext();
    const starts = [], stops = [], requests = [];
    let state = { status: "waiting", through: 0, confidence: null, public: { graphic: null } };
    await context.addInitScript(() => {
      window.captureAttempts = [];
      const forbidden = name => () => { window.captureAttempts.push(name); throw Error("Native OBS must not use " + name); };
      navigator.mediaDevices.getUserMedia = forbidden("getUserMedia");
      navigator.mediaDevices.getDisplayMedia = forbidden("getDisplayMedia");
      window.AudioContext = forbidden("AudioContext");
      window.AudioWorkletNode = forbidden("AudioWorkletNode");
    });
    await context.route("**/*", async route => {
      const url = new URL(route.request().url());
      if (url.origin !== origin) { external.push(url.href); return route.abort(); }
      if (!url.pathname.startsWith("/api/")) return route.continue();
      requests.push(url.pathname);
      const data = route.request().method() === "POST" ? route.request().postDataJSON() : {};
      let result;
      if (url.pathname === "/api/room") result = { csrf: "fixture", liveReady: true, audienceInput: "screen", nativeOBS: { configured, endpoint: configured ? "tcp://127.0.0.1:8790" : null, recording: false }, rehearsal: { ready: true, duration: 10, notes: "Fixture" } };
      else if (url.pathname === "/api/show-materials") result = { folder: "", notes: "", image_count: 0 };
      else if (url.pathname === "/api/prepare") result = { brief, readback: "", question: "" };
      else if (url.pathname === "/api/start") {
        assert.equal(data.input_type, "obs"); assert.equal(data.runtime, "realtime"); assert.equal(data.offline, expectedOffline);
        starts.push(data); state = { status: "waiting", through: data.offset, confidence: null, memory: data.memory, public: { graphic: null } };
        result = { id: "native-" + starts.length, show_id: data.show_id, memory: data.memory };
      } else if (url.pathname === "/api/director/start" || url.pathname === "/api/director/status") result = state;
      else if (url.pathname === "/api/on-air") result = state.public;
      else if (url.pathname === "/api/stop") { stops.push(data); result = { stopped: true, memory: state.memory, transcript: { through: state.through } }; }
      else { errors.push("Unexpected API " + url.pathname); return route.fulfill({ status: 500, json: { error: "Unexpected fixture call" } }); }
      await route.fulfill({ json: result });
    });
    const page = await context.newPage();
    page.on("pageerror", error => errors.push(error.message));
    await page.goto(origin);
    await page.locator("#thought").fill("Show the OBS example.");
    await page.locator("#startDisclosure").evaluate(element => { element.open = true; });
    return { context, page, starts, stops, requests, setState: update => { state = { ...state, ...update }; } };
  }
  try {
    const absent = await fixture(false);
    assert.equal(await absent.page.locator("#inputKind").inputValue(), "camera");
    assert.equal(await absent.page.locator("#obsInputOption").evaluate(element => element.disabled), true);
    assert.equal(await absent.page.locator("#obsInputOption").getAttribute("hidden"), "");
    assert.deepEqual(await absent.page.evaluate(() => window.captureAttempts), []);
    await absent.context.close();

    const native = await fixture(true);
    assert.equal(await native.page.locator("#inputKind").inputValue(), "obs");
    assert.equal(await native.page.locator("#liveTwitchActions").isEnabled(), true);
    assert.equal(await native.page.locator("#liveTwitchActions").isChecked(), false);
    assert.equal(await native.page.locator("#sendPrep").textContent(), "Start offline test");
    assert.match(await native.page.locator("#sourceDisclosure").innerText(), /Twitch actions are simulated/);
    assert.match(await native.page.locator("#obsInputHelp").innerText(), /full audio mix directly/);
    assert.deepEqual(await native.page.evaluate(() => window.captureAttempts), []);
    await native.page.locator("#sendPrep").click();
    await native.page.waitForFunction(() => document.body.classList.contains("presenting"));
    await native.page.getByRole("heading", { name: "Start Recording in OBS", exact: true }).waitFor();
    assert.equal(await native.page.locator("#sessionState").textContent(), "Waiting for OBS");
    await native.page.waitForTimeout(800);
    assert.equal(await native.page.locator("#showClock").textContent(), "00:00", "Waiting for OBS is not observed show time");
    assert.equal(await native.page.locator("button:visible,input:visible,textarea:visible,select:visible,a:visible").count(), 0);
    assert.deepEqual(await native.page.evaluate(() => window.captureAttempts), []);

    native.setState({ status: "connecting", through: 0.5 });
    await native.page.getByRole("heading", { name: "OBS connected. Producer is connecting…", exact: true }).waitFor();
    native.setState({ status: "listening", through: 3.5, confidence: { line: "Show the working example", kind: "guidance" }, public: { graphic: { id: "native-card", kind: "title", line: "OBS programme input", attribution: "Producer", until: Date.now() / 1000 + 60 } } });
    await native.page.getByRole("heading", { name: "Show the working example", exact: true }).waitFor();
    const output = await native.context.newPage();
    output.on("pageerror", error => errors.push(error.message));
    await output.goto(origin + "/on-air");
    await output.getByText("OBS programme input", { exact: true }).waitFor();
    await native.page.keyboard.press("Escape");
    await native.page.locator("#pauseShow").click();
    await native.page.waitForFunction(() => document.querySelector("#sessionState").textContent === "Paused");
    assert.equal(native.stops[0].through, 3.5, "Pause reports accepted OBS time, not browser wall time");
    assert.equal(await native.page.locator("#inputKind").isEnabled(), false);
    await native.page.locator("#startShow").click();
    await native.page.getByRole("heading", { name: "Start Recording in OBS", exact: true }).waitFor();
    assert.equal(native.starts[0].show_id, native.starts[1].show_id);
    assert.equal(native.starts[1].offset, 3.5);
    native.setState({ status: "error", error: "The selected OBS recording feed ended.", confidence: null, public: { graphic: null } });
    await native.page.waitForFunction(() => document.querySelector("#sessionState").textContent === "Paused");
    assert.match(await native.page.locator("#notice").textContent(), /OBS recording feed ended/);
    assert.deepEqual(await native.page.evaluate(() => window.captureAttempts), []);
    assert.equal(native.requests.filter(p => p === "/api/director/audio").length, 0);
    assert.equal(native.requests.filter(p => p === "/api/director/start").length, 2);
    assert.deepEqual(native.stops.map(s => s.reason), ["paused", "paused"]);
    await native.page.keyboard.press("Escape");
    await native.page.locator("#endShow").click();
    await native.page.locator("#reviewView").waitFor();
    assert.equal(native.stops.at(-1).reason, "ended");
    await native.context.close();

    const live = await fixture(true, false);
    await live.page.locator("#liveTwitchActions").check();
    assert.equal(await live.page.locator("#sendPrep").textContent(), "Start show");
    assert.match(await live.page.locator("#sourceDisclosure").innerText(), /Live Twitch actions may post to your connected channel/);
    assert.match(await live.page.locator("#sourceDisclosure").innerText(), /does not start a Twitch broadcast/);
    assert.doesNotMatch(await live.page.locator("#sourceDisclosure").innerText(), /Twitch actions are simulated/);
    assert.equal(await live.page.locator("#offlineNotice").isVisible(), false);
    await live.page.locator("#sendPrep").click();
    await live.page.getByRole("heading", { name: "Start Recording in OBS", exact: true }).waitFor();
    assert.equal(live.starts[0].offline, false);
    assert.equal(live.starts[0].input_type, "obs");
    assert.equal(await live.page.locator("#liveTwitchActions").isEnabled(), false);
    live.setState({ status: "listening", through: 2.5 });
    await live.page.waitForFunction(() => document.querySelector("#sessionState").textContent === "Listening");
    await live.page.keyboard.press("Escape");
    await live.page.locator("#pauseShow").click();
    await live.page.waitForFunction(() => document.querySelector("#sessionState").textContent === "Paused");
    assert.equal(await live.page.locator("#startShow").textContent(), "Resume show");
    assert.equal(await live.page.locator("#liveTwitchActions").isEnabled(), false);
    assert.equal(await live.page.locator("#inputKind").isEnabled(), false);
    await live.page.locator("#startShow").click();
    await live.page.getByRole("heading", { name: "Start Recording in OBS", exact: true }).waitFor();
    assert.equal(live.starts[1].offline, false);
    assert.equal(live.starts[1].show_id, live.starts[0].show_id);
    assert.equal(live.starts[1].offset, 2.5);
    await live.page.keyboard.press("Escape");
    await live.page.locator("#endShow").click();
    await live.page.locator("#reviewView").waitFor();
    assert.equal(await live.page.locator("#liveTwitchActions").isChecked(), false);
    assert.match(await live.page.locator("#sourceDisclosure").textContent(), /Twitch actions are simulated/);
    assert.deepEqual(await live.page.evaluate(() => window.captureAttempts), []);
    assert.equal(live.requests.filter(p => p === "/api/director/audio").length, 0);
    await live.page.reload();
    await live.page.locator("#thought").waitFor();
    await live.page.locator("#startDisclosure").evaluate(element => { element.open = true; });
    assert.equal(await live.page.locator("#inputKind").inputValue(), "obs");
    assert.equal(await live.page.locator("#liveTwitchActions").isChecked(), false);
    assert.equal(await live.page.locator("#sendPrep").textContent(), "Start offline test");
    // The file rehearsal remains strictly offline, even after a prior opt-in.
    await live.page.locator("#liveTwitchActions").check();
    await live.page.locator("#inputKind").selectOption("rehearsal");
    assert.equal(await live.page.locator("#liveTwitchActions").isChecked(), false);
    assert.equal(await live.page.locator("#liveTwitchActions").isEnabled(), false);
    await live.page.locator("#inputKind").selectOption("obs");
    assert.equal(await live.page.locator("#liveTwitchActions").isChecked(), false);
    assert.equal(await live.page.locator("#liveTwitchActions").isEnabled(), true);
    await live.context.close();
    assert.deepEqual(errors, []); assert.deepEqual(external, []);
    console.log(JSON.stringify({ native_obs_default_only_when_configured: true, offline_default_and_reset: true, explicit_obs_live_selection: true, live_mode_pinned_across_pause_resume: true, file_rehearsal_remains_offline: true, browser_capture_attempts: 0, browser_audio_packets: 0, waiting_is_not_programme_time: true, helper_status_updates_both_screens: true, pause_resume_same_source_and_show: true, source_failure_pauses_without_fallback: true, real_model_twitch_or_obs_calls: 0, page_errors: errors }));
  } finally {
    await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });

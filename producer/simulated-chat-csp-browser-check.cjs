const { chromium } = require("playwright");
const { spawn } = require("node:child_process");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");

// Real Python Handler and its CSP, with one independently authored fictional
// native-source timeline. No model, capture device, OBS or Twitch connection.
const fixture = String.raw`
import json, time
from http.server import ThreadingHTTPServer
import server
server.LIVE["csp-fixture"] = {
    "input_type": "obs", "offset": 0, "native_through": 40,
    "created": time.monotonic(), "director": None,
    "simulated_chat": {"duration": 600, "events": [{
        "at": 20, "type": "channel.chat.message", "id": "fixture-comment",
        "author": "Nico · simulated viewer", "text": "Can we see the example a little larger?"
    }]}
}
http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
http.key_file = None
print(json.dumps({"port": http.server_port}), flush=True)
try:
    http.serve_forever()
finally:
    http.server_close()
`;

(async () => {
  const output = path.resolve(__dirname, "../.local/producer/screen-chat-csp");
  await fs.mkdir(output, { recursive: true });
  const helper = spawn("python3", ["-u", "-c", fixture], {
    cwd: __dirname, stdio: ["ignore", "pipe", "pipe"],
  });
  let stderr = "", browser;
  helper.stderr.on("data", chunk => { stderr = (stderr + chunk.toString()).slice(-2000); });
  try {
    const port = await new Promise((resolve, reject) => {
      let line = "";
      const timeout = setTimeout(() => reject(Error("Fixture helper did not start: " + stderr)), 5000);
      helper.on("error", error => { clearTimeout(timeout); reject(error); });
      helper.on("exit", code => { clearTimeout(timeout); reject(Error("Fixture helper exited " + code + ": " + stderr)); });
      helper.stdout.on("data", chunk => {
        line += chunk.toString();
        if (!line.includes("\n")) return;
        clearTimeout(timeout);
        resolve(JSON.parse(line.split("\n")[0]).port);
      });
    });
    const origin = `http://127.0.0.1:${port}`;
    browser = await chromium.launch({ channel: "chrome", headless: true });
    const context = await browser.newContext({ viewport: { width: 1280, height: 720 } });
    const errors = [], violations = [], requests = [];
    await context.route("**/*", route => {
      const url = new URL(route.request().url());
      assert.equal(url.origin, origin, "Only the isolated loopback helper may receive requests");
      requests.push(url.pathname);
      return route.continue();
    });
    const page = await context.newPage();
    page.on("pageerror", error => errors.push(error.message));
    page.on("console", message => {
      if (/Content Security Policy|violates.*policy/i.test(message.text())) violations.push(message.text());
    });
    const response = await page.goto(origin + "/simulated-chat");
    const chatCsp = response.headers()["content-security-policy"];
    try {
      await page.locator("#simulatedChat").waitFor({ state: "visible", timeout: 4000 });
    } catch (error) {
      console.error(JSON.stringify({ csp: chatCsp, violations, image: await page.locator("#simulatedChat").evaluate(image => ({ hidden: image.hidden, width: image.naturalWidth })) }));
      throw error;
    }
    assert.match(chatCsp, /img-src 'self' data: blob:/);
    assert.equal(await page.locator("#simulatedChat").evaluate(image => image.naturalWidth), 1280);
    assert.equal(await page.locator("#simulatedChat").evaluate(image => image.src.startsWith("blob:")), true);
    assert(!(await page.locator("body").textContent()).includes("Can we see the example"), "Comments must remain pixels, not hidden DOM text");
    const bounds = await page.locator("#simulatedChat").evaluate(image => {
      const canvas = document.createElement("canvas"); canvas.width = 1280; canvas.height = 720;
      const draw = canvas.getContext("2d"); draw.drawImage(image, 0, 0);
      const data = draw.getImageData(0, 0, 1280, 720).data;
      let opaque = 0;
      for (let index = 3; index < data.length; index += 4) if (data[index]) opaque++;
      return { opaque };
    });
    assert(bounds.opaque > 10000, "Actual native-source time must paint the fictional comment");
    await page.waitForTimeout(650);
    assert(requests.filter(item => item === "/api/simulated-chat.png").length >= 2);
    await page.screenshot({ path: path.join(output, "native-chat-real-csp.png"), omitBackground: true });
    for (const route of ["/", "/on-air", "/rehearsal"]) {
      const other = await context.request.get(origin + route);
      const policy = other.headers()["content-security-policy"];
      assert.match(policy, /img-src 'self' data:;/);
      assert(!/img-src[^;]*blob:/.test(policy), "Blob image permission is scoped to simulated chat");
    }
    assert.deepEqual(violations, []); assert.deepEqual(errors, []);
    console.log(JSON.stringify({ actual_python_handler_csp: true, native_source_clock_s: 40, fictional_comment_pixels_visible: true,
      blob_image_permission_scoped_to_chat: true, refreshes: requests.filter(item => item === "/api/simulated-chat.png").length,
      opaque_pixels: bounds.opaque, model_capture_or_twitch_calls: 0, page_errors: errors, csp_violations: violations }));
  } finally {
    if (browser) await browser.close();
    helper.kill("SIGTERM");
    await new Promise(resolve => { if (helper.exitCode !== null || helper.signalCode !== null) resolve(); else helper.once("exit", resolve); });
  }
})().catch(error => { console.error(error); process.exitCode = 1; });

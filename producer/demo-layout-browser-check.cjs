const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const http = require("node:http");
const { execFileSync } = require("node:child_process");

// Isolated fixtures only: no OBS, user profile, capture, inference or Twitch.
(async () => {
  const files = new Map([["/demo-layout", "demo-layout.html"], ...[
    "demo-layout.js", "demo-layout.css", "design-system/tokens.css",
    "design-system/components.css", "design-system/components.js", "design-system/timing.js", "visuals/canada.svg",
  ].map(file => ["/" + file, file])]);
  const server = http.createServer(async (request, response) => {
    const file = files.get(new URL(request.url, "http://localhost").pathname);
    response.writeHead(file ? 200 : 404, { "Content-Type": file?.endsWith(".js") ? "text/javascript" : file?.endsWith(".css") ? "text/css" : file?.endsWith(".svg") ? "image/svg+xml" : "text/html",
      "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data: blob:; frame-ancestors 'none'" });
    response.end(file ? await fs.readFile(path.join(__dirname, "static", file)) : "");
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  let browser;
  try {
  browser = await chromium.launch({ channel: "chrome", headless: true });
  const context = await browser.newContext({ viewport: { width: 1920, height: 1080 } });
  const errors = [], requests = [], external = [];
  const line = "When you replay a test, do you call the model again or replay saved actions? I want to compare how the same show looks after changing the screen layout, without paying again.";
  assert.ok(line.length <= 180);
  let state = { active: true, elapsed: 7, simulated_chat: true, public: {
    context: { subject: "Applied AI, in practice", purpose: "Making the build understandable" },
    graphic: { id: "long-question", kind: "question", line, attribution: "fictional_viewer" },
  }, confidence: { line: "PRIVATE CUE MUST NEVER RENDER" }, brief: "PRIVATE BRIEF MUST NEVER RENDER" };
  let fail = false;
  const png = execFileSync(process.env.PRODUCER_PYTHON || "python3", ["-c", `
import sys
from screen_chat_overlay import overlay_png
events = {"duration": 600, "events": [
    {"at": 1, "type": "channel.chat.message", "id": "one", "author": "pixel_viewer", "text": "Can we see the example a little larger?"},
    {"at": 2, "type": "channel.chat.message", "id": "two", "author": "night_shift", "text": "replaying the saved actions makes sense"},
    {"at": 3, "type": "channel.chat.message", "id": "three", "author": "tiny_robot", "text": "wait is the producer producing itself now"}
]}
sys.stdout.buffer.write(overlay_png(events, 7))
`], { cwd: __dirname });
  await context.route("**/*", async route => {
    const url = new URL(route.request().url());
    if (url.origin !== origin) { external.push(url.href); return route.abort(); }
    if (!url.pathname.startsWith("/api/")) return route.continue();
    requests.push({ path: url.pathname, method: route.request().method() });
    if (url.pathname === "/api/demo-layout") return route.fulfill({ status: fail ? 503 : 200, json: state });
    if (url.pathname === "/api/simulated-chat.png") return route.fulfill({ contentType: "image/png", body: png });
    errors.push("Unexpected API " + url.pathname); return route.abort();
  });
  const page = await context.newPage();
  page.on("pageerror", e => errors.push(e.message));
    await page.goto(origin + "/demo-layout");
    await page.locator("#publicSlot .producer-card__line").filter({ hasText: line }).waitFor();
    await page.locator("#chatImage:not([hidden])").waitFor();
    await page.waitForFunction(() => getComputedStyle(document.querySelector("#publicSlot .producer-card__line")).opacity === "1");
    assert.equal(await page.locator("#publicSlot .producer-card").isVisible(), true, "Full question must fit its on-air lower third");
    assert.equal(await page.locator("#disclosure").isVisible(), true);
    assert.equal(await page.locator("#chatCrop").evaluate(node => node.clientHeight), 504, "Opening crop excludes the PNG's own disclosure");
    assert.doesNotMatch(await page.locator("body").innerText(), /PRIVATE|Presenter monitor|On-air graphics|Shared screen|Camera|≋ Producer/);
    assert.doesNotMatch(await page.locator("body").innerText(), /pixel_viewer|see the example/);
    const bounds = await page.evaluate(() => Object.fromEntries(["screenSlot", "cameraSlot", "chatSlot", "publicSlot"].map(id => {
      const { x, y, width, height } = document.getElementById(id).getBoundingClientRect();
      return [id, { x, y, width, height }];
    })));
    assert.deepEqual(bounds.screenSlot, { x: 24, y: 24, width: 1296, height: 1032 });
    assert.deepEqual(bounds.cameraSlot, { x: 1344, y: 24, width: 552, height: 310.5 });
    assert.deepEqual(bounds.chatSlot, { x: 1344, y: 358, width: 552, height: 698 });
    assert.deepEqual(bounds.publicSlot, bounds.screenSlot, "Public graphics must overlay the shared picture");
    const rectangles = [bounds.screenSlot, bounds.cameraSlot, bounds.chatSlot];
    for (let i = 0; i < rectangles.length; i++) for (let j = i + 1; j < rectangles.length; j++) {
      const a = rectangles[i], b = rectangles[j];
      assert.ok(a.x + a.width <= b.x || b.x + b.width <= a.x || a.y + a.height <= b.y || b.y + b.height <= a.y, "Native source areas overlap");
    }
    const withinPicture = rectangle => {
      const picture = bounds.screenSlot;
      assert.ok(rectangle.x >= picture.x && rectangle.y >= picture.y
        && rectangle.x + rectangle.width <= picture.x + picture.width
        && rectangle.y + rectangle.height <= picture.y + picture.height,
        "On-air content must stay over the shared picture, outside camera and chat");
    };
    withinPicture(await page.locator("#publicSlot .producer-card").boundingBox());
    withinPicture(await page.locator("#publicSlot .producer-context").boundingBox());
    const lowerThird = await page.locator("#publicSlot .producer-card").boundingBox();
    assert.ok(lowerThird.y >= 700 && lowerThird.y + lowerThird.height <= 920,
      "The card overlays the lower part of the picture rather than a separate panel");
    for (const selector of ["html", "body", "#demoCanvas", "#publicSlot", ".native-slot", "#chatSlot"]) {
      assert.ok((await page.locator(selector).evaluateAll(nodes => nodes.map(node => {
        const style = getComputedStyle(node);
        return style.backgroundColor === "rgba(0, 0, 0, 0)" && style.backgroundImage === "none";
      }))).every(Boolean), selector + " must stay transparent");
    }
    const proof = path.join(__dirname, "..", ".local", "producer", "broadcast-layout-check");
    await fs.mkdir(proof, { recursive: true });
    await page.screenshot({ path: path.join(proof, "broadcast-layout-1920.png"), omitBackground: true });
    state = { ...state, public: { ...state.public, visual: { kind: "map", asset_id: "canada", until: Date.now() / 1000 + 300 } } };
    await page.locator("#publicSlot .producer-visual").waitFor();
    await page.waitForFunction(() => document.querySelector("#publicSlot .producer-visual__image").naturalWidth > 0);
    assert.equal(await page.locator("#publicSlot .producer-card").isVisible(), true, "Full question must also fit beside an image");
    const visualBounds = await page.locator("#publicSlot .producer-visual").boundingBox();
    const cardBounds = await page.locator("#publicSlot .producer-card").boundingBox();
    withinPicture(visualBounds); withinPicture(cardBounds);
    assert.ok(visualBounds.y + visualBounds.height <= cardBounds.y, "Public visual stays above the lower third");
    const contextBounds = await page.locator("#publicSlot .producer-context").boundingBox();
    assert.ok(contextBounds.y + contextBounds.height <= visualBounds.y, "Context and visual cannot overlap");
    await page.screenshot({ path: path.join(proof, "broadcast-layout-visual-1920.png"), omitBackground: true });
    state = { ...state, elapsed: 20, public: { ...state.public, visual: null, graphic: { id: "wide-question", kind: "question", line: "W".repeat(180), attribution: "fictional_viewer" } } };
    await page.waitForFunction(() => document.querySelector("#publicSlot .producer-card").dataset.omitted === "text-does-not-fit");
    assert.equal(await page.locator("#disclosure").isVisible(), false);
    assert.equal(await page.locator("#chatCrop").evaluate(node => node.clientHeight), 600);
    fail = true;
    await page.waitForFunction(() => document.querySelector("#chatImage").hidden && document.querySelector("#publicSlot .producer-context").hidden);
    assert.equal(await page.locator("#publicSlot .producer-card").isVisible(), false);
    const quietPNG = await page.screenshot({ path: path.join(proof, "broadcast-layout-quiet.png"), omitBackground: true });
    const transparent = execFileSync(process.env.PRODUCER_PYTHON || "python3", ["-c", `
import io, sys
from PIL import Image
image = Image.open(io.BytesIO(sys.stdin.buffer.read())).convert("RGBA")
assert image.getchannel("A").getbbox() is None, "Quiet overlay must not paint over the native programme"
print("transparent")
`], { input: quietPNG });
    assert.equal(transparent.toString().trim(), "transparent");
    await page.goto(origin + "/demo-layout?preview=1");
    await page.locator("#previewLabel").waitFor();
    assert.match(await page.locator("#previewLabel").innerText(), /fictional/);
    assert.ok(requests.every(r => r.method === "GET"));
    assert.ok(requests.every(r => ["/api/demo-layout", "/api/simulated-chat.png"].includes(r.path)));
    assert.deepEqual(errors, []); assert.deepEqual(external, []);
    console.log(JSON.stringify({ transparent_broadcast_overlay: true, public_graphics_over_shared_picture: true, camera_and_chat_not_covered: true, full_question_fits: true, simultaneous_visual_and_card_fit: true,
      chat_is_pixels_only: true, duplicate_disclosure_cropped: true,
      excessive_width_quote_omitted_whole: true, private_data_not_rendered: true, failure_clears_output: true,
      preview_labelled: true, capture_or_inference_calls: 0, external_requests: 0 }));
  } finally {
    await browser?.close(); await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });

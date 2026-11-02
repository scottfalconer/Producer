import { createPublicSurface } from "/design-system/components.js";

const canvas = document.querySelector("#demoCanvas");
const output = createPublicSurface(document.querySelector("#publicSlot"));
const image = document.querySelector("#chatImage");
const disclosure = document.querySelector("#disclosure");
const preview = new URLSearchParams(location.search).get("preview") === "1";
let currentURL;

function resize() {
  canvas.style.transform = `scale(${Math.min(innerWidth / 1920, innerHeight / 1080)})`;
}
addEventListener("resize", resize);
resize();

function render(state = {}) {
  const publicState = state.public || {};
  output.render(publicState);
  disclosure.hidden = !(state.active === true && state.elapsed >= 0 && state.elapsed < 12 && state.simulated_chat === true);
  // The original PNG also has a disclosure below its opening message area.
  // Crop that out; this layout places the same disclosure along the full bottom.
  canvas.classList.toggle("opening", !disclosure.hidden);
}

async function chatImage(url) {
  let next;
  try {
    const response = await fetch(url, { cache: "no-store", signal: AbortSignal.timeout(2000) });
    if (!response.ok || !response.headers.get("content-type")?.startsWith("image/png")) throw Error();
    next = URL.createObjectURL(await response.blob());
    await new Promise((resolve, reject) => { image.onload = resolve; image.onerror = reject; image.src = next; });
    if (currentURL) URL.revokeObjectURL(currentURL);
    currentURL = next;
    image.hidden = false;
  } catch {
    if (next) URL.revokeObjectURL(next);
    image.hidden = true;
  }
}

if (preview) {
  document.querySelector("#previewLabel").hidden = false;
  render({ public: {
    context: { subject: "Applied AI, in practice", purpose: "Building a live producer with Codex" },
    graphic: { id: "layout-preview", kind: "question", line: "When you replay a test, do you call the model again or replay its saved actions?", attribution: "fictional_viewer" },
  }});
  void chatImage("/api/simulated-chat.png?preview=1&at=65");
} else {
  async function poll() {
    try {
      const response = await fetch("/api/demo-layout", { cache: "no-store", signal: AbortSignal.timeout(2000) });
      if (!response.ok) throw Error();
      const state = await response.json();
      render(state);
      if (state.active) await chatImage("/api/simulated-chat.png");
      else image.hidden = true;
    } catch {
      render();
      image.hidden = true;
    }
    setTimeout(poll, 500);
  }
  void poll();
}

const token = location.hash.slice(1);
const cue = document.querySelector("#cue");
const presence = document.querySelector("#presence");
const connection = document.querySelector("#connection");
const validToken = /^[A-Za-z0-9_-]{43}$/.test(token);
let wakeLock;

function clear(message) {
  cue.textContent = "";
  presence.dataset.state = "off";
  presence.setAttribute("aria-label", "Disconnected");
  connection.textContent = message;
  connection.hidden = false;
}

async function stayAwake() {
  if (!navigator.wakeLock || document.visibilityState !== "visible" || wakeLock) return;
  try {
    wakeLock = await navigator.wakeLock.request("screen");
    wakeLock.addEventListener("release", () => { wakeLock = undefined; });
  } catch { /* LAN HTTP or the phone's power policy may not allow wake lock. */ }
}

async function poll() {
  if (!validToken) {
    clear("Open the private phone link from Producer on your Mac.");
    return;
  }
  try {
    const response = await fetch("/api/presenter", {
      headers: { Authorization: "Bearer " + token },
      cache: "no-store",
      signal: AbortSignal.timeout(2500),
    });
    if (response.status === 401) {
      clear("This phone link has expired. Open the current link from your Mac.");
      return;
    }
    if (!response.ok) throw Error();
    const state = await response.json();
    const active = state.presence === "active";
    const paused = state.presence === "paused";
    const line = active && typeof state.confidence?.line === "string" ? state.confidence.line : "";
    if (cue.textContent !== line) cue.textContent = line;
    presence.dataset.state = active ? "active" : paused ? "paused" : "off";
    presence.setAttribute("aria-label", active ? "Selected input active" : paused ? "Monitoring paused" : "Capture off");
    connection.textContent = paused ? "Paused" : "Waiting for Producer";
    connection.hidden = active;
  } catch {
    clear("Connection lost. Reconnecting…");
  }
  setTimeout(poll, 500);
}

document.addEventListener("visibilitychange", stayAwake);
void stayAwake();
void poll();

// The browser source receives pixels only. Timeline messages stay in the painter.
const image = document.querySelector("#simulatedChat");
const preview = new URLSearchParams(location.search).get("preview") === "1";
if (preview) {
  const label = document.createElement("span");
  label.id = "simulatedChatPreview";
  label.textContent = "Simulated chat · preview";
  document.body.append(label);
}
let currentURL;
async function refreshChat() {
  let nextURL;
  try {
    const response = await fetch("/api/simulated-chat.png" + (preview ? "?preview=1" : ""), {
      cache: "no-store", signal: AbortSignal.timeout(2000),
    });
    if (!response.ok || !response.headers.get("content-type")?.startsWith("image/png"))
      throw Error("Simulated chat image unavailable");
    nextURL = URL.createObjectURL(await response.blob());
    await new Promise((resolve, reject) => {
      image.onload = resolve;
      image.onerror = reject;
      image.src = nextURL;
    });
    if (currentURL) URL.revokeObjectURL(currentURL);
    currentURL = nextURL;
    image.hidden = false;
  } catch {
    if (nextURL) URL.revokeObjectURL(nextURL);
    image.hidden = true;
  }
  setTimeout(refreshChat, 500);
}
void refreshChat();

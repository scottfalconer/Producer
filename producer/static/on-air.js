import { createPublicSurface } from "/design-system/components.js";

const surface = createPublicSurface(document.body, {
  align: new URLSearchParams(location.search).get("align"),
});
if (new URLSearchParams(location.search).get("preview") === "1") {
  document.body.classList.add("preview");
  document.querySelector("#preview").hidden = false;
  surface.render({
    context: {
      subject: "Applied AI, in practice",
      purpose: "Making agent-assisted work visible and useful",
    },
    graphic: {
      kind: "remark",
      line: "An API lets software components communicate.",
      attribution: "Producer",
    },
  });
} else {
  async function poll() {
    try {
      const r = await fetch("/api/on-air", {
        cache: "no-store",
        signal: AbortSignal.timeout(2000),
      });
      if (!r.ok) throw Error();
      const data = await r.json();
      const label = document.querySelector("#rehearsalLabel");
      if (label) {
        label.textContent = data.test_label || "Local rehearsal · recorded show + simulated Twitch";
        label.hidden = !data.test_label && location.pathname !== "/rehearsal" && !data.rehearsal;
      }
      surface.render(data);
    } catch {
      surface.clear();
      const label = document.querySelector("#rehearsalLabel");
      if (label) label.hidden = true;
    }
    setTimeout(poll, 500);
  }
  void poll();
}

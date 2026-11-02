import { GRAPHIC_TIMING, toTimingStyle } from "./timing.js";

const PUBLIC_KINDS = new Set(Object.keys(GRAPHIC_TIMING));
const PUBLIC_VISUALS = Object.freeze({
  canada: Object.freeze({
    src: "/visuals/canada.svg",
    title: "Canada",
    alt: "Map of Canada",
    attribution: "Natural Earth · public domain",
    embeddedCaption: true,
  }),
});
const text = (value, maximum = 240) => typeof value === "string" ? value.trim().slice(0, maximum) : "";

function node(root, selector, tag, className, parent = root) {
  let result = root.querySelector(selector);
  if (!result) {
    result = document.createElement(tag);
    parent.append(result);
  }
  result.classList.add(className);
  return result;
}
function setText(element, value) {
  if (element.textContent !== value) element.textContent = value;
}

// Bind production IDs in place, or create the same components in an empty catalog mount.
export function createPublicSurface(root, options = {}) {
  root.classList.add("producer-public-surface");
  root.dataset.align = options.align === "right" ? "right" : "left";
  root.dataset.motion = options.motion === false ? "off" : "on";
  const context = node(root, "#context, .producer-context", "aside", "producer-context");
  const subject = node(root, "#subject, .producer-context__subject", "strong", "producer-context__subject", context);
  const purpose = node(root, "#purpose, .producer-context__purpose", "span", "producer-context__purpose", context);
  const card = node(root, "#graphic, .producer-card", "article", "producer-card");
  const attribution = node(root, "#attribution, .producer-card__attribution", "div", "producer-card__attribution", card);
  const line = node(root, "#line, .producer-card__line", "div", "producer-card__line", card);
  const progress = node(root, ".producer-card__progress", "span", "producer-card__progress", card);
  const captions = node(root, "#subtitles, .producer-captions", "div", "producer-captions");
  const visual = node(root, ".producer-visual", "figure", "producer-visual");
  const visualImage = node(root, ".producer-visual__image", "img", "producer-visual__image", visual);
  const visualCaption = node(root, ".producer-visual__caption", "figcaption", "producer-visual__caption", visual);
  const visualTitle = node(root, ".producer-visual__title", "strong", "producer-visual__title", visualCaption);
  const visualAttribution = node(root, ".producer-visual__attribution", "span", "producer-visual__attribution", visualCaption);
  context.setAttribute("aria-live", "off");
  card.setAttribute("aria-live", "polite");
  card.setAttribute("aria-atomic", "true");
  progress.setAttribute("aria-hidden", "true");
  captions.setAttribute("aria-live", "off");
  visual.setAttribute("aria-live", "polite");
  progress.hidden = true;
  let current = {}, key = "", deadlineKey = "", expiryTimer = null, exitTimer = null, visualTimer = null, resizeFrame = null, destroyed = false;

  function clearVisual() {
    clearTimeout(visualTimer);
    visual.hidden = true;
    visualImage.removeAttribute("src");
    visualImage.alt = "";
    delete visual.dataset.assetId;
    setText(visualTitle, "");
    setText(visualAttribution, "");
  }
  visualImage.onerror = clearVisual;

  function renderVisual(value) {
    clearTimeout(visualTimer);
    let asset = value?.kind === "map" && typeof value.asset_id === "string"
      && Object.hasOwn(PUBLIC_VISUALS, value.asset_id)
      ? PUBLIC_VISUALS[value.asset_id] : null;
    if (value?.kind === "asset"
      && typeof value.asset_id === "string"
      && /^img-[0-9a-f]{16}$/.test(value.asset_id)
      && value.src === "/show-assets/" + value.asset_id
      && typeof value.title === "string" && value.title.length <= 120
      && typeof value.alt === "string" && value.alt.length <= 240
      && Number.isFinite(value.until))
      asset = { src: value.src, title: value.title, alt: value.alt, attribution: "", embeddedCaption: true };
    const expired = Number.isFinite(value?.until) && value.until * 1000 <= Date.now();
    if (!asset || expired || (value.src && value.src !== asset.src)) {
      clearVisual();
      return;
    }
    if (visualImage.getAttribute("src") !== asset.src) visualImage.src = asset.src;
    visualImage.alt = asset.alt;
    visual.dataset.assetId = value.asset_id;
    setText(visualTitle, asset.title);
    setText(visualAttribution, asset.attribution);
    visualCaption.hidden = asset.embeddedCaption === true;
    visual.hidden = false;
    if (Number.isFinite(value.until))
      visualTimer = setTimeout(clearVisual, Math.max(0, value.until * 1000 - Date.now()));
  }

  function clearCard() {
    clearTimeout(expiryTimer);
    clearTimeout(exitTimer);
    card.hidden = true;
    card.removeAttribute("data-enter");
    card.removeAttribute("data-exit");
    progress.hidden = true;
    setText(line, "");
    setText(attribution, "");
    key = "";
    deadlineKey = "";
  }
  const fits = element => element.scrollHeight <= element.clientHeight + 1;
  function fit() {
    if (destroyed || !root.clientWidth || !root.clientHeight) return;
    // Source wording is omitted rather than silently shortened to force it into a slot.
    if (!card.hidden && (!fits(line) || !fits(card) || attribution.scrollWidth > attribution.clientWidth + 1)) {
      card.hidden = true;
      card.dataset.omitted = "text-does-not-fit";
    }
    if (!context.hidden && (!fits(context) || !fits(subject) || !fits(purpose)
      || subject.scrollWidth > subject.clientWidth + 1 || purpose.scrollWidth > purpose.clientWidth + 1)) {
      context.hidden = true;
      context.dataset.omitted = "text-does-not-fit";
    }
    if (!captions.hidden && !fits(captions)) {
      captions.hidden = true;
      captions.dataset.omitted = "text-does-not-fit";
    }
  }
  function render(state = {}) {
    if (destroyed) return;
    current = state;
    renderVisual(state.visual);
    const c = state.context;
    const validContext = c && typeof c.subject === "string" && c.subject.trim() && c.subject.length <= 80 && (!c.purpose || (typeof c.purpose === "string" && c.purpose.length <= 120));
    const contextExpired = Number.isFinite(c?.until) && c.until * 1000 <= Date.now();
    context.hidden = !validContext || contextExpired;
    delete context.dataset.omitted;
    setText(subject, context.hidden ? "" : text(c.subject, 80));
    setText(purpose, context.hidden ? "" : text(c.purpose, 120));
    const caption = state.caption;
    const rawCaption = typeof caption === "string" ? caption : caption?.text;
    const captionText = typeof rawCaption === "string" && rawCaption.length <= 240 ? rawCaption.trim() : "";
    captions.hidden = !captionText;
    setText(captions, captionText);
    delete captions.dataset.omitted;
    if (typeof rawCaption === "string" && rawCaption.length > 240) captions.dataset.omitted = "text-too-long";
    root.classList.toggle("has-captions", Boolean(captionText));
    const g = state.graphic;
    const sticky = g?.kind === "question"
      && (g.sticky === true || g.awaiting_response === true)
      && g.editorial_timing !== true;
    const expired = !sticky && Number.isFinite(g?.until) && g.until * 1000 <= Date.now();
    if (!g || !PUBLIC_KINDS.has(g.kind) || typeof g.line !== "string" || !g.line.trim() || g.line.length > 180 || (g.attribution && (typeof g.attribution !== "string" || g.attribution.length > 240)) || expired) {
      clearCard();
      fit();
      return;
    }
    const nextKey = [g.id || "", g.kind, g.line, g.attribution || ""].join("\n");
    if (nextKey !== key) {
      clearCard();
      key = nextKey;
      card.dataset.kind = g.kind;
      setText(attribution, text(g.attribution) || (g.kind === "result" ? "Reported on stream" : "Producer · AI"));
      setText(line, g.kind === "question" ? g.line : g.line.trim());
      for (const [property, value] of Object.entries(toTimingStyle(g.kind))) card.style.setProperty(property, value);
      card.hidden = false;
      delete card.dataset.omitted;
      if (options.motion !== false) {
        void card.offsetWidth;
        card.dataset.enter = "true";
      }
    }
    const nextDeadlineKey = [sticky ? "held" : "timed", g.until || "", g.duration || ""].join("\n");
    if (nextDeadlineKey !== deadlineKey) {
      clearTimeout(expiryTimer);
      clearTimeout(exitTimer);
      card.removeAttribute("data-exit");
      progress.hidden = true;
      deadlineKey = nextDeadlineKey;
      // Use only an explicit server deadline; do not invent a live browser expiry.
      if (!sticky && Number.isFinite(g.until)) {
        const remaining = Math.max(0, g.until * 1000 - Date.now());
        const exitMs = GRAPHIC_TIMING[g.kind].exitMs;
        if (options.motion !== false && remaining >= exitMs) exitTimer = setTimeout(() => {
          card.removeAttribute("data-enter");
          card.dataset.exit = "true";
        }, remaining - exitMs);
        expiryTimer = setTimeout(clearCard, remaining);
      }
      if (!sticky && options.showProgress === true && Number.isFinite(g.until) && Number.isFinite(g.duration) && g.duration > 0) {
        const remaining = Math.max(0, Math.min(g.duration * 1000, g.until * 1000 - Date.now()));
        progress.style.setProperty("--producer-remaining-ms", remaining + "ms");
        progress.style.setProperty("--producer-progress-start", String(remaining / (g.duration * 1000)));
        progress.hidden = remaining <= 0;
      }
    }
    card.hidden = false;
    fit();
  }
  const observer = typeof ResizeObserver === "function" ? new ResizeObserver(() => {
    cancelAnimationFrame(resizeFrame);
    resizeFrame = requestAnimationFrame(() => {
      if (destroyed) return;
      key = "";
      render(current);
    });
  }) : null;
  observer?.observe(root);
  function clear() {
    current = {};
    clearCard();
    clearVisual();
    context.hidden = true;
    captions.hidden = true;
    setText(subject, ""); setText(purpose, ""); setText(captions, "");
    root.classList.remove("has-captions");
  }
  clear();
  return {
    render, clear,
    setAlign(align) { root.dataset.align = align === "right" ? "right" : "left"; },
    elements: { root, context, subject, purpose, card, attribution, line, captions, progress, visual, visualImage, visualTitle, visualAttribution },
    destroy() { clear(); destroyed = true; observer?.disconnect(); cancelAnimationFrame(resizeFrame); },
  };
}

// One quiet presenter cue; existing awareness and deadline nodes are left untouched.
export function createPresenterSurface(root) {
  root.classList.add("producer-presenter-surface");
  const cue = node(root, "#hostCue, .producer-cue", "article", "producer-cue");
  const line = node(root, "#hostCueLine, .producer-cue__line", "h1", "producer-cue__line", cue);
  const presence = node(root, "#monitorPresence, .producer-presence", "span", "producer-presence");
  cue.setAttribute("aria-live", "polite");
  cue.setAttribute("aria-atomic", "true");
  presence.setAttribute("role", "img");
  let quoted = false, value = "", resizeFrame = null, destroyed = false;
  function fitQuote() {
    if (destroyed || !quoted || !value || !root.clientWidth || !root.clientHeight) return;
    cue.hidden = false;
    delete cue.dataset.omitted;
    const bounds = root.getBoundingClientRect(), content = line.getBoundingClientRect();
    if (content.bottom > bounds.bottom + 1 || content.left < bounds.left - 1
      || content.right > bounds.right + 1 || line.scrollWidth > line.clientWidth + 1) {
      cue.hidden = true;
      cue.dataset.omitted = "text-does-not-fit";
    }
  }
  const observer = typeof ResizeObserver === "function" ? new ResizeObserver(() => {
    cancelAnimationFrame(resizeFrame);
    resizeFrame = requestAnimationFrame(fitQuote);
  }) : null;
  observer?.observe(root);
  function render(state = {}) {
    if (destroyed) return;
    quoted = state.kind === "question";
    value = quoted
      ? (typeof state.line === "string" && state.line.trim() && state.line.length <= 180 ? state.line : "")
      : text(state.line, 240);
    setText(line, value);
    cue.hidden = !value;
    cue.dataset.kind = ["signal", "warning", "editorial", "audience", "question", "timing", "recovery", "guidance"].includes(state.kind) ? state.kind : "quiet";
    delete cue.dataset.omitted;
    const status = ["active", "paused"].includes(state.presence) ? state.presence : "off";
    presence.dataset.state = status;
    const label = status === "active" ? "Selected input active" : status === "paused" ? "Monitoring paused" : "Capture off";
    presence.setAttribute("aria-label", label);
    presence.title = label;
    fitQuote();
  }
  render();
  return { render, clear: () => render(), elements: { root, cue, line, presence },
    destroy() { render(); destroyed = true; observer?.disconnect(); cancelAnimationFrame(resizeFrame); } };
}

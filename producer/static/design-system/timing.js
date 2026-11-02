// Editable design defaults, not measured reading-speed or broadcast standards.
// Live server timing remains authoritative. These values support design previews.
// A selected question uses awaiting_response and has no timed hold or progress bar.
export const GRAPHIC_TIMING = Object.freeze({
  title: Object.freeze({ label: "Topic", enterMs: 450, minHoldMs: 5000, maxHoldMs: 7000, exitMs: 300, gapMs: 45000 }),
  question: Object.freeze({ label: "Viewer question", enterMs: 450, minHoldMs: 8000, maxHoldMs: 12000, exitMs: 350, gapMs: 30000 }),
  remark: Object.freeze({ label: "Producer note", enterMs: 400, minHoldMs: 5000, maxHoldMs: 6500, exitMs: 250, gapMs: 60000 }),
  reference: Object.freeze({ label: "Reference", enterMs: 350, minHoldMs: 6000, maxHoldMs: 8000, exitMs: 250, gapMs: 20000 }),
  takeaway: Object.freeze({ label: "Takeaway", enterMs: 200, minHoldMs: 2500, maxHoldMs: 4000, exitMs: 200, gapMs: 40000 }),
  result: Object.freeze({ label: "Reported result", enterMs: 400, minHoldMs: 5000, maxHoldMs: 6500, exitMs: 250, gapMs: 60000 }),
  thread: Object.freeze({ label: "Thread", enterMs: 400, minHoldMs: 5000, maxHoldMs: 6500, exitMs: 250, gapMs: 60000 }),
  poll: Object.freeze({ label: "Audience vote", enterMs: 450, minHoldMs: 8000, maxHoldMs: 12000, exitMs: 350, gapMs: 30000 }),
});

export function countWords(text) {
  return String(text || "").trim().split(/\s+/u).filter(Boolean).length;
}

export function holdDuration(kind, text, overrides = {}) {
  const timing = { ...(GRAPHIC_TIMING[kind] || GRAPHIC_TIMING.remark), ...overrides };
  const minimum = Number.isFinite(timing.minHoldMs) ? Math.max(0, timing.minHoldMs) : 5000;
  const maximum = Number.isFinite(timing.maxHoldMs) ? Math.max(minimum, timing.maxHoldMs) : 6500;
  return Math.min(maximum, Math.max(minimum, 2000 + countWords(text) * 350));
}

export function toTimingStyle(kind) {
  const timing = GRAPHIC_TIMING[kind] || GRAPHIC_TIMING.remark;
  return {
    "--producer-enter-ms": timing.enterMs + "ms",
    "--producer-exit-ms": timing.exitMs + "ms",
  };
}

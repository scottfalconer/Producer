export const copy = (x) => structuredClone(x);
export const OBSERVATION_SECONDS = 8;
export const complete = (i) => i.status === "covered" || i.status === "skipped";
export const clockText = (s) => {
  const n = Math.floor(Math.abs(s));
  return (
    (s < 0 ? "−" : "") +
    String(Math.floor(n / 60)).padStart(2, "0") +
    ":" +
    String(n % 60).padStart(2, "0")
  );
};
export const memoryFor = (brief) => ({
  items: brief.intentions.map((i) => ({
    ...copy(i),
    status: "open",
    locked: false,
    evidence: "",
    at: 0,
  })),
  closing: false,
  through: 0,
});
export const unfinished = (m) => (m?.items || []).filter((i) => !complete(i));
export function returnTo(m) {
  const items = unfinished(m);
  return (
    items.find((i) => i.status === "started") ||
    items.find((i) => i.priority === "essential") ||
    items[0]
  );
}
export function shortLine(s) {
  const words = String(s).split(/\s+/);
  return words.length > 10 ? words.slice(0, 10).join(" ") + "…" : s;
}
export function reminder(m, kind, now) {
  const items = unfinished(m),
    item =
      kind === "remaining"
        ? items.find((i) => i.priority === "essential") || items[0]
        : returnTo(m);
  return {
    id: "manual-" + kind + "-" + now,
    kind: "memory",
    line: item
      ? shortLine(item.next_action || item.text)
      : "Nothing left to protect.",
    support: item
      ? kind === "remaining"
        ? "Still to cover · " +
          items.filter((i) => i.priority === "essential").length +
          " essentials"
        : item.text
      : "You can finish in your own way.",
    intention_id: item?.id || "",
    at: now,
    until: now + 30,
    manual: true,
  };
}
export function deadlineNote(brief, elapsed) {
  if (brief?.timing?.mode !== "hard") return "";
  const left = brief.timing.minutes * 60 - elapsed;
  if (left > 300) return "";
  return left <= 0 ? "Hard stop reached" : `${Math.ceil(left / 60)} min left`;
}
export function landing(m, brief, now) {
  if (
    !m?.closing &&
    !(brief?.timing?.mode === "hard" && brief.timing.minutes * 60 - now <= 300)
  )
    return null;
  const item = unfinished(m).find((i) => i.priority === "essential");
  return item
    ? {
        id: "close-" + item.id,
        kind: "timing",
        line: shortLine(item.text),
        support: "Before you finish",
        intention_id: item.id,
        at: now,
        until: now + 30,
      }
    : null;
}
// A note to glance at, not an interruption or a claim about live speech.
// The planned order is a fallback only; evidence-backed memory can advance it.
export function plannedReference(memory, brief, elapsed = 0) {
  if (
    !memory || memory.closing ||
    (brief?.timing?.mode === "hard" && brief.timing.minutes * 60 - elapsed <= 60)
  ) return null;
  const planned = (brief?.intentions || []).map((note) => ({
    note,
    state: memory.items?.find((item) => item.id === note.id),
  }));
  const next = planned.find(({ state }) => state?.status === "open");
  if (!next) return null;
  const progressed = planned.some(({ state }) => state && state.status !== "open");
  const uncertain = planned.slice(0, planned.indexOf(next)).some(({ state }) => state?.status === "uncertain");
  const words = String(next.note.next_action || next.note.text || "").trim().split(/\s+/);
  if (!words[0]) return null;
  const action = words.slice(0, 8).join(" ") + (words.length > 8 ? "…" : "");
  return {
    kind: "guidance",
    line: `${uncertain ? "When ready" : progressed ? "Next" : "Start with"}: ${action}`,
    intention_id: next.note.id,
  };
}
export function presenterReference(memory, brief, elapsed = 0) {
  // A model decision, including deliberate quiet, supersedes the bootstrap notes.
  if (memory?.reference_decided === true) {
    const reference = memory.presenter_reference;
    return typeof reference?.line === "string" && reference.line.trim()
      ? { kind: "guidance", line: reference.line, intention_id: reference.intention_id || "" }
      : null;
  }
  return plannedReference(memory, brief, elapsed);
}
export class CueDesk {
  constructor() {
    this.active = null;
    this.queue = [];
    this.seen = new Set();
  }
  add(cue, now) {
    if (!cue || this.seen.has(cue.id) || cue.until <= now) return;
    this.seen.add(cue.id);
    if (this.seen.size > 300) this.seen.delete(this.seen.values().next().value);
    if (cue.manual || cue.urgent || cue.kind === "signal") {
      this.active = cue;
      return;
    }
    // Keep the producer's newest choice, not a backlog ordered by cue category.
    this.queue = [cue];
  }
  tick(now, memory) {
    const valid = (c) =>
      c &&
      c.at <= now &&
      now < c.until &&
      (!(c.manual || c.kind === "timing") || !c.intention_id ||
        !memory?.items.some((i) => i.id === c.intention_id && complete(i)));
    this.queue = this.queue.filter(valid);
    if (this.active && !valid(this.active)) this.active = null;
    const protectedCue = this.active?.manual || this.active?.urgent || this.active?.kind === "signal";
    if (!this.active || (this.queue.length && !protectedCue)) {
      this.active = this.queue.shift() || null;
    }
    return this.active;
  }
  withdraw(id) {
    this.queue = this.queue.filter((c) => c.id !== id);
    if (this.active?.id === id) this.active = null;
  }
  clearEditorial() {
    const protectedCue = (cue) => cue?.manual || cue?.urgent || cue?.kind === "signal";
    this.queue = this.queue.filter(protectedCue);
    if (!protectedCue(this.active)) this.active = null;
  }
  keepEditorial(now, lifetime = 22) {
    if (this.active?.modelDirected && this.active.kind !== "signal"
      && !this.active.manual && !this.active.urgent)
      this.active.until = now + lifetime;
  }
  dismiss() {
    this.active = null;
  }
  clear() {
    this.active = null;
    this.queue = [];
  }
}
export const exampleBrief = {
  title: "A useful detour",
  goal: "Show how a presenter can follow a question and return to an unfinished demonstration.",
  audience: "Builders and streamers new to this project.",
  context: "Example brief. No factual research findings supplied.",
  tip: "Sparse cues. Let the conversation wander.",
  recovery: "What is the next unfinished action?",
  timing: { mode: "flexible", minutes: 30 },
  rundown: [
    { name: "Why this approach", minutes: 8 },
    { name: "Try it together", minutes: 17 },
    { name: "What to take away", minutes: 5 },
  ],
  intentions: [
    {
      id: "why",
      text: "Explain why we changed direction",
      kind: "explain",
      priority: "essential",
      next_action: "Explain the pivot",
    },
    {
      id: "demo",
      text: "Demonstrate a detour and return",
      kind: "demonstrate",
      priority: "essential",
      next_action: "Show the unfinished step",
    },
    {
      id: "repo",
      text: "Share the repository",
      kind: "share",
      priority: "essential",
      next_action: "Share the repository",
    },
    {
      id: "architecture",
      text: "Explore the implementation",
      kind: "explain",
      priority: "optional",
      next_action: "Explain one design choice",
    },
  ],
  research: [],
};

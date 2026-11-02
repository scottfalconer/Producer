export const formatClock = (t) => {
  const v = Math.floor(Math.abs(t));
  return (
    (t < 0 ? "−" : "") +
    String(Math.floor(v / 60)).padStart(2, "0") +
    ":" +
    String(v % 60).padStart(2, "0")
  );
};
export function schedule(brief) {
  let start = 0;
  return brief.rundown.map((b) => {
    const end = start + b.minutes * 60;
    const item = { ...b, start, end };
    start = end;
    return item;
  });
}
export function activeCue(events, time, dismissed) {
  const rank = { signal: 3, timing: 2, editorial: 1 };
  return (
    events
      .filter((c) => c.at <= time && time < c.until && !dismissed.has(c.id))
      .sort((a, b) => rank[b.kind] - rank[a.kind] || b.at - a.at)[0] || null
  );
}
export function parseRundown(answer) {
  const beats = answer
    .trim()
    .split(/\n/)
    .map((line) => {
      const m = line.match(
        /^\s*(\d{1,3})\s*(?:min(?:ute)?s?)?\s*[-–:|]\s*(.+?)\s*$/i,
      );
      if (!m || +m[1] < 1 || +m[1] > 120 || m[2].length > 100)
        throw Error("Use one line per beat, such as 5 – Open the room.");
      return { minutes: +m[1], name: m[2] };
    });
  if (beats.length > 12) throw Error("Use up to twelve beats.");
  return beats;
}

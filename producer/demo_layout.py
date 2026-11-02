"""Small read-only projections for display surfaces, without lifecycle heartbeats."""

import copy
import math


def presenter_mirror_snapshot(session, *, source_available, source_time):
    """Project only the current cue for an explicitly configured private mirror.

    The caller holds the show lock. This deliberately avoids director.snapshot(),
    which refreshes the source heartbeat and also includes private show memory.
    This projection is not exposed by the public demo-layout HTTP route.
    """
    empty = {"confidence": None, "presence": "off", "elapsed": 0.0}
    if not session or not source_available(session):
        return empty
    director = session.get("director")
    if not director:
        return empty
    state = director.core.snapshot()
    if not state.get("connected") or state.get("closed") or state.get("error"):
        return empty
    elapsed = source_time(session)
    if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed):
        return empty
    cue = session.get("confidence")
    line = cue.get("line") if isinstance(cue, dict) else None
    if not isinstance(line, str) or not line.strip():
        cue = None
    else:
        kinds = {"signal", "warning", "editorial", "audience", "question", "timing", "recovery", "guidance"}
        cue = {"line": line, "kind": cue.get("kind") if cue.get("kind") in kinds else "editorial"}
    return {"confidence": copy.deepcopy(cue), "presence": "active", "elapsed": max(0.0, elapsed)}

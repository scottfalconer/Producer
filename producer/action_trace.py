"""Opt-in local screen-action receipts and API-free routing/render replay.

This is an action boundary journal, not a model transcript: no speech, media,
prompt, private working notes, editor advice or model reasoning is retained.
"""

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
import time


SCHEMA = "producer.actions.v1"
MAX_LINE = 1000000
MAX_RECORDS = 20000
KINDS = {"trace_started", "session_ready", "response_requested", "response_created",
         "response_done", "function_event", "function_call", "tool_receipt", "screen_state",
         "session_closed", "local_error", "input_frame", "screen_commit", "production_commit", "production_chat"}
HIDDEN = {"working_notes", "director_notes", "deeper_advice", "reasoning", "analysis",
          "chain_of_thought", "notes", "brief", "show_brief", "transcript", "audio",
          "image", "image_url", "api_key", "authorization", "token", "csrf",
          "research_packet"}
SECRET = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}|\bBearer\s+[A-Za-z0-9._-]{12,}", re.I)
STATE_KEYS = ("revision", "source_time", "confidence", "public", "audience", "assets",
              "offline", "public_output_enabled", "audience_source", "screen_frames", "production",
              "confidence_timing_policy")

PRODUCTION_FIELDS = ("id", "plan_id", "kind", "text", "state", "evidence_at", "attempted_at", "detail",
                     "remote_id", "simulated", "channel_id", "sender_id", "sender_login", "sender_mode", "display_name",
                     "message", "request_key", "request_fingerprint", "choices", "duration", "poll_mode", "category_id")


def production_receipt(value):
    """Only public action metadata; never retain arbitrary adapter responses."""
    if not isinstance(value, dict):
        return None
    return _safe({key: value[key] for key in PRODUCTION_FIELDS if key in value})


def production_state(value):
    if not isinstance(value, dict):
        return None
    result = {key: value[key] for key in ("policy", "offline", "enabled") if key in value}
    capabilities = value.get("capabilities")
    if isinstance(capabilities, dict):
        result["capabilities"] = {key: capabilities[key] for key in
            ("chat", "reference", "marker", "title", "category", "poll") if key in capabilities}
    elif isinstance(capabilities, list):
        result["capabilities"] = [item for item in capabilities[:12] if item in (
            "share_on_air", "execute_plan", "create_poll", "create_marker", "set_title", "set_category", "get_poll_result")]
    result["reviewed_plans"] = [production_receipt(plan)
        for plan in value.get("reviewed_plans", [])[:20] if isinstance(plan, dict)]
    result["shareable_outputs"] = [{key: row[key] for key in ("output_key", "slot", "text", "kind") if key in row}
        for row in value.get("shareable_outputs", [])[:8] if isinstance(row, dict)]
    result["receipts"] = [production_receipt(row) for row in value.get("receipts", [])[-20:]
                           if isinstance(row, dict)]
    return _safe(result)


def poll_receipt(value):
    if not isinstance(value, dict):
        return None
    result = {key: value[key] for key in ("id", "status", "simulated", "counts", "line", "detail") if key in value}
    result["choices"] = [{key: row[key] for key in ("title", "votes") if key in row} if isinstance(row, dict) else row
                          for row in value.get("choices", [])[:5]]
    return _safe(result)


def _safe(value, depth=0):
    if depth > 12:
        raise ValueError("Action trace nesting exceeds its bound.")
    if isinstance(value, dict):
        return {str(k): _safe(v, depth + 1) for k, v in value.items()
                if str(k).casefold() not in HIDDEN}
    if isinstance(value, (list, tuple)):
        return [_safe(v, depth + 1) for v in value[:128]]
    if isinstance(value, str):
        return SECRET.sub("[credential redacted]", value[:60000])
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError("Action trace only accepts bounded JSON data.")


def visible_state(state):
    """Keep both exact screen snapshots and the local routing prerequisites."""
    if not isinstance(state, dict):
        return None
    result = {key: state[key] for key in STATE_KEYS if key in state}
    if "production" in result:
        result["production"] = production_state(result["production"])
    return _safe(result)


def tool_arguments(raw):
    """Normalize complete calls. Invalid JSON is identified without saving prose."""
    if not isinstance(raw, str):
        return {"arguments": None, "arguments_error": "not_a_string"}
    digest = hashlib.sha256(raw.encode()).hexdigest()
    try:
        args = json.loads(raw)
        if not isinstance(args, dict):
            raise ValueError()
        arguments = _safe(args)
    except (ValueError, TypeError):
        return {"arguments": None, "arguments_error": "invalid_json_object",
                "arguments_sha256": digest, "arguments_length": len(raw)}
    return {"arguments": arguments, "arguments_sha256": digest,
            "arguments_length": len(raw)}


def _payload(kind, payload):
    if not isinstance(payload, dict):
        raise ValueError("Invalid action trace payload.")
    allowed = {
        "trace_started": ("model", "purpose", "source_id", "source_sha256", "seconds"),
        "session_ready": ("model",),
        "response_requested": ("response_sequence", "source_through", "state"),
        "response_created": ("response_sequence", "response_id"),
        "response_done": ("response_sequence", "response_id", "status"),
        "function_event": ("event_type", "call_id", "name", "response_id", "response_sequence",
                           "arguments", "arguments_sha256", "arguments_length", "arguments_error"),
        "function_call": ("call_id", "name", "response_id", "response_sequence", "arguments",
                          "arguments_sha256", "arguments_length", "arguments_error", "expected_state"),
        "tool_receipt": ("call_id", "name", "response_id", "response_sequence", "receipt"),
        "screen_state": ("state", "status", "through"),
        "screen_commit": ("revision",),
        "production_commit": ("revision", "action", "plan_id"),
        "production_chat": ("event",),
        "session_closed": ("reason", "error", "source_through"),
        "local_error": ("phase", "error"),
        "input_frame": ("frame_id", "source_time", "sha256"),
    }[kind]
    result = {key: payload[key] for key in allowed if key in payload}
    for key in ("state", "expected_state"):
        if key in result:
            result[key] = visible_state(result[key])
    if "receipt" in result:
        receipt = result["receipt"]
        result["receipt"] = {k: receipt[k] for k in ("ok", "error", "reason", "detail", "state", "queued", "id", "simulated", "duplicate", "receipt", "poll", "show_stopped") if k in receipt} if isinstance(receipt, dict) else {"ok": False, "error": "invalid_local_receipt"}
        if "state" in result["receipt"]:
            result["receipt"]["state"] = visible_state(result["receipt"]["state"])
        if "receipt" in result["receipt"]:
            result["receipt"]["receipt"] = production_receipt(result["receipt"]["receipt"])
        if "poll" in result["receipt"]:
            result["receipt"]["poll"] = poll_receipt(result["receipt"]["poll"])
    if kind == "production_chat":
        event = result.get("event") or {}
        result["event"] = {key: event[key] for key in ("type", "id", "at", "user_id", "author", "text") if key in event}
    return _safe(result)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


class ActionTrace:
    """One private, append-only JSONL action journal with ordered hash receipts."""

    def __init__(self, path, *, clock=time.monotonic, source_clock=lambda: 0, metadata=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock, self.source_clock = clock, source_clock
        self.origin = clock()
        self.lock = threading.Lock()
        self.sequence, self.previous, self.closed = 0, None, False
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self.file = os.fdopen(descriptor, "w", encoding="utf-8")
        self.emit("trace_started", metadata or {})

    def emit(self, kind, payload):
        if kind not in KINDS:
            raise ValueError("Unknown action trace event.")
        clean = _payload(kind, payload)
        with self.lock:
            if self.closed:
                raise ValueError("Action trace is closed.")
            wall, source = self.clock() - self.origin, self.source_clock()
            if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0 for v in (wall, source)):
                raise ValueError("Invalid action trace clock.")
            if self.sequence >= MAX_RECORDS:
                raise ValueError("Action trace event limit reached.")
            event = {"schema": SCHEMA, "sequence": self.sequence, "wall_s": round(wall, 6),
                     "source_s": round(source, 6), "kind": kind, "payload": clean,
                     "previous_sha256": self.previous}
            event["sha256"] = hashlib.sha256(_canonical(event).encode()).hexdigest()
            encoded = _canonical(event)
            if len(encoded.encode()) > MAX_LINE:
                raise ValueError("Action trace event is too large.")
            self.file.write(encoded + "\n")
            self.file.flush()
            self.previous = event["sha256"]
            self.sequence += 1
            return copy.deepcopy(event)

    def close(self):
        with self.lock:
            if not self.closed:
                self.file.flush()
                try:
                    os.fsync(self.file.fileno())
                finally:
                    self.file.close()
                    self.closed = True


def load_trace(path):
    """Reject truncated, reordered or altered journals before replaying actions."""
    events, previous, last_wall = [], None, 0
    with Path(path).open(encoding="utf-8") as source:
        for line in source:
            if len(events) >= MAX_RECORDS or len(line.encode()) > MAX_LINE:
                raise ValueError("Action trace exceeds its replay bounds.")
            try:
                event = json.loads(line)
                checksum = event.pop("sha256")
                valid = (event["schema"] == SCHEMA and event["sequence"] == len(events)
                         and event["previous_sha256"] == previous
                         and event["kind"] in KINDS and event["wall_s"] >= last_wall
                         and hashlib.sha256(_canonical(event).encode()).hexdigest() == checksum)
            except (ValueError, KeyError, TypeError):
                valid = False
            if not valid:
                raise ValueError("Action trace integrity check failed.")
            event["sha256"] = checksum
            events.append(event)
            previous, last_wall = checksum, event["wall_s"]
    if not events or events[0]["kind"] != "trace_started":
        raise ValueError("Action trace has no header.")
    return events


def capture_scene(director, trace):
    """Sample and journal on one side of a screen commit, using its exact clock."""
    with director.lock:
        state = director._state()
        event = trace.emit("screen_state", {"state": state})
        return {"wall_s": event["wall_s"], "source_s": state["source_time"],
                "confidence": state["confidence"], **state["public"], "revision": state["revision"]}


def normalized_scene(state):
    """Compare displayed meaning; renderer identities and wall-clock TTLs vary."""
    state = visible_state(state) or {}
    volatile = {"id", "displayed_at", "until", "fresh_until"}

    def strip(value):
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items() if k not in volatile}
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value

    result = strip({"confidence": state.get("confidence"), "public": state.get("public")})
    if "confidence_timing_policy" in state:
        result["confidence_timing_policy"] = state["confidence_timing_policy"]
    if "production" in state:
        result["production"] = normalized_production(state["production"])
    return result


def normalized_production(value):
    """Action identity and outcomes matter; absolute attempt timestamps do not."""
    if isinstance(value, dict):
        return {key: normalized_production(item) for key, item in value.items()
                if key not in ("attempted_at", "checked")}
    if isinstance(value, list):
        return [normalized_production(item) for item in value]
    return value


def receipts_match(actual, expected, *, production=False):
    if not isinstance(expected, dict) or actual.get("ok") != expected.get("ok"):
        return False
    if "state" in expected and normalized_scene(actual.get("state")) != normalized_scene(expected["state"]):
        return False
    if not expected.get("ok") and any(actual.get(k) != expected.get(k) for k in ("error", "reason")):
        return False
    if production:
        # A successful screen update cannot stand in for a delivered chat post,
        # a poll, or a duplicate-suppressed production action.
        keys = ("simulated", "duplicate", "receipt", "poll", "show_stopped")
        return normalized_production({k: visible_receipt(actual)[k] for k in keys if k in actual}) == normalized_production({k: visible_receipt(expected)[k] for k in keys if k in expected})
    return True


def replay_actions(events, apply, *, before_call=None, state=None, production_apply=None):
    """Re-run recorded screen tools through a supplied local router, never an API.

    The caller restores the pinned assets/inbox and clock in before_call when
    required. Original examined-state revisions are preserved, including stale
    revisions. Recorded results are comparison evidence, never applied output.
    """
    receipts = {e["payload"].get("call_id"): e["payload"].get("receipt")
                for e in events if e["kind"] == "tool_receipt"}
    results = []
    for event in events:
        call = event["payload"]
        if event["kind"] != "function_call" or call.get("name") not in ("update_screens", "production_action"):
            continue
        if before_call:
            before_call(copy.deepcopy(event))
        try:
            if call.get("name") == "production_action" and production_apply is None:
                actual = {"ok": False, "error": "production_replay_unavailable"}
            elif not isinstance(call.get("arguments"), dict):
                rejected_json = (call.get("arguments_error") == "invalid_json_object"
                                 and call.get("arguments_length", 0) <= 16384)
                actual = {"ok": False, "error": "tool_arguments_or_local_state_rejected" if rejected_json else "invalid_tool_call"}
                if rejected_json and state is not None:
                    actual["state"] = state()
            else:
                callback = production_apply if call.get("name") == "production_action" else apply
                actual = callback(copy.deepcopy(call["arguments"]), copy.deepcopy(call.get("expected_state")))
        except (ValueError, TypeError, KeyError) as exc:
            actual = {"ok": False, "error": "tool_arguments_or_local_state_rejected", "detail": str(exc)[:240]}
            if state is not None:
                actual["state"] = state()
        expected = receipts.get(call.get("call_id"))
        same = receipts_match(actual, expected, production=call.get("name") == "production_action")
        results.append({"call_id": call.get("call_id"), "sequence": event["sequence"],
                        "wall_s": event["wall_s"], "source_s": event["source_s"],
                        "matches_recording": bool(same), "expected": expected, "actual": visible_receipt(actual)})
    return results


def visible_receipt(receipt):
    return _payload("tool_receipt", {"receipt": receipt})["receipt"]

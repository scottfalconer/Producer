"""Bounded production tools for the live director, with an offline-first transport.

Editorial choices belong to the director. This bridge selects only a reviewed
payload or an explicitly simulated action, and never turns private notes into a
chat message. It makes no Twitch connection of its own.
"""

import copy
import hashlib
import json
import math
import re
import threading

from production import validate_production


POLICY = "realtime-production-v1"
MAX_ACTIONS = 200
MAX_RECEIPTS = 20
ACTIONS = (
    "share_on_air", "execute_plan", "create_poll", "create_marker",
    "set_title", "set_category", "get_poll_result",
)
_DYNAMIC = {
    "create_poll": "poll", "create_marker": "marker",
    "set_title": "title", "set_category": "category",
}


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def public_outputs(public):
    """Build stable identities from currently visible public text, never UUIDs."""
    if not isinstance(public, dict):
        return []
    outputs = []
    card = public.get("graphic")
    if isinstance(card, dict) and not card.get("redacted"):
        text, attribution = card.get("line"), card.get("attribution", "")
        if isinstance(text, str) and text.strip() and isinstance(attribution, str):
            message = text + (" — " + attribution if attribution else "")
            if len(message) <= 500:
                outputs.append({"slot": "card", "text": message,
                                "kind": str(card.get("kind", ""))[:30]})
    context = public.get("context")
    if isinstance(context, dict) and not context.get("redacted"):
        subject, purpose = context.get("subject"), context.get("purpose", "")
        if isinstance(subject, str) and subject.strip() and isinstance(purpose, str):
            message = subject + (" — " + purpose if purpose else "")
            if len(message) <= 500:
                outputs.append({"slot": "context", "text": message, "kind": "context"})
    return [{"output_key": "public-" + _digest(row)[:32], **row} for row in outputs]


def _receipt(row, offline):
    """Only public payloads and deterministic receipts are model-visible."""
    result = {key: copy.deepcopy(row[key]) for key in (
        "plan_id", "kind", "text", "state", "detail", "remote_id", "evidence_at",
        "sender_id", "sender_login", "sender_mode", "display_name", "message", "channel_id",
        "choices", "duration", "poll_mode", "category_id",
    ) if key in row}
    result["simulated"] = offline
    return result


class _VisibleShareAdapter:
    """Recheck public eligibility after reservation without locking platform I/O."""

    def __init__(self, adapter, current):
        self.adapter, self.current = adapter, current

    def execute(self, plan):
        if not self.current():
            return {"state": "needs_attention",
                    "detail": "The public output changed before dispatch; nothing sent."}
        return self.adapter.execute(plan)


class DirectorProduction:
    def __init__(self, production, *, offline, active, public_enabled,
                 public_state, clock, live_enabled=False):
        if type(offline) is not bool or type(live_enabled) is not bool:
            raise ValueError("Choose an explicit production transport.")
        self.production = production
        self.offline, self.live_enabled = offline, live_enabled
        self.active, self.public_enabled = active, public_enabled
        self.public_state, self.clock = public_state, clock
        # Serializes reservations, not screen/audio state; never held by snapshot.
        self._reservation = threading.RLock()

    def _available(self):
        adapter = self.production.adapter
        return bool(adapter and (
            (self.offline and getattr(adapter, "platform", None) == "fixture")
            or (not self.offline and self.live_enabled
                and getattr(adapter, "platform", None) == "twitch")
        ))

    def _check(self):
        if not self.active():
            raise ValueError("The selected show has stopped.")
        if not self.public_enabled():
            raise ValueError("The owner has disabled public output for this show.")
        if not self._available():
            if not self.offline:
                raise ValueError("Live Twitch production is not enabled; use the offline test.")
            raise ValueError("Offline production requires the simulated Twitch transport.")

    def snapshot(self, publicstate=None):
        enabled = bool(self.active() and self.public_enabled() and self._available())
        public = self.public_state() if publicstate is None else publicstate
        rows = self.production.ledger.snapshot()
        return {
            "policy": POLICY, "offline": self.offline, "enabled": enabled,
            "capabilities": list(ACTIONS) if enabled and self.offline else (
                ["share_on_air", "execute_plan"] if enabled else []),
            "reviewed_plans": copy.deepcopy(self.production.plans["actions"]),
            "shareable_outputs": public_outputs(public) if enabled else [],
            "receipts": [_receipt(row, self.offline) for row in rows[-MAX_RECEIPTS:]],
        }

    @staticmethod
    def _arguments(args, allowed):
        if not isinstance(args, dict) or set(args) - ({"action"} | set(allowed)):
            raise ValueError("Unexpected production arguments.")

    @staticmethod
    def _identity(value, field):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
            raise ValueError(f"Choose a stable {field} using letters, numbers, dash or underscore.")
        return value

    def _dynamic_plan(self, action, args):
        if not self.offline:
            raise ValueError("Live production requires an existing reviewed plan_id.")
        self._arguments(args, ("request_id", "text", "choices", "duration", "poll_mode", "category_id"))
        request_id = self._identity(args.get("request_id"), "request_id")
        request_key = "dynamic:" + request_id
        plan = {
            "id": "rt-" + _digest(request_key)[:32], "kind": _DYNAMIC[action],
            "when": "Chosen by Producer in this offline show", "text": args.get("text", ""),
            "choices": args.get("choices", []), "duration": args.get("duration", 0),
            "poll_mode": args.get("poll_mode", "native"),
            "category_id": args.get("category_id", ""), "intention_id": "",
        }
        plan = validate_production({"preferences": "Model-led offline production", "actions": [plan]})["actions"][0]
        return {**plan, "request_key": request_key,
                "request_fingerprint": _digest(plan)}

    def _share_plan(self, args, expected_state):
        self._arguments(args, ("output_key",))
        output_key = self._identity(args.get("output_key"), "output_key")
        examined = {row["output_key"]: row for row in public_outputs(expected_state.get("public"))}
        current = {row["output_key"]: row for row in public_outputs(self.public_state())}
        if output_key not in examined or output_key not in current:
            raise ValueError("That public output is no longer visible; reconsider the current screen.")
        text = current[output_key]["text"]
        if examined[output_key]["text"] != text:
            raise ValueError("The examined public text changed.")
        request_key = "public:" + output_key
        return {
            "id": "share-" + _digest(request_key)[:32], "kind": "chat", "text": text,
            "when": "Share the exact current public output", "choices": [], "duration": 0,
            "category_id": "", "intention_id": "", "poll_mode": "native",
            "request_key": request_key, "request_fingerprint": _digest({"text": text}),
        }

    def _execute_plan(self, plan, source_time):
        with self._reservation:
            self._check()
            current_share = None
            if plan.get("request_key", "").startswith("public:"):
                current_share = lambda: self._share_is_current(plan)
                if not current_share():
                    raise ValueError("That public output is no longer visible; reconsider the current screen.")
            rows = self.production.ledger.snapshot()
            previous = next((r for r in rows if r["plan_id"] == plan["id"]), None)
            if previous:
                if plan.get("request_fingerprint") != previous.get("request_fingerprint"):
                    raise ValueError("This request_id already refers to a different production action.")
                return {"ok": previous["state"] == "done", "simulated": self.offline,
                        "duplicate": True, "receipt": _receipt(previous, self.offline)}
            if len(rows) >= MAX_ACTIONS:
                raise ValueError("This show's bounded production history is full; no action sent.")
            # The ledger reserves before dispatch and never retries uncertain writes.
            adapter = self.production.adapter
            if current_share is not None:
                adapter = _VisibleShareAdapter(adapter, current_share)
            row = self.production.ledger.execute(
                plan, self.production.channel, source_time, adapter,
                active=lambda: bool(self.active() and self.public_enabled() and self._available()),
            )
        if row is None:
            raise ValueError("The selected show stopped before production dispatch.")
        return {"ok": row["state"] == "done", "simulated": self.offline,
                "duplicate": False, "receipt": _receipt(row, self.offline)}

    def _share_is_current(self, plan):
        try:
            public = self.public_state()
        except ValueError:
            # Runtime refuses public-state access after the selected show stops.
            return False
        return any("public:" + row["output_key"] == plan["request_key"]
                   and row["text"] == plan["text"] for row in public_outputs(public))

    def _poll_result(self, args):
        self._arguments(args, ("poll_id",))
        if not self.offline:
            raise ValueError("Model-visible poll results are enabled only for the offline transport.")
        ident = self._identity(args.get("poll_id"), "poll_id")
        created = next((row for row in self.production.ledger.snapshot()
                        if row.get("remote_id") == ident and row["kind"] == "poll"
                        and row["state"] == "done"), None)
        if created is None:
            raise ValueError("Choose a poll created by this show.")
        self._check()
        result = self.production.adapter.poll_result(ident)
        poll = {"id": ident, "status": "active", "counts": [],
                "choices": list(created.get("choices", []))}
        if result is not None:
            if not isinstance(result, dict):
                raise ValueError("The simulated poll returned an invalid result.")
            counts = result.get("counts")
            if (not isinstance(counts, list) or not 2 <= len(counts) <= 5
                    or any(type(count) is not int or not 0 <= count <= 1_000_000 for count in counts)
                    or len(counts) != len(poll["choices"])):
                raise ValueError("The simulated poll returned invalid vote counts.")
            poll.update(status="completed", counts=list(counts))
        return {"ok": True, "simulated": True, "poll": poll}

    def execute(self, args, expected_state):
        """Dispatch after the owner checks selected-show source/revision freshness."""
        self._check()
        if not isinstance(args, dict) or not isinstance(expected_state, dict):
            raise ValueError("Invalid production instruction.")
        action = args.get("action")
        if action not in ACTIONS:
            raise ValueError("Unknown production action.")
        source_time = self.clock()
        if (type(source_time) not in (int, float) or not math.isfinite(source_time)
                or source_time < 0):
            raise ValueError("The selected show clock is unavailable.")
        if action == "get_poll_result":
            return self._poll_result(args)
        if action == "share_on_air":
            plan = self._share_plan(args, expected_state)
        elif action == "execute_plan":
            self._arguments(args, ("plan_id",))
            ident = self._identity(args.get("plan_id"), "plan_id")
            plan = next((copy.deepcopy(item) for item in self.production.plans["actions"]
                         if item["id"] == ident), None)
            if plan is None:
                raise ValueError("Choose an existing reviewed production plan.")
        else:
            plan = self._dynamic_plan(action, args)
        return self._execute_plan(plan, source_time)

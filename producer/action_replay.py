"""Replay saved screen tools through the actual router, with no model/network."""

from contextlib import ExitStack
import copy
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import threading
import tempfile
from unittest.mock import patch

from action_trace import load_trace, normalized_scene, receipts_match, visible_state
from audience import Audience
from director_runtime import CONFIDENCE_TIMING_POLICY, ShowDirector
from on_air import OnAir
import server


class _Core:
    def __init__(self, *_args, **_kwargs):
        self.closed = False

    def snapshot(self):
        return {"closed": self.closed}

    def close(self):
        self.closed = True


class _Editor:
    def __init__(self, *_args, **_kwargs):
        pass

    def request(self, *_args):
        raise AssertionError("Action replay cannot request model advice.")

    def snapshot(self):
        return {"busy": False, "closed": False, "current": None, "results": []}

    def close(self):
        pass


class _Materials:
    def __init__(self, catalog):
        self.catalog, self.contact_sheet = copy.deepcopy(catalog), None

    def get(self, ident):
        # Content was pinned at recording time. Routing only tests membership;
        # rendering independently reads the archived image bytes in the bundle.
        return (b"pinned-replay-asset", "image/jpeg") if any(item["id"] == ident for item in self.catalog) else None


class RoutingReplay:
    """One deterministic local router with the run's source clock and inputs."""

    def __init__(self, manifest, fixture=None):
        self.manifest = manifest
        self.epoch = float(manifest["wall_epoch_s"])
        self.wall = 0.0
        self.running = True
        self.network_attempts = 0
        self.stack = ExitStack()
        self.sid = "local-action-replay"
        self.audience = Audience()
        self.audience.owner = self.sid
        self.audience.connected = True
        self.audience.channel = "simulated-action-replay"
        self.audience.status = "Saved simulated audience"
        fixture_events = (fixture or {}).get("events", [])
        self.fixture_users = {event["id"]: event["user_id"] for event in fixture_events
                              if event.get("type") == "channel.chat.message"}
        self.moderation = [event for event in fixture_events if event.get("type") in (
            "channel.chat.message_delete", "channel.chat.clear_user_messages", "channel.chat.clear")]
        self.moderation_sent = set()
        initial = manifest["initial_state"]
        self.confidence_timing_policy = manifest.get("confidence_timing_policy",
            initial.get("confidence_timing_policy", "legacy"))
        if self.confidence_timing_policy not in ("legacy", CONFIDENCE_TIMING_POLICY):
            raise ValueError("Unknown recorded confidence timing policy.")
        if ("confidence_timing_policy" in initial
                and initial["confidence_timing_policy"] != self.confidence_timing_policy):
            raise ValueError("Conflicting recorded confidence timing policies.")
        context_policy = manifest.get("context_policy", "timed")
        if context_policy not in ("timed", "model-managed"):
            raise ValueError("Unknown recorded topic-context policy.")
        # Old recordings allowed model-written summaries and independent private
        # cue wording. Preserve those outcomes instead of applying today's quote
        # policy to historical actions. New runs pin their policy explicitly.
        self.audience_text_policy = manifest.get("audience_text_policy", "legacy-summary")
        if self.audience_text_policy not in ("legacy-summary", "verbatim"):
            raise ValueError("Unknown recorded audience-text policy.")
        # Earlier routers always supplied fixed non-question labels even when
        # the model requested source credit. Reproduce that historical output.
        self.card_attribution_policy = manifest.get("card_attribution_policy", "legacy-fixed")
        if self.card_attribution_policy not in ("legacy-fixed", "model"):
            raise ValueError("Unknown recorded card-attribution policy.")
        audience_input = manifest.get("audience_input", "legacy-fixture")
        if audience_input not in ("screen", "legacy-fixture"):
            raise ValueError("Unknown recorded audience-input policy.")
        self.production_policy = manifest.get("production_policy")
        if "production_policy" in manifest and self.production_policy != "realtime-production-v1":
            raise ValueError("Unknown recorded production policy.")
        self.production_unavailable = None
        if self.production_policy:
            config = manifest.get("production_replay")
            if not isinstance(config, dict) or config.get("platform") != "fixture":
                self.production_unavailable = "Recorded simulated production transport inputs are missing."
            elif (initial.get("production") or {}).get("receipts"):
                self.production_unavailable = "Replay of an already-started production ledger is not implemented."
            elif not isinstance(config.get("channel_id"), str) or not config["channel_id"]:
                self.production_unavailable = "Recorded simulated production channel identity is missing."
        self.session = {
            "offset": float(initial.get("source_time", 0)), "created": 0,
            "brief": {}, "notes": "", "memory": {},
            "materials": _Materials(manifest.get("asset_catalog", initial.get("assets", []))),
            "on_air": OnAir(initial.get("public_output_enabled", True),
                            context_persistent=context_policy == "model-managed"),
            "visual": None, "offline": True, "test_question": None,
            "audience_text_policy": self.audience_text_policy,
            "card_attribution_policy": self.card_attribution_policy,
            "audience_input": audience_input,
            "confidence_timing_policy": self.confidence_timing_policy,
        }

    def __enter__(self):
        try:
            return self._enter()
        except BaseException:
            self.stack.close()
            raise

    def _enter(self):
        def forbidden(*_args, **_kwargs):
            self.network_attempts += 1
            raise AssertionError("Network/model calls are forbidden during action replay.")
        self.stack.enter_context(patch("time.time", lambda: self.epoch + self.wall))
        self.stack.enter_context(patch("time.time_ns", lambda: int((self.epoch + self.wall) * 1000000000)))
        self.stack.enter_context(patch("time.monotonic", lambda: self.wall))
        self.stack.enter_context(patch("server.audience", self.audience))
        self.stack.enter_context(patch("director_runtime.EditorConsult", _Editor))
        for target in ("requests.sessions.Session.request", "websockets.sync.client.connect",
                       "socket.create_connection", "socket.socket.connect"):
            self.stack.enter_context(patch(target, forbidden))
        if self.production_policy and not self.production_unavailable:
            from production import Ledger, Production
            from rehearsal import MockTwitch
            config = self.manifest["production_replay"]
            temporary = self.stack.enter_context(tempfile.TemporaryDirectory(prefix="producer-action-replay-"))
            brief = copy.deepcopy(config.get("brief") or {})
            self.session.update(production_policy=self.production_policy, brief=brief)
            self.session["production"] = Production(
                brief, Ledger(Path(temporary) / "production.json"),
                MockTwitch(clock=lambda: getattr(self, "director", None).audio_end
                           if hasattr(self, "director") else self.session["offset"],
                           channel_id=config["channel_id"]), channel=config["channel_id"],
            )
        self.director = ShowDirector(
            self.sid, self.session, key="unused-offline-replay", lock=threading.RLock(),
            active=lambda: self.running,
            public_state=lambda: server.public_state(self.sid, self.session),
            audience=self.audience, allow_audience=True, director_class=_Core,
        )
        self.session["director"] = self.director
        initial = self.manifest["initial_state"]
        public = initial.get("public") or {}
        self.session["confidence"] = copy.deepcopy(initial.get("confidence"))
        if self.session["confidence"] and self.confidence_timing_policy == CONFIDENCE_TIMING_POLICY:
            cue = self.session["confidence"]
            shown_at = cue.pop("shown_at_source_s", None)
            cue.pop("visible_for_s", None)
            if shown_at is not None:
                if (type(shown_at) not in (int, float) or not math.isfinite(shown_at)
                        or not 0 <= shown_at <= self.director.audio_end):
                    raise ValueError("Invalid recorded confidence source timestamp.")
                self.session["confidence_shown_at_source_s"] = shown_at
        self.session["director_revision"] = initial.get("revision", 0)
        self.session["on_air"].current = copy.deepcopy(public.get("graphic"))
        self.session["on_air"].context = copy.deepcopy(public.get("context"))
        self.session["visual"] = copy.deepcopy(public.get("visual"))
        self.inputs(initial)
        return self

    def __exit__(self, *_args):
        self.director.close()
        self.stack.close()

    def inputs(self, state):
        """Restore recorded prerequisites only; never restore a target scene."""
        self.director.audio_end = state.get("source_time", self.director.audio_end)
        if "public_output_enabled" in state:
            self.session["on_air"].enabled = state["public_output_enabled"]
        if self.session.get("audience_input") == "screen":
            for frame in state.get("screen_frames", []):
                self.session["screen_chat"].register_frame(frame["frame_id"], frame["source_time"], frame["sha256"])
            return  # Screen observations are reconstructed from tool arguments, never fixture bodies.
        if "audience" in state:
            self.audience.questions = [
                {**copy.deepcopy(q), "user_id": self.fixture_users.get(q["id"], "replay-" + q["id"]),
                 "received": self.epoch + self.wall}
                for q in state["audience"]
            ]
            self.audience.connected = True
        # Inbox expiry alone does not revoke an already pinned question. Replay
        # the archived explicit moderation through the same audience boundary as
        # the live fixture, independently of model calls and target screen state.
        for event in self.moderation:
            if event["at"] > self.director.audio_end or event["id"] in self.moderation_sent:
                continue
            self.moderation_sent.add(event["id"])
            self.audience.accept({
                "metadata": {"message_id": "replay-fixture-" + event["id"], "message_timestamp":
                             datetime.fromtimestamp(self.epoch + self.wall, timezone.utc).isoformat()},
                "payload": {"subscription": {"type": event["type"], "version": "1"}, "event": {
                    "broadcaster_user_id": self.audience.channel,
                    "message_id": event.get("message_id"), "target_user_id": event.get("user_id")}},
            })

    def state(self):
        return visible_state(self.director.state())

    def apply(self, args, expected):
        try:
            return self.director.apply(args, expected)
        except (ValueError, TypeError, KeyError) as exc:
            return {"ok": False, "error": "tool_arguments_or_local_state_rejected",
                    "detail": str(exc)[:240], "state": self.director.state()}

    def production_apply(self, args, expected):
        if not self.production_policy or self.production_unavailable:
            return {"ok": False, "error": "production_replay_unavailable",
                    "detail": self.production_unavailable or "The recording has no production policy."}
        if self.director.closed:
            return {"ok": False, "error": "production_replay_unavailable",
                    "detail": "An in-flight production action finishing after Stop cannot be reconstructed from this journal."}
        try:
            return self.director.production_action(args, expected)
        except (ValueError, TypeError, KeyError) as exc:
            return {"ok": False, "error": "tool_arguments_or_local_state_rejected",
                    "detail": str(exc)[:240], "state": self.director.state()}


def replay_bundle(bundle):
    bundle = Path(bundle)
    manifest = json.loads((bundle / "manifest.json").read_text())
    fixture_path = bundle / "mock-events.json"
    fixture = json.loads(fixture_path.read_text()) if fixture_path.is_file() else None
    events = load_trace(bundle / "actions.jsonl")
    receipt_events = {e["payload"].get("call_id"): e
                      for e in events if e["kind"] == "tool_receipt"}
    commits = {e["payload"].get("revision"): e
               for e in events if e["kind"] == "screen_commit"}
    production_commits = {e["payload"].get("revision"): e
                          for e in events if e["kind"] == "production_commit"}
    scheduled_calls = {}
    for event in events:
        payload = event["payload"]
        if event["kind"] != "function_call" or payload.get("name") not in ("update_screens", "production_action"):
            continue
        receipt_event = receipt_events.get(payload.get("call_id"))
        receipt = (receipt_event or {}).get("payload", {}).get("receipt") or {}
        revision = (receipt.get("state") or {}).get("revision")
        is_production = payload.get("name") == "production_action"
        commit = (production_commits if is_production else commits).get(revision) if is_production or receipt.get("ok") else None
        # Rejected and duplicate actions can keep a prior revision; that prior
        # action's commit is not a new mutation boundary for this invocation.
        if commit and commit["sequence"] < event["sequence"]:
            commit = None
        # New journals record the mutation under the state lock. Old journals
        # only bracket it with invocation/result: use the receipt, never assume
        # invocation already changed the screens. This is an explicit historical
        # timing approximation, not restoration of a recorded target state.
        boundary = commit or receipt_event or event
        if not event["sequence"] <= boundary["sequence"] <= (receipt_event or boundary)["sequence"]:
            raise ValueError("Screen commit is outside its recorded call/receipt boundary.")
        basis = ("production_commit" if is_production else "screen_commit") if commit else "tool_receipt" if receipt_event else "missing_receipt"
        scheduled_calls.setdefault(boundary["sequence"], []).append((event, receipt, basis))
    response_started = {}
    calls, scenes, checks = [], [], []
    with RoutingReplay(manifest, fixture=fixture) as router:
        for event in events:
            router.wall = event["wall_s"]
            kind, payload = event["kind"], event["payload"]
            if kind == "input_frame" and router.session.get("audience_input") == "screen":
                router.session["screen_chat"].register_frame(payload["frame_id"], payload["source_time"], payload["sha256"])
            elif kind == "response_requested":
                response_started[payload["response_sequence"]] = event["wall_s"]
                router.inputs(payload.get("state") or {})
            for call_event, expected, timing_basis in scheduled_calls.get(event["sequence"], []):
                payload = call_event["payload"]
                # These messages/catalogue settings are recorded local INPUTS at
                # this instant. Resulting confidence/public screens are not read.
                router.inputs((expected or {}).get("state") or payload.get("expected_state") or {})
                started = response_started.get(payload.get("response_sequence"), call_event["wall_s"])
                if call_event["wall_s"] - started > 20:
                    actual = {"ok": False, "error": "stale_response", "state": router.state()}
                elif not isinstance(payload.get("arguments"), dict):
                    # Invalid argument prose is deliberately not retained. Its
                    # classified boundary outcome can still be compared; this
                    # is not a byte-for-byte JSON parser reproduction.
                    rejected_json = (payload.get("arguments_error") == "invalid_json_object"
                                     and payload.get("arguments_length", 0) <= 16384)
                    actual = {"ok": False, "error": "tool_arguments_or_local_state_rejected" if rejected_json else "invalid_tool_call"}
                    if rejected_json:
                        actual["state"] = router.state()
                else:
                    callback = router.production_apply if payload.get("name") == "production_action" else router.apply
                    actual = callback(copy.deepcopy(payload["arguments"]), copy.deepcopy(payload.get("expected_state")))
                same = receipts_match(actual, expected, production=payload.get("name") == "production_action")
                calls.append({"call_id": payload.get("call_id"), "sequence": call_event["sequence"],
                              "tool": payload.get("name"),
                              "wall_s": call_event["wall_s"], "source_s": call_event["source_s"],
                              "applied_wall_s": event["wall_s"], "applied_sequence": event["sequence"],
                              "timing_basis": timing_basis,
                              "expected_revision": (payload.get("expected_state") or {}).get("revision"),
                              "matches_recording": bool(same),
                              "expected": expected, "actual": visible_state_receipt(actual)})
            payload = event["payload"]
            if kind == "screen_state":
                expected = payload["state"]
                router.inputs(expected)
                actual = router.state()
                public = actual.get("public") or {}
                scenes.append({"wall_s": event["wall_s"], "source_s": event["source_s"],
                               "confidence": actual.get("confidence"),
                               "graphic": public.get("graphic"), "context": public.get("context"),
                               "visual": public.get("visual"), "revision": actual.get("revision")})
                checks.append({"sequence": event["sequence"], "wall_s": event["wall_s"],
                               "matches_recording": normalized_scene(actual) == normalized_scene(expected)})
            elif kind == "session_closed":
                router.director.close()
                # Same clear on closed core as actual public_state on Stop.
                router.session["on_air"].clear_output()
                router.session["visual"] = router.session["confidence"] = None
        attempts = router.network_attempts
        expected_posts = [event["payload"]["event"] for event in events if event["kind"] == "production_chat"]
        actual_posts = router.session.get("producer_posts", [])
        posts_match = actual_posts == expected_posts
    unvalidated = [call for call in calls if call["actual"].get("error") == "production_replay_unavailable"]
    report = {"schema": "producer.action-replay.v1", "api_calls": attempts,
              "audience_text_policy": router.audience_text_policy,
              "card_attribution_policy": router.card_attribution_policy,
              "input_events": len(events), "screen_actions": sum(c["tool"] == "update_screens" for c in calls),
              "matched_actions": sum(c["matches_recording"] for c in calls),
              "scene_checks": len(checks), "matched_scenes": sum(c["matches_recording"] for c in checks),
              "status": "partial" if unvalidated else "passed" if posts_match and all(c["matches_recording"] for c in calls + checks) else "mismatched",
              "scope": "Saved model screen tools through actual local ShowDirector.apply, OnAir and public_state; no model behavior reevaluation. Original examined revisions, recorded input availability and a frozen clock are retained.",
              "comparison": "Visible confidence/public output, attribution, asset and question identity, type and durations. Generated graphic IDs and absolute expiry timestamps are normalized.",
              "limits": ["Private model working notes, advice, prompts and transcripts are excluded. Malformed argument prose has only a digest/length/classification; its rejection outcome is replayed without reconstructing the unsafe original bytes."],
              "calls": calls, "scene_mismatches": [c for c in checks if not c["matches_recording"]]}
    if router.confidence_timing_policy != "legacy":
        report["confidence_timing_policy"] = router.confidence_timing_policy
        report["comparison"] += " Private cue source timestamps and elapsed visibility are compared exactly."
    if router.production_policy or any(call["tool"] == "production_action" for call in calls):
        report.update(production_policy=router.production_policy,
                      production_actions=sum(c["tool"] == "production_action" for c in calls),
                      unvalidated_production_actions=len(unvalidated),
                      production_chat={"matches_recording": posts_match, "expected": expected_posts, "actual": actual_posts})
        report["scope"] += " Recorded production actions use the real director bridge and isolated simulated Twitch transport, with no live API calls."
        report["comparison"] += " Production payloads, deduplication, identities, outcomes, poll counts and simulated chat events are compared; absolute attempt timestamps are normalized."
        if router.production_unavailable:
            report["limits"].append(router.production_unavailable)
    if any(call["timing_basis"] == "tool_receipt" for call in calls):
        report["limits"].append("Calls without an atomic screen_commit/production_commit marker use the tool receipt clock as an approximation. Historical logs do not identify the exact mutation instant between invocation and receipt; an intervening scene or expiry near that boundary can still mismatch.")
    return report, scenes


def visible_state_receipt(receipt):
    from action_trace import visible_receipt
    return visible_receipt(receipt)


def write_private(path, text):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(text)

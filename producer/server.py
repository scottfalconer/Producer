"""Loopback-only desk. Serves only allowlisted assets, bounded current cuts and the selected proxy."""

import argparse
import base64
import copy
import json
import math
import mimetypes
import os
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from requests import RequestException

from engine import (
    PRIVATE,
    ROOT,
    TRANSCRIBER,
    OpenAIProducer,
    extract,
    fingerprint,
    produce,
    suppress,
    valid_brief,
)
from twitch import channel_status
from continuity import restore_memory, host_update
from preparation import prepare, research
from audience import audience
from production import Production, Ledger, research_context
from platform_twitch import TwitchAdapter
from question_selection import select_questions, question_cue, public_question
from on_air import OnAir, propose_graphic
from captions import CaptionSession
from show_materials import ShowMaterials, validate_research_packet, research_packet_summary
from transcript import ShowTranscript
from simulation_store import show_chat
from recorded_inputs import validate_events
from director_runtime import ShowDirector, note_brief
from realtime_director import MODEL as REALTIME_MODEL
from obs_capture import OBSConfig, OBSFeed
from phone_monitor import start_phone_monitor
from demo_layout import presenter_mirror_snapshot
from action_trace import ActionTrace
import rehearsal
import speech_direction

from credentials import read_openai_key as _read_key

STATIC = Path(__file__).with_name("static")
ASSETS = {
    "/demo-layout": "demo-layout.html",
    "/demo-layout.js": "demo-layout.js",
    "/demo-layout.css": "demo-layout.css",
    "/simulated-chat": "simulated-chat.html",
    "/simulated-chat.js": "simulated-chat.js",
    "/simulated-chat.css": "simulated-chat.css",
    "/visuals/canada.svg": "visuals/canada.svg",
    "/design": "design.html",
    "/design.css": "design.css",
    "/design.js": "design.js",
    "/design-system/tokens.css": "design-system/tokens.css",
    "/design-system/components.css": "design-system/components.css",
    "/design-system/components.js": "design-system/components.js",
    "/design-system/timing.js": "design-system/timing.js",
    "/rehearsal": "rehearsal.html",
    "/rehearsal.js": "rehearsal.js",
    "/rehearsal.css": "rehearsal.css",
    "/": "room.html",
    "/replay": "index.html",
    "/room.js": "room.js",
    "/room-core.js": "room-core.js",
    "/room.css": "room.css",
    "/on-air": "on-air.html",
    "/on-air.js": "on-air.js",
    "/on-air.css": "on-air.css",
    "/captions.js": "captions.js",
    "/caption-worklet.js": "caption-worklet.js",
    "/director.js": "director.js",
    "/director-worklet.js": "director-worklet.js",
    "/desk.js": "desk.js",
    "/style.css": "style.css",
    "/desk-core.js": "desk-core.js",
}
TOKEN = secrets.token_urlsafe(24)
LOCK = threading.Lock()
LIVE = {}
TRANSCRIPTS = {}  # Bounded handles for final End after Pause; contains no transcript text.
TWITCH_LOCK = threading.Lock()
last_twitch = None
PREP_LOCK = threading.Lock()
RESEARCH_LOCK = threading.Lock()
VOICE_QUESTION = {
    "id": "mic-voice-controls-question",
    "user_id": "fictional-mic-viewer",
    "author": "Mic test · fictional viewer",
    "text": "What could a non-coder try tomorrow?",
}
VOICE_TEST_LABEL = "Live microphone · simulated question"
DEFAULT_SHOW_FOLDER = ROOT / "show-materials"
OFFLINE_LABEL = "Offline test · simulated Twitch"


class NativeActionTrace(ActionTrace):
    """Stop closes the journal without waiting for an in-flight Twitch request.

    A late delivery still reaches the durable production ledger, but cannot
    append to this ended capture's journal or break cleanup with a closed file.
    """

    def __init__(self, *args, **kwargs):
        self.lifecycle_lock = threading.RLock()
        super().__init__(*args, **kwargs)

    def emit(self, kind, payload):
        with self.lifecycle_lock:
            if not self.closed:
                return super().emit(kind, payload)

    def close(self):
        with self.lifecycle_lock:
            super().close()


def offline_choice(data, *, active=False):
    value = data.get("offline", True)
    if type(value) is not bool:
        raise ValueError("Choose offline test or live Twitch with a boolean setting.")
    if active:
        with LOCK:
            session = next(iter(LIVE.values()), None)
            if session:
                return value or session.get("offline", True)
    return value


def show_material_snapshot(http, *, load_research=True):
    folder = Path(getattr(http, "show_folder", DEFAULT_SHOW_FOLDER))
    return ShowMaterials(folder, allow_empty=folder == DEFAULT_SHOW_FOLDER, load_research=load_research)


def configured_twitch_adapter(http):
    """Sender identity is selected by the helper host, never by show content."""
    mode = getattr(http, "producer_chat_sender", "separate")
    return TwitchAdapter() if mode == "separate" else TwitchAdapter(sender_mode=mode)


def load_chat_fixture(path):
    """Read a bounded explicitly selected fixture before accepting local requests."""
    try:
        with Path(path).open("rb") as source:
            encoded = source.read(256001)
        if len(encoded) > 256000:
            raise ValueError("Keep the simulated chat fixture within 256 KB.")
        data = json.loads(encoded.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Choose an existing UTF-8 JSON simulated chat fixture.") from exc
    return validate_events(data)


def native_obs_config(http):
    """Native input is selected by the local helper host, never by programme text."""
    return getattr(http, "obs_config", None)


def native_obs_summary(http):
    config = native_obs_config(http)
    return {"configured": config is not None,
            "endpoint": f"tcp://127.0.0.1:{config.port}" if config else None,
            "recording": bool(config and config.recording_dir is not None)}


def show_source_time(session):
    if session.get("input_type") == "obs":
        return max(session.get("native_through", session["offset"]),
                   getattr(session.get("director"), "audio_end", session["offset"]))
    return session["offset"] + time.monotonic() - session["created"]


def source_available(session):
    # A native show belongs to its local capture lifecycle, not browser focus or
    # polling. The capture's own input deadlines still fail a missing source.
    if session.get("input_type") == "obs":
        return not session.get("native_error") and (
            session.get("native_capture") is not None or time.monotonic() - session["seen"] < 120
        )
    return time.monotonic() - session["seen"] < 120


def fail_native_obs(sid, session, message):
    with LOCK:
        if LIVE.get(sid) is not session or session.get("native_error"):
            return
        session["native_error"] = str(message)[:180]
        session["confidence"] = None
        session["visual"] = None
        session["on_air"].clear_output()
        feed, director = session.get("native_capture"), session.get("director")
    # No joins or model/socket cleanup under the global session lock. This may
    # be called by the capture's own callback, including during model startup.
    if feed:
        feed.close()
    if director:
        director.close()
    if session.get("action_trace"):
        session["action_trace"].emit("local_error", {"phase": "native_input", "error": str(message)[:180]})


def native_obs_snapshot(sid, session):
    with LOCK:
        if LIVE.get(sid) is not session:
            raise ValueError("The selected show has stopped.")
        error = session.get("native_error")
        director, feed = session.get("director"), session.get("native_capture")
        connecting = session.get("native_connecting", False)
        result = {"status": "error" if error else "connecting" if connecting else "waiting",
                  "detail": error or ("Connecting Realtime Producer" if connecting else "Waiting for the local OBS recording feed"),
                  "connected": False, "closed": bool(error), "error": error,
                  "model": REALTIME_MODEL, "confidence": None,
                  "memory": copy.deepcopy(session.get("memory")), "through": show_source_time(session)}
    if director and not error:
        result = director.snapshot()
    result["through"] = show_source_time(session)
    result["native_input"] = feed.snapshot() if feed else {"status": "waiting"}
    return result


def start_native_obs(http, sid, session):
    """Start a loopback receiver; no paid director exists until OBS sends A/V."""
    with LOCK:
        if LIVE.get(sid) is not session or session.get("native_capture") or session.get("director_starting"):
            raise ValueError("The selected OBS feed is already starting or stopped.")
        session["director_starting"] = True

    def packet(sequence, audio, frame, source_end):
        with LOCK:
            if LIVE.get(sid) is not session or session.get("native_error"):
                raise ValueError("The selected OBS show has stopped.")
            current = session.get("director")
            if current is None:
                session["native_connecting"] = True
                session["created"] = time.monotonic()
                session["wall_created"] = time.time()
        if current is None:
            candidate = None
            try:
                candidate = ShowDirector(
                    sid, session, key=_read_key(http.key_file), lock=LOCK,
                    active=lambda: LIVE.get(sid) is session and not session.get("native_error"),
                    public_state=lambda: public_state(sid, session), audience=audience,
                    allow_audience=False,
                )
                with LOCK:
                    if LIVE.get(sid) is not session or session.get("native_error"):
                        raise ValueError("The OBS show ended while Realtime was connecting.")
                    session["director"] = candidate
                    session["native_connecting"] = False
                    current = candidate
            except Exception:
                if candidate:
                    candidate.close()
                fail_native_obs(sid, session, "Realtime could not connect to the selected OBS feed. Nothing will restart automatically.")
                raise
        accepted = current.append(sequence, audio, frame, source_end)
        with LOCK:
            if LIVE.get(sid) is session and not session.get("native_error"):
                session["native_through"] = accepted["through"]
                session["seen"] = time.monotonic()

    feed = None
    try:
        trace_directory = getattr(http, "obs_action_trace_dir", None)
        if trace_directory is not None and not session.get("action_trace"):
            directory = Path(trace_directory)
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if directory.is_symlink():
                raise ValueError("OBS action traces require a local directory, not a link.")
            with LOCK:
                if LIVE.get(sid) is not session:
                    raise ValueError("The selected OBS show has stopped.")
                # Each resumed feed gets a separate journal under the same show
                # identity. Never truncate the previous attempt's evidence.
                session["action_trace"] = NativeActionTrace(
                    directory / f"{session['show_id']}-{secrets.token_hex(8)}.jsonl",
                    source_clock=lambda: show_source_time(session),
                    metadata={"model": REALTIME_MODEL, "source_id": session["show_id"],
                              "purpose": "Native OBS action diagnostics"},
                )
                session["owns_action_trace"] = True
        feed = OBSFeed(session["obs_config"], packet,
                       lambda message: fail_native_obs(sid, session, message),
                       offset=session["offset"])
        with LOCK:
            if LIVE.get(sid) is not session:
                raise ValueError("The selected OBS show has stopped.")
            session["native_capture"] = feed
        feed.start()
        return native_obs_snapshot(sid, session)
    except Exception:
        if feed:
            feed.close()
        fail_native_obs(sid, session, "The local OBS input could not start. Check its configured port and retry explicitly.")
        raise
    finally:
        with LOCK:
            session["director_starting"] = False


def selected_visual(session):
    visual = session.get("visual")
    if not visual or visual.get("kind") != "asset":
        return visual  # Retained legacy local-map fixtures are not a request route.
    materials = session.get("materials")
    item = next(
        (i for i in materials.catalog if i["id"] == visual.get("asset_id")), None
    ) if materials else None
    until = visual.get("until")
    if (
        not item or not isinstance(until, (int, float)) or not math.isfinite(until)
        or until <= time.time() or not materials.get(item["id"])
    ):
        return None
    # Only the pinned catalogue chooses the path and label; model URLs are ignored.
    return {
        "kind": "asset", "asset_id": item["id"],
        "src": "/show-assets/" + item["id"],
        "title": item["title"], "alt": item["title"], "until": until,
    }


def record_mic_event(
    session,
    event,
    *,
    command="none",
    question_status="keep",
    resolution_applied=False,
    changed=False,
    reason=None,
):
    """Bounded fixture diagnostics contain state enums only, never speech/content."""
    if session.get("test_question") != "voice-controls":
        return
    graphic = session["on_air"].snapshot().get("graphic") or {}
    has_question = graphic.get("kind") == "question"
    row = {
        "event": event
        if event in ("start", "direction", "question_withdrawn", "audience_stop")
        else "state_change",
        "elapsed_s": round(max(0, time.monotonic() - session["created"]), 3),
        "command": command
        if command in (
            "none", "show_map", "clear_visual", "clear_question", "clear_all",
            "unsupported",
        )
        else "none",
        "question_status": question_status
        if question_status in ("keep", "answered", "dismissed", "skipped")
        else "keep",
        "resolution_applied": bool(resolution_applied),
        "changed": bool(changed),
        "revision": session.get("direction_revision", 0),
        "has_question": has_question,
        "has_visual": bool(session.get("visual")),
    }
    if reason in (
        "semantic_resolution", "clear_all", "audience_disconnected",
        "selection_invalid", "unattributed_state_change",
    ):
        row["reason"] = reason
    session["test_events"] = (session.get("test_events", []) + [row])[-30:]
    session["test_last_question"] = has_question


def seed_voice_question(sid, session):
    """One authored inbox item; this fixture never opens a Twitch connection."""
    audience.stop()
    with audience.lock:
        audience.owner = sid
        audience.channel = "synthetic-mic-test"
        audience.connected = True
        audience.status = "Simulated audience · no Twitch connection"
        audience.questions.append({**VOICE_QUESTION, "received": time.time()})
    session["on_air"].publish(
        {
            "kind": "question",
            "line": VOICE_QUESTION["text"],
            "attribution": VOICE_QUESTION["author"],
            "message_id": VOICE_QUESTION["id"],
        }
    )
    audience.pin_question(sid, VOICE_QUESTION["id"])
    session["shown_questions"].add(VOICE_QUESTION["id"])
    audience.vote = session["production"].vote
    record_mic_event(session, "start", changed=True)


def public_state(sid, session):
    director = session.get("director")
    if director and director.core.snapshot().get("closed"):
        session["on_air"].clear_output()
        session["visual"] = None
        session["confidence"] = None
    testing = session.get("test_question") == "voice-controls"
    before = (
        session["on_air"].snapshot().get("graphic") or {} if testing else {}
    )
    had_question = before.get("kind") == "question"
    previously_present = session.get("test_last_question", had_question)
    selected = None
    inbox = session.get("screen_chat") if session.get("audience_input") == "screen" else audience
    if inbox is not None and inbox.owner == sid:
        try:
            selected = inbox.poll(sid).get("selection")
        except ValueError:
            pass  # A concurrent disconnect withdraws the question, not voice input.
    available = {selected["message_id"]} if selected and selected["valid"] else set()
    session["on_air"].withdraw_question(available)
    with session["on_air"].lock:
        state = session["on_air"].snapshot()
        represented = selected and any(
            item and item.get("kind") == "question"
            and item.get("message_id") == selected["message_id"]
            for item in (session["on_air"].current, session["on_air"].pending)
        )
    if selected and not represented:
        # A model-timed expiry ends display, not the unanswered question's meaning.
        # Keep no moderation identity once it is neither displayed nor queued.
        with inbox.lock:
            if inbox.owner == sid and inbox.selection and inbox.selection["message_id"] == selected["message_id"]:
                inbox.selection = None
    graphic = state.get("graphic") or {}
    if testing:
        has_question = graphic.get("kind") == "question"
        if had_question and not has_question:
            record_mic_event(
                session,
                "question_withdrawn",
                changed=True,
                reason="audience_disconnected"
                if audience.owner != sid or not audience.connected
                else "selection_invalid",
            )
        elif previously_present and not has_question:
            record_mic_event(
                session,
                "question_withdrawn",
                changed=True,
                reason="unattributed_state_change",
            )
        else:
            session["test_last_question"] = has_question
    if graphic.get("kind") == "question" and graphic.get("id") != session.get(
        "voice_question_seen"
    ):
        session["voice_question_seen"] = graphic["id"]
        session["question_voice_baseline"] = session.get("voice_latest_text", "")
    return {**state, "visual": selected_visual(session)}


def offer_speech(sid, session, key_file, speech, window_start=None):
    """Coalesce fresh selected-feed text into one bounded speech-director worker."""
    if not isinstance(speech, str) or not speech.strip():
        return
    if len(speech.split()) < 2:
        return  # A lone wake word or filler is not a completed direction/answer.
    with LOCK:
        if LIVE.get(sid) is not session or not session["on_air"].enabled:
            return
        speech = speech[-6000:]
        session["voice_latest_text"] = speech
        state = public_state(sid, session)
        question = state.get("graphic") or {}
        if question.get("kind") != "question" and not speech_direction.addressed(
            speech
        ):
            return
        baseline = session.get("question_voice_baseline", "")
        question_speech = (
            speech[len(baseline) :]
            if baseline and speech.startswith(baseline)
            else speech
        )
        if window_start is not None and question.get("displayed_at", 0) > window_start:
            question_speech = ""  # An answer from before display cannot resolve it.
        signature = (speech, question.get("id", ""), question_speech)
        if signature == session.get("voice_last_signature"):
            return
        session["voice_pending"] = (
            signature,
            speech,
            question_speech,
            time.monotonic(),
        )
        if session.get("voice_busy"):
            return
        session["voice_busy"] = True
    threading.Thread(
        target=direct_speech, args=(sid, session, key_file), daemon=True
    ).start()


def apply_direction(sid, session, decision):
    """Only local on-air effects; this path has no Twitch/OBS/search executor."""
    public = session["on_air"]
    had_question = (
        (public.snapshot().get("graphic") or {}).get("kind") == "question"
        if session.get("test_question") == "voice-controls"
        else False
    )
    changed = False
    resolution_applied = False
    if decision["question_status"] != "keep":
        changed = public.resolve_question(
            decision["question_id"], decision["question_status"]
        )
        resolution_applied = changed
        if changed and audience.owner == sid:
            audience.unpin_question(sid)
    command = decision["command"]
    if command in ("clear_all", "clear_visual") and decision.get(
        "expected_revision", session.get("direction_revision", 0)
    ) != session.get("direction_revision", 0):
        command = "none"
    if command == "show_map":
        session["visual"] = {
            "id": secrets.token_hex(8),
            "kind": "map",
            "asset_id": "canada",
        }
        changed = True
    elif command == "clear_visual":
        session["visual"] = None
        changed = True
    elif command == "clear_all":
        public.clear_output()
        session["visual"] = None
        if audience.owner == sid:
            audience.unpin_question(sid)
        changed = True
    if changed:
        session["direction_revision"] = session.get("direction_revision", 0) + 1
    if command == "unsupported":
        session["director_note"] = {
            "line": "That visual is not available yet.",
            "until": time.time() + 10,
        }
    elif changed:
        session["director_note"] = None
    record_mic_event(
        session,
        "direction",
        command=command,
        question_status=decision["question_status"],
        resolution_applied=resolution_applied,
        changed=changed,
        reason="semantic_resolution"
        if resolution_applied
        else "clear_all"
        if command == "clear_all" and had_question
        else None,
    )
    return changed


def direct_speech(sid, session, key_file):
    try:
        while True:
            with LOCK:
                if LIVE.get(sid) is not session:
                    return
                pending = session.pop("voice_pending", None)
                if not pending:
                    return
                signature, speech, question_speech, offered = pending
                session["voice_last_signature"] = signature
                state = public_state(sid, session)
                revision = session.get("direction_revision", 0)
            delay = 1.5 - (time.monotonic() - session.get("voice_last_attempt", 0))
            if delay > 0:
                time.sleep(delay)
            if time.monotonic() - offered > 10:
                continue
            session["voice_last_attempt"] = time.monotonic()
            try:
                decision = speech_direction.interpret(
                    OpenAIProducer(_read_key(key_file)), speech, state, question_speech
                )
            except Exception:
                continue  # No retry of the same failed observation or provider fallback.
            with LOCK:
                if LIVE.get(sid) is not session or time.monotonic() - offered > 15:
                    continue
                receipt = (
                    decision["command"],
                    decision["target"],
                    decision["command_quote"].casefold(),
                )
                previous = session.get("voice_receipt")
                if (
                    decision["command"] != "none"
                    and previous
                    and previous[0] == receipt
                    and time.monotonic() - previous[1] < 10
                ):
                    decision["command"] = "none"
                if decision["command"] != "none":
                    session["voice_receipt"] = (receipt, time.monotonic())
                decision["expected_revision"] = revision
                apply_direction(sid, session, decision)
    finally:
        with LOCK:
            session["voice_busy"] = False
            # A newly coalesced input can arrive at the worker's exit boundary.
            if LIVE.get(sid) is session and session.get("voice_pending"):
                session["voice_busy"] = True
                threading.Thread(
                    target=direct_speech, args=(sid, session, key_file), daemon=True
                ).start()


def timing(brief):
    t = 0
    events = []
    for i, beat in enumerate(brief["rundown"]):
        t += beat["minutes"] * 60
        next_name = (
            brief["rundown"][i + 1]["name"] if i + 1 < len(brief["rundown"]) else None
        )
        events.append(
            {
                "id": f"time-{i}",
                "at": t - 30,
                "until": t,
                "kind": "timing",
                "line": f"Finish the thought. Next: {next_name}"
                if next_name
                else "Land the takeaway. Thirty seconds to out.",
                "evidence": f"Planned end of {beat['name']} at {t // 60}:{t % 60:02d}.",
            }
        )
    return events


def events(doc):
    cues = timing(doc["brief"])
    emitted = []
    for i, r in sorted(enumerate(doc["rows"]), key=lambda item: item[1]["available"]):
        if (
            suppress(r["cue"], emitted, r["available"], legacy=True)
            and r["available"] < doc["duration"]
        ):
            emitted.append(r)
            cues.append(
                {
                    "id": f"window-{i}",
                    "at": r["available"],
                    "until": min(r["available"] + 18, doc["duration"]),
                    **r["cue"],
                }
            )
    return sorted((c for c in cues if c["at"] < doc["duration"]), key=lambda c: c["at"])


def review_index(doc):
    candidates = events(doc)
    rank = {"signal": 3, "timing": 2, "editorial": 1}
    shown = []
    for cue in candidates:
        boundaries = sorted(
            {
                cue["at"],
                *(
                    c["until"]
                    for c in candidates
                    if cue["at"] < c["until"] < cue["until"]
                ),
            }
        )
        for t in boundaries:
            eligible = [c for c in candidates if c["at"] <= t < c["until"]]
            winner = max(eligible, key=lambda c: (rank[c["kind"]], c["at"]))
            if winner["id"] == cue["id"]:
                shown.append({**cue, "at": t})
                break
    return sorted(shown, key=lambda c: c["at"])


def as_of(doc, t):
    if not math.isfinite(t) or t < 0:
        raise ValueError("Invalid replay time.")
    stamp = min(t, doc["duration"])
    eligible = [c for c in events(doc) if c["at"] <= stamp]
    rows = [r for r in doc["rows"] if r["available"] <= stamp]
    row = rows[-1] if rows else None
    observation = (
        None
        if row is None
        else {
            **{k: row[k] for k in ("start", "end", "available", "outcome", "meter")},
            "cutoff": row.get("cutoff", row["end"]),
        }
    )
    return {"at": stamp, "eligible": eligible[-12:], "observation": observation}


def load_replay():
    api = PRIVATE / "verified.json"
    if api.exists():
        doc = json.loads(api.read_text())
        if doc["rows"] and doc["rows"][-1]["end"] >= doc["duration"]:
            return doc
    return json.loads((PRIVATE / "signal.json").read_text())


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def end_headers(self):
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def good_host(self):
        return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

    def reply(self, data, status=200):
        encoded = json.dumps(data, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        if not self.good_host():
            self.send_error(403)
            return
        parsed = urlparse(self.path)
        try:
            if parsed.path in ASSETS:
                path = STATIC / ASSETS[parsed.path]
                data = path.read_bytes()
                image_sources = "'self' data:" + (" blob:" if parsed.path in ("/simulated-chat", "/demo-layout") else "")
                self.send_response(200)
                self.send_header(
                    "Content-Type",
                    mimetypes.guess_type(path)[0] or "application/octet-stream",
                )
                self.send_header(
                    "Content-Security-Policy",
                    f"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; media-src 'self' blob:; img-src {image_sources}; frame-ancestors 'none'; base-uri 'none'",
                )
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif parsed.path == "/api/show-materials":
                try:
                    materials = show_material_snapshot(self.server)
                except ValueError as exc:
                    self.reply({"error": str(exc)[:180]}, 400)
                    return
                self.reply({**materials.summary, "notes": materials.notes,
                            "research_packet": materials.research_packet})
            elif parsed.path == "/api/simulated-chat.png":
                from screen_chat_overlay import overlay_png, with_producer_posts
                query = parse_qs(parsed.query)
                with LOCK:
                    session = next(iter(LIVE.values()), None)
                    elapsed = show_source_time(session) if session else 0
                    events = (session.get("simulated_chat") or {"events": [], "duration": 600}) if session else rehearsal.DATA
                    if session and session.get("producer_posts"):
                        events = {**events, "events": with_producer_posts(events, session["producer_posts"])}
                if query.get("preview") == ["1"]:
                    elapsed = float(query.get("at", ["0"])[0])
                    if not math.isfinite(elapsed) or not 0 <= elapsed <= events["duration"]:
                        raise ValueError("Choose a valid simulated-chat preview time.")
                elif session is None:
                    events, elapsed = {"events": [], "duration": 600}, 12
                # The authored audience timeline can end before the show does.
                # Keep the source clock advancing for real Producer deliveries
                # and normal message expiry instead of freezing at its end.
                data = overlay_png(events, elapsed)
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif parsed.path.startswith("/show-assets/"):
                ident = parsed.path[len("/show-assets/"):]
                with LOCK:
                    session = next(iter(LIVE.values()), None)
                    materials = session.get("materials") if session else None
                    asset = materials.get(ident) if materials and source_available(session) else None
                if not asset:
                    self.send_error(404)
                    return
                payload, mime = asset
                if mime != "image/jpeg":
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            elif parsed.path == "/api/room":
                self.reply(
                    {
                        "csrf": TOKEN,
                        "liveReady": self.server.key_file is not None,
                        "producerModel": REALTIME_MODEL,
                        "producerRuntime": "realtime",
                        "audienceInput": "screen",
                        "nativeOBS": native_obs_summary(self.server),
                        "rehearsal": rehearsal.config(),
                        "audienceSelection": bool(
                            getattr(self.server, "audience_openai", False)
                        ),
                    }
                )
            elif parsed.path == "/api/rehearsal":
                with LOCK:
                    session = next(
                        (s for s in LIVE.values() if s.get("rehearsal")), None
                    )
                    elapsed = (
                        min(
                            rehearsal.DATA["duration"],
                            session["offset"] + time.monotonic() - session["created"],
                        )
                        if session
                        else 0
                    )
                    if session and session.get("events"):
                        session["events"].advance(elapsed)
                    self.reply(
                        {
                            "running": bool(session),
                            "elapsed": elapsed,
                            "duration": rehearsal.DATA["duration"],
                            "synthetic": True,
                        }
                    )
            elif parsed.path == "/api/demo-layout":
                # Programme layout is a public projection. Presenter cues,
                # notes, research and transcript memory never belong here.
                with LOCK:
                    selected = next(iter(LIVE.items()), None)
                    result = {"active": False, "elapsed": 0, "simulated_chat": False, "public": {"graphic": None}}
                    if selected:
                        sid, session = selected
                        director = session.get("director")
                        core = director.core.snapshot() if director else {}
                        if source_available(session) and core.get("connected") and not core.get("closed") and not core.get("error"):
                            result = {"active": True, "elapsed": show_source_time(session),
                                      "simulated_chat": bool(session.get("simulated_chat")),
                                      "public": public_state(sid, session)}
                self.reply(result)
            elif parsed.path == "/api/on-air":
                with LOCK:
                    session = next(iter(LIVE.values()), None)
                    result = {"graphic": None}
                    if session and source_available(session):
                        result = public_state(
                            next(k for k, v in LIVE.items() if v is session), session
                        )
                        result["rehearsal"] = bool(session.get("rehearsal"))
                        result["offline"] = session.get("offline", True)
                        result["test_label"] = session.get("test_label")
                        result["timing"] = {
                            "window": session.get("window_timing"),
                            "public": session.get("public_timing"),
                        }
                        if session.get("test_question") == "voice-controls":
                            result["test_events"] = list(session.get("test_events", []))
                        result["caption"] = session["director"].caption_snapshot() if (
                            session.get("director") and session.get("captions_enabled")
                        ) else (
                            session["captions"].snapshot()
                            if session.get("captions")
                            and session.get("captions_enabled")
                            else None
                        )
                        note = session.get("director_note")
                        result["director_note"] = (
                            note if note and note["until"] > time.time() else None
                        )
                self.reply(result)
            elif parsed.path == "/api/desk":
                doc = load_replay()
                self.reply(
                    {
                        "csrf": TOKEN,
                        "duration": doc["duration"],
                        "brief": doc["brief"],
                        "models": doc["models"],
                        "windows": len(doc["rows"]),
                        "liveReady": self.server.key_file is not None,
                    }
                )
            elif parsed.path == "/api/review-index":
                self.reply(review_index(load_replay()))
            elif parsed.path == "/api/replay":
                self.reply(as_of(load_replay(), float(parse_qs(parsed.query)["at"][0])))
            elif parsed.path == "/media":
                self.media()
            elif parsed.path == "/rehearsal-media":
                self.media(rehearsal.CLIP)
            else:
                self.send_error(404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (OSError, ValueError, KeyError):
            self.reply({"error": "Replay source or time unavailable."}, 400)

    def media(self, path=None):
        path = path or PRIVATE / "review.mp4"
        size = path.stat().st_size
        start = 0
        end = size - 1
        status = 200
        hdr = self.headers.get("Range")
        if hdr:
            match = re.fullmatch(r"bytes=(\d+)-(\d*)", hdr)
            if not match or int(match[1]) >= size:
                self.send_error(416)
                return
            start = int(match[1])
            end = min(size - 1, int(match[2]) if match[2] else size - 1)
            if end < start:
                self.send_error(416)
                return
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        left = end - start + 1
        self.send_header("Content-Length", str(left))
        self.end_headers()
        with path.open("rb") as f:
            f.seek(start)
            while left:
                block = f.read(min(left, 131072))
                if not block:
                    break
                self.wfile.write(block)
                left -= len(block)

    def do_POST(self):
        origin = f"http://127.0.0.1:{self.server.server_port}"
        if (
            not self.good_host()
            or self.headers.get("Origin") != origin
            or not secrets.compare_digest(self.headers.get("X-Desk", ""), TOKEN)
        ):
            self.send_error(403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 3000000:
                raise ValueError("Invalid request size.")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("Invalid request.")
            if self.path in ("/api/prepare", "/api/research", "/api/voice"):
                if self.server.key_file is None:
                    raise ValueError("OpenAI is not configured on this helper.")
                gate = RESEARCH_LOCK if self.path == "/api/research" else PREP_LOCK
                if not gate.acquire(blocking=False):
                    raise ValueError("That producer task is still running.")
                try:
                    if self.path == "/api/prepare" and data.get("runtime") == "realtime":
                        self.reply({"brief": valid_brief(note_brief(data.get("notes"))),
                                    "readback": "", "question": ""})
                        return
                    api = OpenAIProducer(_read_key(self.server.key_file))
                    if self.path == "/api/prepare":
                        result = prepare(api, data)
                    elif self.path == "/api/research":
                        result = research(api, data)
                    else:
                        encoded = base64.b64decode(data["audio"], validate=True)
                        pcm = extract(
                            [
                                "-f",
                                "matroska",
                                "-i",
                                "pipe:0",
                                "-t",
                                "60",
                                "-vn",
                                "-ac",
                                "1",
                                "-ar",
                                "16000",
                                "-f",
                                "s16le",
                                "pipe:1",
                            ],
                            encoded,
                        )
                        result = {"text": api.transcribe(pcm)[:4000]}
                finally:
                    gate.release()
            elif self.path == "/api/twitch":
                global last_twitch
                if offline_choice(data, active=True):
                    result = {
                        "offline": True, "simulated": True, "platform": "fixture",
                        "channel": "simulated", "title": OFFLINE_LABEL,
                        "status": OFFLINE_LABEL,
                        "category": "Simulated programme", "online": False,
                        "viewers": None, "checked": int(time.time()),
                    }
                else:
                    with TWITCH_LOCK:
                        if (
                            last_twitch is None
                            or time.time() - last_twitch["checked"] >= 30
                        ):
                            last_twitch = channel_status()
                        result = last_twitch
            elif self.path == "/api/production/check":
                brief = valid_brief(data["brief"])
                offline = offline_choice(data, active=True)
                adapter = rehearsal.MockTwitch() if offline else configured_twitch_adapter(self.server)
                caps, issues = adapter.check_plans(
                    brief.get("production", {}).get("actions", [])
                )
                result = {"connection": caps, "issues": issues}
            elif self.path.startswith("/api/captions/"):
                with LOCK:
                    session = LIVE.get(data.get("id"))
                    if session and session.get("runtime") == "realtime":
                        raise ValueError("Realtime captions use the existing selected-source audio session.")
                    if not session or not session["on_air"].enabled:
                        raise ValueError(
                            "Start an on-air show before connecting speech controls."
                        )
                if self.path == "/api/captions/start":
                    with LOCK:
                        if session.get("captions") or session.get("captions_starting"):
                            raise ValueError("Captions already started for this show.")
                        session["captions_starting"] = True
                    try:
                        caption = CaptionSession(
                            _read_key(self.server.key_file),
                            on_text=None,
                        )
                        with LOCK:
                            if LIVE.get(data["id"]) is not session:
                                caption.close()
                                raise ValueError("Show ended during caption setup.")
                            session["captions"] = caption
                    finally:
                        with LOCK:
                            session["captions_starting"] = False
                    result = {"started": True}
                elif self.path == "/api/captions/audio" and session.get("captions"):
                    audio = data.get("audio")
                    if session.get("rehearsal"):
                        raw = session["rehearsal"].caption_audio(
                            data.get("sequence"),
                            session["offset"],
                            session["offset"] + time.monotonic() - session["created"],
                        )
                        audio = base64.b64encode(raw).decode()
                    session["captions"].append(data.get("sequence"), audio)
                    result = {"accepted": True}
                else:
                    raise ValueError("Unknown caption action.")
            elif self.path == "/api/production/status":
                with LOCK:
                    session = LIVE.get(data.get("id"))
                    if not session:
                        raise ValueError("That show is no longer running.")
                    result = {
                        "activity": session["production"].activity,
                        "actions": session["production"].ledger.snapshot(),
                    }
            elif self.path == "/api/production/research":
                with LOCK:
                    session = LIVE.get(data.get("id"))
                    if not session:
                        raise ValueError("That show is no longer running.")
                    session["production"].shelf = research_context(
                        data.get("research", [])
                    )
                    result = {"updated": True}
            elif self.path.startswith("/api/audience/"):
                action = self.path.rsplit("/", 1)[-1]
                if action == "stop":
                    with LOCK:
                        stopped_session = LIVE.get(audience.owner)
                    audience.stop()
                    with LOCK:
                        if stopped_session and any(
                            s is stopped_session for s in LIVE.values()
                        ):
                            record_mic_event(
                                stopped_session,
                                "audience_stop",
                                reason="audience_disconnected",
                            )
                    result = {"stopped": True}
                else:
                    with LOCK:
                        if data.get("id") not in LIVE:
                            raise ValueError(
                                "Start the show before reading audience questions."
                            )
                        if LIVE[data["id"]].get("audience_input") == "screen":
                            raise ValueError("Producer reads chat from the selected programme images; no chat API is used.")
                    if action == "start":
                        session = LIVE[data["id"]]
                        if session.get("offline") or session.get("test_question"):
                            if audience.owner != data["id"] and session.get("events"):
                                session["events"].connect()
                            result = {"status": audience.status}
                        else:
                            result = audience.start(data["id"])
                        audience.vote = LIVE[data["id"]]["production"].vote
                    elif action == "poll":
                        session = LIVE.get(data["id"])
                        if session and session.get("events"):
                            session["events"].advance(
                                session["offset"]
                                + time.monotonic()
                                - session["created"]
                            )
                        result = audience.poll(data["id"])
                        with LOCK:
                            session = LIVE.get(data["id"])
                            if session:
                                state = public_state(data["id"], session)
                                graphic = state.get("graphic") or {}
                                result["active_question"] = (
                                    graphic
                                    if graphic.get("kind") == "question"
                                    else None
                                )
                                current = session["production"].activity or {}
                                result["decisions"] = session["question_decisions"]
                                result["cue"] = None
                                serial = session.get("question_decision_serial", 0)
                                if session.get("question_context_at") == current.get("at") and serial != session.get("question_delivered_serial", 0):
                                    available = {
                                        q["id"]: q for q in result["questions"]
                                    }
                                    for d in session["question_decisions"]:
                                        ident = d["message_id"]
                                        if (
                                            d["decision"] == "show_now"
                                            and ident in available
                                            and (d.get("surface") == "backstage" or not session["on_air"].enabled)
                                        ):
                                            session["shown_questions"].add(ident)
                                            result["cue"] = question_cue(
                                                d, available[ident]
                                            )
                                            result["cue"]["id"] += "-" + str(serial)
                                            session["question_delivered_serial"] = serial
                                            break
                    elif action == "dismiss":
                        audience.dismiss(data["id"], data["message_id"])
                        result = {"dismissed": True}
                    else:
                        raise ValueError("Unknown audience action.")
            elif self.path == "/api/start":
                if self.server.key_file is None:
                    raise ValueError("Set --key-file to use live OpenAI processing.")
                brief = valid_brief(data["brief"])
                runtime = data.get("runtime", "legacy")
                if runtime not in ("legacy", "realtime"):
                    raise ValueError("Unknown producer runtime.")
                notes = data.get("notes", "")
                if not isinstance(notes, str) or len(notes) > 20000:
                    raise ValueError("Keep the show notes within 20,000 characters.")
                offline = offline_choice(data)
                with LOCK:
                    for old_id, old in list(LIVE.items()):
                        old_director = old.get("director")
                        old_core = getattr(old_director, "core", None)
                        disconnected = bool(old_director and (
                            getattr(old_director, "closed", False)
                            or (old_core and old_core.snapshot().get("closed"))
                        ))
                        if old.get("input_type") == "obs" and old.get("native_capture") is not None:
                            # Native source failure closes resources itself and
                            # remains visible until explicit Pause/End. Never
                            # replace a receiving show because its screen slept.
                            continue
                        if disconnected or time.monotonic() - old["seen"] > 120:
                            LIVE.pop(old_id)
                            old["on_air"].close()
                            if old.get("captions"):
                                old["captions"].close()
                            try:
                                if old.get("transcript"):
                                    old["transcript"].stop(
                                        show_source_time(old), "source_lost"
                                    )
                            finally:
                                if old.get("director"):
                                    old["director"].close()
                                if audience.owner == old_id:
                                    audience.stop()
                    if LIVE:
                        raise ValueError("End the existing live session first.")
                materials = show_material_snapshot(self.server, load_research="research_packet" not in data)
                research_packet = validate_research_packet(
                    data["research_packet"] if "research_packet" in data else materials.research_packet
                )
                sid = secrets.token_urlsafe(20)
                demo = data.get("rehearsal")
                if demo not in (None, "office-hours"):
                    raise ValueError("Unknown local rehearsal.")
                if demo and not offline:
                    raise ValueError("The recording rehearsal uses offline test mode.")
                input_type = data.get("input_type", "selected-feed")
                if input_type not in (
                    "selected-feed", "camera", "tab", "rehearsal", "microphone", "obs"
                ):
                    raise ValueError("Unknown live input type.")
                if input_type == "rehearsal" and not offline:
                    raise ValueError("The recording rehearsal uses offline test mode.")
                obs_config = native_obs_config(self.server) if input_type == "obs" else None
                if input_type == "obs" and (obs_config is None or runtime != "realtime" or demo):
                    raise ValueError("Local OBS input requires configured native capture and Realtime, without a recording rehearsal.")
                test_question = data.get("test_question")
                if test_question not in (None, "voice-controls"):
                    raise ValueError("Unknown microphone test question.")
                audience_input = data.get("audience_input", "screen" if runtime == "realtime" and not test_question else "legacy-fixture")
                if audience_input not in ("screen", "legacy-fixture") or (audience_input == "legacy-fixture" and runtime == "realtime" and not offline):
                    raise ValueError("Realtime chat is read from the selected screen. Legacy fixture input is offline-only.")
                if input_type == "obs" and audience_input != "screen":
                    raise ValueError("OBS comments must be read from the selected programme picture.")
                if test_question and (
                    input_type != "microphone" or data.get("on_air") is not True or demo
                ):
                    raise ValueError(
                        "The voice-controls question requires live microphone input and on-air mode."
                    )
                if demo and input_type == "microphone":
                    raise ValueError("A recording rehearsal cannot use the microphone.")
                if demo and not rehearsal.CLIP.exists():
                    raise ValueError("Local rehearsal media is missing.")
                show_id = data.get("show_id") or secrets.token_hex(16)
                if not isinstance(show_id, str) or not re.fullmatch(
                    r"[a-zA-Z0-9-]{16,60}", show_id
                ):
                    raise ValueError("Invalid show identity.")
                plans = brief.get("production", {}).get("actions", [])
                adapter, channel, issues = None, "", []
                live_production_enabled = (
                    runtime == "realtime" and not offline
                    and getattr(self.server, "enable_live_production", False) is True
                )
                if offline or test_question:
                    adapter = rehearsal.MockTwitch()
                    channel = "synthetic-mic-test" if test_question else "synthetic-office-hours"
                elif live_production_enabled or (runtime != "realtime" and plans):
                    try:
                        adapter = configured_twitch_adapter(self.server)
                        caps, issues = adapter.check_plans(plans)
                        channel = caps["channel_id"]
                        if not isinstance(channel, str) or not channel:
                            raise ValueError("Twitch did not confirm a broadcaster identity.")
                        if live_production_enabled and issues:
                            adapter, live_production_enabled = None, False
                    except (ValueError, OSError, KeyError, TypeError, RequestException):
                        adapter = None
                        channel, live_production_enabled = "", False
                        issues = [
                            "Twitch production is unavailable. The private monitor can still run."
                        ]
                production = Production(
                    brief,
                    Ledger(
                        (PRIVATE / "actions" / "offline" if offline else PRIVATE / "actions")
                        / (show_id + ".json")
                    ),
                    adapter,
                    channel,
                    data.get("research", []),
                )
                if adapter and not (offline or test_question):
                    adapter.chat_ready = lambda: (
                        audience.owner == sid and audience.connected
                    )
                with LOCK:
                    if LIVE:
                        raise ValueError("End the existing live session first.")
                    offset = float(data.get("offset", 0))
                    if not math.isfinite(offset) or not 0 <= offset <= 86400:
                        raise ValueError("Invalid clock offset.")
                    transcript = ShowTranscript(
                        PRIVATE / "transcripts", show_id,
                        {"input_type": input_type, "rehearsal": demo, "offline": offline, "transcriber": TRANSCRIBER},
                        fingerprint({"brief": brief, "notes": notes,
                                     **({"native_input": {"endpoint": f"tcp://127.0.0.1:{obs_config.port}"}} if obs_config else {}),
                                     **({"audience_input": "screen"} if audience_input == "screen" else {}),
                                     **({"research_packet": research_packet} if research_packet else {})})
                        if runtime == "realtime" else fingerprint(brief), offset,
                    )
                    TRANSCRIPTS[show_id] = transcript
                    while len(TRANSCRIPTS) > 32:
                        TRANSCRIPTS.pop(next(iter(TRANSCRIPTS)))
                    simulated_chat = show_chat(
                        PRIVATE / "simulated-chat", show_id, notes=notes,
                        duration=rehearsal.DATA["duration"] if demo else 600,
                        override=getattr(self.server, "chat_fixture_override", None),
                    ) if audience_input == "screen" and (
                        offline or getattr(self.server, "simulate_chat", False) is True
                    ) else None
                    LIVE[sid] = {
                        "show_id": show_id,
                        "runtime": runtime,
                        "production_policy": "realtime-production-v1" if runtime == "realtime" else None,
                        "live_production_enabled": live_production_enabled,
                        "producer_posts": [],
                        "audience_input": audience_input,
                        "simulated_chat": simulated_chat,
                        "notes": notes,
                        "research_packet": research_packet,
                        "director": None,
                        "transcript": transcript,
                        "offset": offset,
                        "input_type": input_type,
                        "obs_config": obs_config,
                        "native_through": offset,
                        "offline": offline,
                        "test_question": test_question,
                        "test_label": VOICE_TEST_LABEL if test_question else (
                            OFFLINE_LABEL if offline and not demo else None
                        ),
                        "brief": brief,
                        "materials": materials,
                        "audience_openai": bool(getattr(self.server, "audience_openai", False)),
                        "history": [],
                        "last": offset,
                        "busy": False,
                        "created": time.monotonic(),
                        "wall_created": time.time(),
                        "seen": time.monotonic(),
                        "memory": restore_memory(brief, data.get("memory"))
                        if "intentions" in brief
                        else None,
                        "production": production,
                        "question_decisions": [],
                        "question_busy": False,
                        "shown_questions": set(),
                        "shown_polls": set(),
                        "on_air": OnAir(data.get("on_air") is True, context_persistent=runtime == "realtime"),
                        "captions_enabled": data.get("captions") is True,
                        "captions": None,
                        "visual": None,
                        "direction_revision": 0,
                        "voice_latest_text": "",
                        "voice_busy": False,
                        "rehearsal": rehearsal.Rehearsal(audience, sid, offset,
                                                       chat_events=simulated_chat if audience_input == "screen" else None)
                        if demo
                        else None,
                        "events": rehearsal.EventFixture(audience, sid, offset)
                        if offline and not test_question and audience_input != "screen" else None,
                    }
                    if test_question:
                        seed_voice_question(sid, LIVE[sid])
                    elif LIVE[sid]["events"]:
                        LIVE[sid]["events"].connect()
                        audience.vote = production.vote
                result = {
                    "id": sid,
                    "show_id": show_id,
                    "memory": LIVE[sid]["memory"],
                    "production_issues": issues,
                    "live_production_enabled": live_production_enabled,
                    "audience_required": audience_input != "screen" and (bool(plans) or offline or bool(test_question)),
                    "audience_input": audience_input,
                    "offline": offline,
                    "test_label": LIVE[sid]["test_label"],
                    "show_materials": materials.summary,
                    "research_packet": research_packet_summary(research_packet),
                    "transcript": transcript.summary(),
                }
            elif self.path == "/api/intention":
                with LOCK:
                    session = LIVE.get(data["id"])
                    if not session or session["memory"] is None:
                        raise ValueError(
                            "Start the show before updating its live memory."
                        )
                    session["memory"] = host_update(
                        session["memory"], data["intention_id"], data["status"]
                    )
                    result = {"memory": session["memory"]}
            elif self.path == "/api/stop":
                result = stop_show(data)
            elif self.path.startswith("/api/director/"):
                result = self.director(data)
            elif self.path == "/api/window":
                result = self.live(data)
            else:
                self.send_error(404)
                return
            self.reply(result)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (ValueError, RuntimeError) as exc:
            self.reply({"error": str(exc)[:180]}, 400)
        except Exception:
            self.reply(
                {
                    "error": "Source or provider unavailable. Check the local connection."
                },
                503,
            )

    def director(self, data):
        sid = data.get("id")
        with LOCK:
            session = LIVE.get(sid)
            if not session or session.get("runtime") != "realtime":
                raise ValueError("Start the selected Realtime show first.")
            current = session.get("director")
            session["seen"] = time.monotonic()
        if self.path == "/api/director/start":
            if session.get("input_type") == "obs":
                return start_native_obs(self.server, sid, session)
            with LOCK:
                if current or session.get("director_starting"):
                    raise ValueError("Realtime Producer is already starting or connected.")
                session["director_starting"] = True
            candidate = None
            try:
                candidate = ShowDirector(
                    sid, session, key=_read_key(self.server.key_file), lock=LOCK,
                    active=lambda: LIVE.get(sid) is session,
                    public_state=lambda: public_state(sid, session), audience=audience,
                    allow_audience=session.get("audience_input") != "screen" and bool(session.get("offline") or session.get("audience_openai")),
                )
                with LOCK:
                    if LIVE.get(sid) is not session:
                        raise ValueError("The show ended while Realtime was connecting.")
                    session["director"] = candidate
                    session["created"] = time.monotonic()
                    session["wall_created"] = time.time()
                return candidate.snapshot()
            except Exception:
                if candidate:
                    candidate.close()
                raise
            finally:
                with LOCK:
                    session["director_starting"] = False
        if session.get("input_type") == "obs":
            if self.path == "/api/director/audio":
                raise ValueError("OBS media comes only from the configured local feed, not the browser.")
            if self.path == "/api/director/status":
                return native_obs_snapshot(sid, session)
        if not current:
            raise ValueError("Realtime Producer is not connected.")
        if self.path == "/api/director/audio":
            return current.append(data.get("sequence"), data.get("audio"), data.get("frame"), data.get("source_end"))
        if self.path == "/api/director/status":
            return current.snapshot()
        raise ValueError("Unknown Realtime route.")

    def live(self, data):
        received_at = time.monotonic()
        sid = data["id"]
        start = float(data["start"])
        end = float(data["end"])
        if not 0 <= start < end or end - start > 45:
            raise ValueError("Invalid live cut.")
        with LOCK:
            session = LIVE.get(sid)
            if session and session.get("runtime") == "realtime":
                raise ValueError("This show uses continuous Realtime input, not observation windows.")
            if (
                not session
                or session["busy"]
                or start < session["last"]
                or end > session["offset"] + time.monotonic() - session["created"] + 3
            ):
                raise ValueError("Window out of order or session stopped.")
            session["busy"] = True
            session["seen"] = time.monotonic()
            import copy

            memory_at_start = copy.deepcopy(session["memory"])
            arrival_lag = max(0, session["offset"] + received_at - session["created"] - end)
        transcript_attempted = transcript_saved = False
        try:
            decode_started = time.monotonic()
            if session.get("rehearsal"):
                pcm, frame = session["rehearsal"].cut(start, end)
            else:
                encoded = base64.b64decode(data["audio"], validate=True)
                frame = base64.b64decode(data.get("frame", ""), validate=True)
            if len(frame) > 350000 or (
                not frame.startswith(b"\xff\xd8")
                and not (not frame and session.get("input_type") == "microphone")
            ):
                raise ValueError("Invalid sampled frame.")
            if not session.get("rehearsal"):
                pcm = extract(
                    [
                        "-f",
                        "matroska",
                        "-i",
                        "pipe:0",
                        "-t",
                        str(end - start),
                        "-vn",
                        "-ac",
                        "1",
                        "-ar",
                        "16000",
                        "-f",
                        "s16le",
                        "pipe:1",
                    ],
                    encoded,
                )
            if session.get("events"):
                session["events"].advance(end)
            decode_s = time.monotonic() - decode_started
            api = OpenAIProducer(_read_key(self.server.key_file))

            def active():
                with LOCK:
                    return LIVE.get(sid) is session

            def speech_ready(speech):
                nonlocal transcript_attempted, transcript_saved
                with LOCK:
                    if LIVE.get(sid) is not session:
                        raise ValueError("Session ended before transcription was accepted.")
                    transcript_attempted = True
                    session["transcript"].record(start, end, speech)
                    transcript_saved = True
                earlier = session["transcript"].context(
                    start, speech, memory_at_start, session["brief"]["goal"]
                )
                # This worker receives speech/public assets only. It neither
                # depends on nor mutates the private continuity result below.
                dispatch_public(sid, session, self.server.key_file, speech, start, end)
                return earlier

            row = produce(
                session["brief"],
                start,
                end,
                pcm,
                frame,
                session["history"],
                api,
                active,
                memory_at_start,
                session["production"].context(),
                speech_ready,
            )
            with LOCK:
                if LIVE.get(sid) is not session:
                    raise ValueError("Session ended during processing.")
                if not transcript_saved:
                    transcript_attempted = True
                    level = row.get("meter") or {}
                    status = "transcribed" if row.get("speech") else (
                        "signal_skipped" if row.get("outcome") == "measured" else
                        "quiet_not_transcribed" if level.get("dbfs", 0) is not None and level.get("dbfs", 0) < -60 else "no_text"
                    )
                    session["transcript"].record(start, end, row.get("speech", ""), status)
                    transcript_saved = True
            fresh = (
                session["offset"] + time.monotonic() - session["created"] - end <= 20
            )
            if active():
                production = session["production"]
                produced = production.consume(
                    row.pop("production_answer", {}), row["speech"], end, active, fresh
                )
                row.update(activity=produced["activity"], actions=produced["actions"])
                if not row.get("cue") and produced["research_cue"]:
                    row["cue"] = suppress(
                        produced["research_cue"], session["history"], row["available"]
                    )
                if not row.get("cue") and fresh:
                    poll_cue = production.poll_cue(end)
                    if poll_cue:
                        row["cue"] = poll_cue
                for outcome in produced["actions"]:
                    plan = next(
                        p
                        for p in production.plans["actions"]
                        if p["id"] == outcome["plan_id"]
                    )
                    if outcome["state"] == "done" and plan["kind"] == "reference":
                        session["on_air"].publish(
                            {
                                "kind": "reference",
                                "line": plan["text"],
                                "attribution": "Reference · Producer",
                            }
                        )
                    if (
                        outcome["state"] == "done"
                        and plan["kind"] == "reference"
                        and row.get("memory")
                    ):
                        for item in row["memory"]["items"]:
                            if (
                                item["id"] == plan["intention_id"]
                                and item["kind"] == "share"
                                and not item["locked"]
                            ):
                                item.update(
                                    status="covered",
                                    evidence="Simulated reference delivery in the offline test."
                                    if session.get("offline")
                                    else "Twitch confirmed the approved reference was delivered.",
                                    at=end,
                                )
                # Audience interpretation is separate, text-only, explicitly enabled on the
                # helper. It cannot execute actions or feed fetched chat into media inference.
                if (
                    (
                        getattr(self.server, "audience_openai", False)
                        or session.get("offline")
                    )
                    and audience.owner == sid
                    and not session["question_busy"]
                ):
                    session["question_busy"] = True
                    threading.Thread(
                        target=judge_audience,
                        args=(sid, session, self.server.key_file),
                        daemon=True,
                    ).start()
                # A context-only transition can wait for the boundary judgment.
                # The source-end latch also prevents dispatching the early worker twice.
                dispatch_public(
                    sid, session, self.server.key_file, row.get("speech", ""), start, end,
                    boundary=bool(row.get("boundary")),
                )
            with LOCK:
                if LIVE.get(sid) is not session:
                    raise ValueError("Session ended during processing.")
                session["history"] = (session["history"] + [row])[-12:]
                session["last"] = end
                if row.get("memory") is not None:
                    # Preserve presenter corrections made while the model was busy.
                    for old in session["memory"]["items"]:
                        before = next(
                            (
                                i
                                for i in memory_at_start["items"]
                                if i["id"] == old["id"]
                            ),
                            None,
                        )
                        if old != before:
                            row["memory"]["items"] = [
                                old if i["id"] == old["id"] else i
                                for i in row["memory"]["items"]
                            ]
                    session["memory"] = row["memory"]
                row["processing_timing_s"] = {
                    **row.get("processing_timing_s", {}),
                    "arrival_lag_s": round(arrival_lag, 3),
                    "decode_s": round(decode_s, 3),
                    "server_total_s": round(time.monotonic() - received_at, 3),
                    "source_to_private_s": round(
                        max(0, session["offset"] + time.monotonic() - session["created"] - end), 3
                    ),
                }
                session["window_timing"] = {
                    "source_end": end, **row["processing_timing_s"]
                }
                row["transcript"] = session["transcript"].summary()
            return {
                k: v for k, v in row.items() if k not in ("speech", "production_answer")
            }
        except Exception:
            with LOCK:
                if LIVE.get(sid) is session:
                    if not transcript_attempted:
                        session["transcript"].record(start, end, status="failed")
                    elif transcript_saved:
                        session["transcript"].processing_failed(start, end)
            raise
        finally:
            with LOCK:
                session["busy"] = False


def stop_show(data):
    """Release all live resources even if durable transcript finalization fails."""
    sid = data.get("id")
    ended = None
    try:
        with LOCK:
            session = LIVE.get(sid)
            reason = data.get("reason", "ended")
            if reason not in ("paused", "ended"):
                raise ValueError("Unknown show stop reason.")
            through = data.get("through")
            if session and session.get("input_type") == "obs":
                # The browser is a screen/control, not this source's clock.
                through = show_source_time(session)
            elif through is None and session:
                through = show_source_time(session)
            if through is not None and (isinstance(through, bool) or not isinstance(through, (int, float))
                or not math.isfinite(through) or not 0 <= through <= 86400):
                raise ValueError("Invalid show stop time.")
            if sid is None:
                show_id = data.get("show_id")
                transcript = TRANSCRIPTS.get(show_id)
                if not transcript or not transcript.closed or reason != "ended" or any(
                    s.get("show_id") == show_id for s in LIVE.values()
                ):
                    raise ValueError("Only a known paused, inactive show can be ended here.")
                transcript.stop(through if through is not None else transcript.summary()["through"], "ended")
                return {"stopped": True, "memory": None, "actions": [], "transcript": transcript.summary()}
            ended = LIVE.pop(sid, None)
            if ended:
                ended["on_air"].close()
                if ended.get("captions"):
                    ended["captions"].close()
                ended["transcript"].stop(through, reason)
            return {
                "stopped": True,
                "memory": ended.get("memory") if ended else None,
                "actions": ended["production"].ledger.snapshot() if ended else [],
                "transcript": ended["transcript"].summary() if ended else None,
            }
    finally:
        try:
            if ended and ended.get("native_capture"):
                ended["native_capture"].close()
            if ended and ended.get("director"):
                ended["director"].close()
            if ended and audience.owner == sid:
                audience.stop()
        finally:
            if ended and ended.get("owns_action_trace"):
                trace = ended["action_trace"]
                try:
                    trace.emit("session_closed", {"reason": "native_show_" + reason,
                                                   "source_through": show_source_time(ended)})
                finally:
                    trace.close()


def dispatch_public(sid, session, key_file, speech, start, end, *, boundary=False):
    """Start at most one public worker, as soon as its speech dependency is ready."""
    with LOCK:
        if LIVE.get(sid) is not session or not speech:
            return False
        public = session["on_air"]
        if (
            not public.enabled or public.busy
            or session["offset"] + time.monotonic() - session["created"] - end > 20
            or end <= public.last_attempt
            or end - public.last_attempt < 8
        ):
            return False
        public.last_attempt, public.busy = end, True
        try:
            threading.Thread(
                target=make_public_graphic,
                args=(sid, session, key_file, speech, end, start),
                daemon=True,
            ).start()
        except RuntimeError:
            public.busy = False
            return False  # Optional output cannot stop private continuity; no retry.
        return True


def judge_audience(sid, session, key_file):
    started = time.monotonic()
    try:
        if not (session.get("offline") or session.get("audience_openai")):
            return
        with LOCK:
            if LIVE.get(sid) is not session:
                return
            examined_card = public_state(sid, session).get("graphic") or {}
            revision = session.get("direction_revision", 0)
        questions = audience.poll(sid)["questions"]
        context = {
            "goal": session["brief"]["goal"],
            "intentions": [
                {"text": i["text"], "status": i["status"]}
                for i in session["memory"]["items"]
            ],
            "activity": session["production"].activity,
            "preferences": session["production"].plans["preferences"],
            "prepared_research": session["production"].shelf,
            "current_question": {
                k: examined_card.get(k, "") for k in ("id", "line", "attribution")
            } if examined_card.get("kind") == "question" else {},
            "previously_shown_ids": [q["id"] for q in questions if q["id"] in session["shown_questions"]],
        }
        if not questions:
            return
        decisions = select_questions(
            OpenAIProducer(_read_key(key_file)), questions, context
        )
        with LOCK:
            if (
                LIVE.get(sid) is not session
                or audience.owner != sid
                or time.monotonic() - started > 20
                or session.get("direction_revision", 0) != revision
            ):
                return
            # Deletion/clear during inference wins; re-check the current inbox.
            available = {q["id"]: q for q in audience.poll(sid)["questions"]}
            session["question_decisions"] = [
                d for d in decisions if d["message_id"] in available
            ]
            session["question_decision_serial"] = session.get("question_decision_serial", 0) + 1
            session["question_context_at"] = (context.get("activity") or {}).get("at")
            if (session["production"].activity or {}).get("at") == session[
                "question_context_at"
            ]:
                for d in session["question_decisions"]:
                    if d["decision"] == "show_now":
                        q = available[d["message_id"]]
                        if session["on_air"].apply_card(
                            "show", public_question(d, q), expected_id=examined_card.get("id")
                        ):
                            if audience.pin_question(sid, q["id"]):
                                session["direction_revision"] = (
                                    session.get("direction_revision", 0) + 1
                                )
                                session["shown_questions"].add(q["id"])
                            else:
                                session["on_air"].withdraw_question(set())
    except Exception:
        # Optional question selection failure must not stop the presenter feed.
        with LOCK:
            session["question_decisions"] = []
    finally:
        with LOCK:
            session["question_busy"] = False


def make_public_graphic(sid, session, key_file, speech, source_end=None, source_start=None):
    started = time.monotonic()
    public = session["on_air"]
    effects = {"result_accepted": False, "context_changed": False,
               "graphic_queued": False, "question_resolved": False, "visual_changed": False}
    try:
        with LOCK:
            if LIVE.get(sid) is not session:
                return
            state = public_state(sid, session)
            examined_card = state.get("graphic") or {}
            question = state.get("graphic") or {}
            if question.get("kind") != "question":
                question = None
            audience_allowed = bool(
                session.get("offline") or session.get("test_question")
                or session.get("audience_openai")
            )
            if not audience_allowed:
                question = None
            if question and source_start is not None and question.get("displayed_at", 0) > (
                session["wall_created"] + source_start - session["offset"]
            ):
                question = None  # A cut spanning question display cannot answer it.
            materials = session.get("materials")
            revision = session.get("direction_revision", 0)
        direction = propose_graphic(
            OpenAIProducer(_read_key(key_file)),
            speech,
            list(public.recent) if audience_allowed else [],
            state["context"],
            question=question,
            catalog=materials.catalog if materials else [],
            contact_sheet=materials.contact_sheet if materials else b"",
            visual=state.get("visual"),
            current_card=examined_card if (
                audience_allowed or examined_card.get("kind") not in ("question", "poll")
            ) else None,
        )
        with LOCK:
            if (
                LIVE.get(sid) is session
                and time.monotonic() - started <= 20
                and session.get("direction_revision", 0) == revision
                and (
                    source_end is None
                    or session["offset"] + time.monotonic() - session["created"]
                    - source_end <= 20
                )
            ):
                effects["result_accepted"] = True
                question_action = direction.get("question_status")
                if question and audience_allowed and question_action in ("answered", "clear") and public.resolve_question(
                    direction.get("question_id"), question_action
                ):
                    effects["question_resolved"] = True
                    if audience.owner == sid:
                        audience.unpin_question(sid)
                    session["direction_revision"] = revision + 1
                    record_mic_event(
                        session, "direction", question_status=question_action,
                        resolution_applied=True, changed=True,
                        reason="semantic_resolution",
                    )
                before_context = public.snapshot().get("context") or {}
                public.update_context(direction.get("context"), action=direction.get(
                    "context_action", "set" if direction.get("context") else "keep"
                ))
                after_context = public.snapshot().get("context") or {}
                effects["context_changed"] = any(
                    before_context.get(k) != after_context.get(k) for k in ("subject", "purpose")
                )
                effects["graphic_queued"] = public.apply_card(
                    direction.get("card_action", "show" if direction.get("graphic") else "keep"),
                    direction.get("graphic"), direction.get("hold_seconds"),
                    expected_id=examined_card.get("id"),
                )
                after_card = public.snapshot().get("graphic") or {}
                if examined_card.get("kind") == "question" and after_card.get("id") != examined_card.get("id"):
                    if audience.owner == sid:
                        audience.unpin_question(sid)
                    session["direction_revision"] = revision + 1
                before_visual = (session.get("visual") or {}).get("asset_id")
                visual_action = direction.get("visual_action", "keep")
                if visual_action == "clear":
                    session["visual"] = None
                elif visual_action == "show" and materials:
                    proposed = direction.get("visual") or {}
                    item = next(
                        (i for i in materials.catalog if i["id"] == proposed.get("asset_id")),
                        None,
                    )
                    if proposed.get("kind") == "asset" and item and materials.get(item["id"]):
                        session["visual"] = {
                            "kind": "asset", "asset_id": item["id"],
                            "until": time.time() + 45,
                        }
                effects["visual_changed"] = before_visual != (session.get("visual") or {}).get("asset_id")
    except Exception:
        pass  # Optional on-air generation must never halt the private monitor.
    finally:
        with LOCK:
            if LIVE.get(sid) is session:
                session["public_timing"] = {
                    "source_end": source_end,
                    "processing_s": round(time.monotonic() - started, 3),
                    "source_to_public_result_s": round(max(
                        0, session["offset"] + time.monotonic() - session["created"] - source_end
                    ), 3) if source_end is not None else None,
                    **effects,
                }
            public.busy = False


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8786)
    p.add_argument("--key-file", type=Path)
    p.add_argument(
        "--show-folder", type=Path, default=DEFAULT_SHOW_FOLDER,
        help="Optional local show notes and still images, pinned when a show starts.",
    )
    p.add_argument(
        "--audience-openai",
        action="store_true",
        help="Enable separately authorized Twitch question processing; never feeds chat to media understanding.",
    )
    p.add_argument(
        "--enable-live-production", action="store_true",
        help="Enable Realtime Twitch production after explicit live mode and capability checks; offline tests stay simulated.",
    )
    p.add_argument(
        "--producer-chat-sender", choices=("separate", "broadcaster_labelled"), default="separate",
        help="Use a separate Producer account, or explicitly use the broadcaster with '(From Producer)' attribution.",
    )
    p.add_argument(
        "--simulate-chat", action="store_true",
        help="Include fictional incoming chat pixels for a prepared live demo; offline screen tests already include them.",
    )
    p.add_argument(
        "--chat-fixture", type=Path,
        help="Optional validated fictional chat JSON, pinned for new shows. Default remains fresh generated chat.",
    )
    p.add_argument(
        "--obs-input-port", type=int,
        help="Accept the selected OBS MPEG-TS recording feed on 127.0.0.1 at this port; no browser media capture.",
    )
    p.add_argument(
        "--obs-recording-dir", type=Path,
        help="Explicitly save native OBS input recordings in this local directory.",
    )
    p.add_argument(
        "--obs-action-trace-dir", type=Path,
        help="Save private native OBS model-action/receipt journals for diagnosis; not a full replay bundle.",
    )
    p.add_argument("--phone-bind", help="Optional explicit private IPv4 address for the read-only phone monitor.")
    p.add_argument("--phone-port", type=int, default=8787)
    p.add_argument("--phone-link-file", type=Path,
                   help="Save the ephemeral phone pairing link as a private local JSON file.")
    a = p.parse_args()
    try:
        chat_fixture = load_chat_fixture(a.chat_fixture) if a.chat_fixture else None
        if a.obs_recording_dir is not None and a.obs_input_port is None:
            raise ValueError("--obs-recording-dir requires --obs-input-port.")
        if a.obs_action_trace_dir is not None and a.obs_input_port is None:
            raise ValueError("--obs-action-trace-dir requires --obs-input-port.")
        if a.obs_input_port == a.port:
            raise ValueError("OBS input and the browser screens require different local ports.")
        if a.phone_link_file and not a.phone_bind:
            raise ValueError("--phone-link-file requires --phone-bind.")
        if a.phone_bind and a.phone_port in (a.port, a.obs_input_port):
            raise ValueError("The phone monitor requires a separate port.")
        obs_config = OBSConfig(port=a.obs_input_port, recording_dir=a.obs_recording_dir,
                               startup_timeout=120) if a.obs_input_port is not None else None
    except ValueError as exc:
        p.error(str(exc))
    s = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    s.key_file = a.key_file
    s.show_folder = a.show_folder
    s.audience_openai = a.audience_openai
    s.enable_live_production = a.enable_live_production
    s.producer_chat_sender = a.producer_chat_sender
    s.simulate_chat = a.simulate_chat
    s.chat_fixture_override = chat_fixture
    s.obs_config = obs_config
    s.obs_action_trace_dir = a.obs_action_trace_dir
    phone = None

    def phone_snapshot():
        with LOCK:
            session = next(iter(LIVE.values()), None)
            return presenter_mirror_snapshot(session, source_available=source_available,
                                             source_time=show_source_time)

    print(f"Producer desk at http://127.0.0.1:{a.port}/", flush=True)
    try:
        if a.phone_bind:
            phone = start_phone_monitor(a.phone_bind, a.phone_port, phone_snapshot)
            if a.phone_link_file:
                a.phone_link_file.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(a.phone_link_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as output:
                    os.fchmod(output.fileno(), 0o600)
                    json.dump({"pairing_url": phone.pairing_url}, output)
                print(f"Private phone link saved to {a.phone_link_file}", flush=True)
            else:
                print(f"Private phone link (keep off the broadcast): {phone.pairing_url}", flush=True)
        s.serve_forever()
    finally:
        if phone:
            phone.close()
        for sid in list(LIVE):
            stop_show({"id": sid, "reason": "ended"})
        s.server_close()


if __name__ == "__main__":
    main()

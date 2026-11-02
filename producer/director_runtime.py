"""Deterministic local routing for one model-led Realtime show director."""

import base64
import copy
import hashlib
import math
import threading
import time

from on_air import KINDS
from realtime_director import RealtimeDirector
from editor_consult import EditorConsult
from screen_chat import ScreenChat
from director_production import DirectorProduction


def note_brief(notes):
    """A compatibility envelope; the director interprets the original notes itself."""
    if not isinstance(notes, str) or not 0 < len(notes.strip()) <= 20000:
        raise ValueError("Add up to 20,000 characters of show notes.")
    return {
        "title": notes.strip().splitlines()[0][:100],
        "goal": "Help the presenter and audience make the best possible show.",
        "audience": "As described in the show notes",
        "context": "The original show notes are supplied directly to the live director.",
        "tip": "Use production judgment.", "rundown": [],
        "intentions": [{"id": "show", "text": "Develop the show from the supplied notes",
                        "kind": "explain", "priority": "essential", "next_action": ""}],
        "timing": {"mode": "flexible", "minutes": 30}, "research": [],
        "recovery": "Use the actual show context.",
    }


def line(value, limit=180):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError("Screen text exceeds its rendering bounds.")
    return value.strip()


def audience_line(question):
    """Use the complete selected comment, or decline it without shortening."""
    value = question.get("text")
    if (
        not isinstance(value, str) or not value.strip() or len(value) > 180
        or len(value.encode("utf-16-le", errors="surrogatepass")) > 180 * 2
    ):
        raise ValueError("The complete audience comment does not fit; choose another or leave it off screen.")
    return value


CONFIDENCE_TIMING_POLICY = "source-clock-v1"


class ShowDirector:
    def __init__(self, sid, session, *, key, lock, active, public_state, audience,
                 allow_audience=False, director_class=RealtimeDirector):
        self.sid, self.session, self.lock = sid, session, lock
        self.active, self.public_state, self.audience = active, public_state, audience
        self.screen_mode = session.get("audience_input") == "screen"
        self.screen_chat = ScreenChat(sid) if self.screen_mode else None
        if self.screen_chat is not None:
            session["screen_chat"] = self.screen_chat
        self.comment_source = self.screen_chat if self.screen_mode else audience
        self.allow_audience = self.screen_mode or allow_audience
        self.audio_lock = threading.Lock()
        self.audio_end = session["offset"]
        self.last_frame = session["offset"]
        self.caption = None
        self.sequence = 0
        self.closed = False
        self.featured_counts = {}
        self.confidence_timing_policy = session.get("confidence_timing_policy", CONFIDENCE_TIMING_POLICY)
        if self.confidence_timing_policy not in ("legacy", CONFIDENCE_TIMING_POLICY):
            raise ValueError("Unknown confidence timing policy.")
        session["confidence"] = None
        session.pop("confidence_shown_at_source_s", None)
        session["director_revision"] = 0
        self.production = None
        self.production_receipts = []
        if session.get("production_policy") == "realtime-production-v1" and session.get("production"):
            self.production = DirectorProduction(
                session["production"], offline=session.get("offline", True),
                active=lambda: not self.closed and self.active()
                and not getattr(getattr(self, "core", None), "closed", False),
                public_enabled=lambda: session["on_air"].enabled,
                public_state=self.production_public_state, clock=lambda: self.audio_end,
                live_enabled=session.get("live_production_enabled") is True
                and session.get("offline") is False,
            )
            adapter = session["production"].adapter
            if session.get("offline", True) and getattr(adapter, "platform", None) == "fixture":
                adapter.clock = lambda: self.audio_end
            self.production_receipts = self.production.snapshot(self.public_state())["receipts"]
            if session.get("rehearsal") is not None:
                session["rehearsal"].producer_posts = self.producer_post_snapshot
        self.editor = EditorConsult(key, context=self.review_context,
                                    active=lambda: not self.closed and self.active())
        try:
            self.core = director_class(
                key, brief={"notes": session.get("notes", ""), "brief": session["brief"],
                            "research_packet": session.get("research_packet", "")},
                state=self.state, apply=self.apply, recall=self.recall,
                transcript=self.transcribe, offset=session["offset"],
                contact_sheet=session["materials"].contact_sheet, consult=self.editor.request,
                **({"production": self.production_action} if self.production else {}),
                **({"event_sink": session["action_trace"].emit} if session.get("action_trace") else {}),
            )
        except Exception:
            self.editor.close()
            raise

    def _check(self):
        if self.closed or not self.active():
            raise ValueError("The selected show has stopped.")

    def _messages(self):
        if not self.allow_audience or self.comment_source.owner != self.sid:
            return []
        result = self.comment_source.poll(self.sid, self.audio_end) if self.screen_mode else self.comment_source.poll(self.sid)
        questions = result["questions"][:20]
        available = {q["id"] for q in questions}
        self.featured_counts = {key: value for key, value in self.featured_counts.items() if key in available}
        keys = ("id", "text", "author", "source", "frame_id", "frame_source_time") if self.screen_mode else ("id", "text", "author")
        return [{**{k: q[k] for k in keys},
                 "times_featured": self.featured_counts.get(q["id"], 0)} for q in questions]

    def _state(self):
        public = self.public_state()
        graphic = public.get("graphic") or {}
        if not self.allow_audience and graphic.get("kind") in ("question", "poll"):
            public = {**public, "graphic": {"kind": graphic["kind"], "redacted": True}}
        materials = self.session["materials"]
        state = {
            "revision": self.session["director_revision"],
            "source_time": round(self.audio_end, 3),
            "confidence": self._confidence_state(),
            "public": public,
            "working_notes": (self.session.get("memory") or {}).get("director_notes", ""),
            "audience": self._messages(),
            "assets": [{k: item[k] for k in ("id", "title", "width", "height")}
                       for item in materials.catalog],
            "offline": self.session.get("offline", True),
            "public_output_enabled": self.session["on_air"].enabled,
            "deeper_advice": self.editor.snapshot(),
            "capabilities": ["update_local_screens", "read_selected_show_transcript", "request_private_editor_review"],
        }
        if self.screen_mode:
            state.update(audience_source="screen", screen_frames=self.screen_chat.frames(self.audio_end))
        if self.confidence_timing_policy == CONFIDENCE_TIMING_POLICY:
            state["confidence_timing_policy"] = self.confidence_timing_policy
        if self.production:
            state["production"] = self.production.snapshot(public)
            state["production"]["receipts"] = copy.deepcopy(self.production_receipts)
            state["capabilities"].append("production_action")
        return state

    def _confidence_state(self):
        cue = copy.deepcopy(self.session.get("confidence"))
        shown_at = self.session.get("confidence_shown_at_source_s")
        if cue and self.confidence_timing_policy == CONFIDENCE_TIMING_POLICY and shown_at is not None:
            cue.update(shown_at_source_s=shown_at,
                       visible_for_s=max(0.0, round(self.audio_end - shown_at, 3)))
        return cue

    def _set_confidence(self, cue):
        if cue is None:
            self.session.pop("confidence_shown_at_source_s", None)
        elif self.confidence_timing_policy == CONFIDENCE_TIMING_POLICY and (
            cue != self.session.get("confidence") or "confidence_shown_at_source_s" not in self.session
        ):
            self.session["confidence_shown_at_source_s"] = round(self.audio_end, 3)
        self.session["confidence"] = cue

    def state(self):
        with self.lock:
            self._check()
            return self._state()

    def production_public_state(self):
        with self.lock:
            self._check()
            return copy.deepcopy(self.public_state())

    def production_action(self, args, expected_state):
        """Reserve/route outside the show lock; never wait on Twitch during Stop."""
        if not self.production or not isinstance(expected_state, dict):
            raise ValueError("Production tools are unavailable for this show.")
        with self.lock:
            self._check()
            if expected_state.get("revision") != self.session["director_revision"]:
                return {"ok": False, "reason": "The screen state changed; reconsider the current state.", "state": self._state()}
        result = self.production.execute(args, expected_state)
        with self.lock:
            if self.closed or not self.active():
                # Preserve the actual dispatch receipt, but never append a
                # delivery echo or alter a screen after Stop.
                return {**result, "show_stopped": True}
            receipt = result.get("receipt") or {}
            if (result.get("ok") and receipt.get("state") == "done" and receipt.get("message")
                    and (receipt.get("simulated") or self.session.get("simulated_chat"))):
                self.record_producer_post(receipt)
            self.production_receipts = self.production.snapshot(self.public_state())["receipts"]
            self.session["director_revision"] += 1
            if self.session.get("action_trace"):
                self.session["action_trace"].emit("production_commit", {
                    "revision": self.session["director_revision"], "action": args.get("action"),
                    "plan_id": receipt.get("plan_id"),
                })
            result["state"] = self._state()
        return result

    def producer_post_snapshot(self):
        with self.lock:
            return copy.deepcopy(self.session.get("producer_posts", []))

    def record_producer_post(self, receipt):
        """A confirmed outgoing message becomes future chat-layer pixels only."""
        author = receipt.get("display_name") or receipt.get("sender_login")
        sender = receipt.get("sender_id")
        if not isinstance(author, str) or not author or not isinstance(sender, str) or not sender:
            return  # Never invent a sender when the adapter has not confirmed one.
        posts = self.session.setdefault("producer_posts", [])
        ident = "producer-" + str(receipt["plan_id"])
        if any(item["id"] == ident for item in posts):
            return
        posts.append({"type": "channel.chat.message", "id": ident,
                      "at": round(self.audio_end + 0.001, 3), "user_id": sender,
                      "author": author, "text": receipt["message"]})
        if self.session.get("action_trace"):
            self.session["action_trace"].emit("production_chat", {"event": posts[-1]})

    def own_chat_echo(self, observed):
        """Match known delivered text and sender, never all comments by the host."""
        author, text = observed["author"].casefold(), observed["text"]
        if any(post["author"].casefold() == author and post["text"] == text
               for post in self.session.get("producer_posts", [])):
            return True
        return any(row.get("state") == "done" and row.get("message") == text
                   and author in {str(row.get("sender_login", "")).casefold(),
                                  str(row.get("display_name", "")).casefold()}
                   for row in self.production_receipts)

    def apply(self, args, expected_state):
        """Validate routes/identities, then atomically apply the model's choices."""
        if not isinstance(args, dict) or not isinstance(expected_state, dict):
            raise ValueError("Invalid production instruction.")
        confidence = args.get("confidence", {"action": "keep"})
        card = args.get("card", {"action": "keep"})
        context = args.get("context", {"action": "keep"})
        visual = args.get("visual", {"action": "keep"})
        for part, allowed in ((confidence, ("keep", "show", "clear")),
                              (card, ("keep", "show", "clear")),
                              (context, ("keep", "set", "clear")),
                              (visual, ("keep", "show", "clear"))):
            if not isinstance(part, dict) or part.get("action") not in allowed:
                raise ValueError("Unknown screen action.")
        confidence, card = dict(confidence), dict(card)
        has_screen_comment = "screen_comment" in args
        if has_screen_comment and not self.screen_mode:
            raise ValueError("Screen comments require the selected screen-input mode.")
        confidence_line = line(confidence.get("line", ""))
        card_line = line(card.get("line", ""))
        verbatim_audience = self.screen_mode or self.session.get("audience_text_policy", "verbatim") != "legacy-summary"
        confidence_question_id = confidence.get("question_id") if verbatim_audience else None
        public_question = card["action"] == "show" and card.get("kind") == "question"
        subject = line(context.get("subject", ""), 80)
        purpose = line(context.get("subtitle", context.get("purpose", "")), 120)
        notes = args.get("working_notes")
        if notes is not None:
            notes = line(notes, 4000)
        duration = card.get("hold_seconds", 30)
        if type(duration) is not int or not 1 <= duration <= 120:
            raise ValueError("Choose a display duration from 1 to 120 seconds.")
        if confidence["action"] in ("show", "set") and not confidence_line and not (
            verbatim_audience and (confidence_question_id or public_question or has_screen_comment)
        ):
            raise ValueError("A confidence cue needs text.")
        if context["action"] in ("show", "set") and not subject:
            raise ValueError("A context label needs a subject.")
        with self.lock:
            self._check()
            if expected_state.get("revision") != self.session["director_revision"]:
                return {"ok": False, "reason": "The screen state changed; reconsider the current state.", "state": self._state()}
            if not self.session["on_air"].enabled and (
                card["action"] == "show" or context["action"] == "set" or visual["action"] == "show"
            ):
                raise ValueError("The owner has disabled public output for this show.")
            messages = {q["id"]: q for q in self._messages()}
            observed = None
            if has_screen_comment:
                if not public_question and confidence["action"] != "show":
                    raise ValueError("Select a screen comment for an audience or presenter question.")
                observed = self.screen_chat.prepare_observation(
                    args["screen_comment"], expected_state.get("screen_frames"), self.audio_end,
                )
                if self.own_chat_echo(observed):
                    raise ValueError("Producer's own delivery echo is not a new audience question.")
                if public_question:
                    if card.get("question_id") not in (None, observed["id"]):
                        raise ValueError("Use the observed screen comment identity.")
                    card["question_id"] = observed["id"]
                if confidence["action"] == "show" and (not public_question or confidence_question_id is not None or not confidence_line):
                    if confidence_question_id not in (None, observed["id"]):
                        raise ValueError("Use the observed screen comment identity.")
                    confidence_question_id = observed["id"]
                messages[observed["id"]] = observed
            confidence_question = None
            if verbatim_audience and confidence["action"] == "show":
                selected_id = confidence_question_id
                if public_question and (selected_id is not None or not confidence_line):
                    if selected_id is not None and selected_id != card.get("question_id"):
                        raise ValueError("Use the same audience comment on both screens, or keep the presenter cue unchanged.")
                    selected_id = card.get("question_id")
                if selected_id is not None:
                    if not isinstance(selected_id, str) or not selected_id:
                        raise ValueError("Choose an available audience comment identity.")
                    confidence_question = messages.get(selected_id)
                    if not confidence_question:
                        raise ValueError("That audience comment is no longer available.")
                    confidence_line = audience_line(confidence_question)
            graphic = None
            if card["action"] in ("show", "set"):
                kind = card.get("kind")
                if kind not in (*KINDS, "question") or (
                    not card_line and not (verbatim_audience and kind == "question")
                ):
                    raise ValueError("Choose an available card type and legible text.")
                attribution = {"result": "Reported on stream", "takeaway": "Takeaway · Producer",
                               "reference": "Reference", "title": "Now"}.get(kind, "Producer")
                if kind != "question" and self.session.get("card_attribution_policy", "model") == "model":
                    attribution = line(card.get("attribution", ""), 120) or attribution
                graphic = {"kind": kind, "line": card_line, "attribution": attribution}
                if kind == "question":
                    if verbatim_audience and not isinstance(card.get("question_id"), str):
                        raise ValueError("Choose an available audience comment identity.")
                    question = messages.get(card.get("question_id"))
                    if not question:
                        raise ValueError("That audience question is no longer available.")
                    if verbatim_audience:
                        graphic["line"] = audience_line(question)
                    elif card_line != question["text"] and not card_line.startswith("Summary: "):
                        graphic["line"] = "Summary: " + card_line
                    if len(graphic["line"]) > 180:
                        raise ValueError("The attributed question does not fit.")
                    origin = " · from screen" if question.get("source") == "screen" else " · from chat"
                    graphic.update(message_id=question["id"], attribution=question["author"] + origin)
            asset_id = visual.get("asset_id", "")
            if visual["action"] in ("show", "set") and not self.session["materials"].get(asset_id):
                raise ValueError("Choose an image from this show's pinned catalog.")
            public = self.session["on_air"]
            examined = public.snapshot().get("graphic") or {}
            if observed is not None:
                self.screen_chat.commit_observation(observed)
            if graphic and graphic["kind"] == "question" and not self.comment_source.pin_question(self.sid, graphic["message_id"]):
                raise ValueError("That audience question was removed before it could be selected.")
            action = "show" if card["action"] == "set" else card["action"]
            changed = public.apply_card(action, graphic, duration, expected_id=examined.get("id"))
            if changed and graphic and graphic["kind"] == "question":
                ident = graphic["message_id"]
                self.featured_counts[ident] = self.featured_counts.get(ident, 0) + 1
            after = public.snapshot().get("graphic") or {}
            if not (graphic and graphic["kind"] == "question") and examined.get("kind") == "question" and after.get("id") != examined.get("id"):
                self.comment_source.unpin_question(self.sid)
            context_action = "set" if context["action"] == "show" else context["action"]
            public.update_context({"subject": subject, "purpose": purpose}, action=context_action)
            if confidence["action"] == "clear":
                self._set_confidence(None)
            elif confidence["action"] in ("show", "set"):
                self._set_confidence(
                    {"line": confidence_line, "kind": "question",
                     "message_id": confidence_question["id"], "attribution": confidence_question["author"]}
                    if confidence_question else {"line": confidence_line, "kind": "editorial"}
                )
            if visual["action"] == "clear":
                self.session["visual"] = None
            elif visual["action"] in ("show", "set"):
                self.session["visual"] = {"kind": "asset", "asset_id": asset_id, "until": time.time() + duration}
            elif self.session.get("visual") and (self.public_state().get("visual") or {}).get("asset_id") == self.session["visual"].get("asset_id"):
                self.session["visual"]["until"] = time.time() + duration
            if notes is not None and self.session.get("memory") is not None:
                self.session["memory"]["director_notes"] = notes
            self.session["director_revision"] += 1
            if self.session.get("action_trace"):
                # The invocation and receipt are outside this state lock. A
                # scene can be sampled between either of them and this commit;
                # journal its causal position before another sampler can run.
                self.session["action_trace"].emit("screen_commit", {
                    "revision": self.session["director_revision"],
                })
            return {"ok": True, "state": self._state()}

    def recall(self, args, before):
        if not isinstance(args, dict) or not isinstance(before, (int, float)) or not math.isfinite(before):
            raise ValueError("Invalid transcript request.")
        with self.lock:
            self._check()
            cutoff = min(before, self.audio_end)
            return self.session["transcript"].read(
                before=cutoff, query=args.get("query", ""), start=args.get("start"),
                end=args.get("end"), limit=args.get("limit", 12),
            )

    def review_context(self, before):
        with self.lock:
            self._check()
            cutoff = min(before, self.audio_end)
            return {
                "notes": self.session.get("notes", ""),
                "research_packet": self.session.get("research_packet", ""),
                "state": self._state(),
                "transcript": self.session["transcript"].read(before=cutoff),
                "source_through": cutoff,
            }

    def transcribe(self, start, end, text, status="transcribed"):
        with self.lock:
            self._check()
            self.session["transcript"].record(start, end, text, status)
            if text:
                self.caption = {"text": text, "expires": time.monotonic() + 6}

    def caption_snapshot(self):
        if not self.caption or self.caption["expires"] <= time.monotonic():
            return None
        words = self.caption["text"].split()
        while words and len(" ".join(words)) > 170:
            words.pop(0)
        return {"text": " ".join(words), "partial": False}

    def append(self, sequence, encoded, frame=None, source_end=None):
        with self.audio_lock:
            with self.lock:
                self._check()
                if type(sequence) is not int or sequence != self.sequence:
                    raise ValueError("Realtime packets are out of order.")
                start = self.session["offset"] + sequence * 0.5
                end = start + 0.5
                elapsed = self.session["offset"] + time.monotonic() - self.session["created"]
                if end > elapsed + 1.5:
                    raise ValueError("Realtime input is ahead of the selected source.")
            if self.session.get("rehearsal"):
                encoded = base64.b64encode(self.session["rehearsal"].audio24(start, end, available_through=elapsed)).decode()
                if end - self.last_frame >= 4:
                    frame = base64.b64encode(self.session["rehearsal"].frame_at(end, available_through=elapsed)).decode()
                    source_end = end
            try:
                pcm = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError) as exc:
                raise ValueError("Invalid selected audio packet.") from exc
            if len(pcm) != 24000:
                raise ValueError("Expected half a second of 24 kHz mono PCM.")
            jpeg = None
            if frame is not None:
                jpeg = base64.b64decode(frame, validate=True)
                if not jpeg.startswith(b"\xff\xd8") or not jpeg.endswith(b"\xff\xd9") or len(jpeg) > 350000:
                    raise ValueError("Invalid selected image.")
                if isinstance(source_end, bool) or not isinstance(source_end, (float, int)) or not math.isfinite(source_end) or not self.session["offset"] <= source_end <= end + 0.1:
                    raise ValueError("Image belongs to another source time.")
                if self.screen_mode and source_end > end:
                    raise ValueError("Screen chat cannot read a future source image.")
            self.core.append(sequence, encoded)
            with self.lock:
                self._check()
                self.sequence += 1
                self.audio_end = end
                self.session["seen"] = time.monotonic()
                if self.session.get("events") and not self.screen_mode:
                    self.session["events"].advance(end)
            if jpeg:
                if self.screen_mode:
                    frame_id = self.screen_chat.frame_id(jpeg, source_end)
                    self.core.image(jpeg, source_end, frame_id=frame_id)
                    with self.lock:
                        self._check()
                        metadata = self.screen_chat.register_frame(frame_id, source_end, hashlib.sha256(jpeg).hexdigest())
                    if self.session.get("action_trace"):
                        self.session["action_trace"].emit("input_frame", metadata)
                else:
                    self.core.image(jpeg, source_end)
                self.last_frame = end
            return {"accepted": True, "through": end}

    def snapshot(self):
        with self.lock:
            self._check()
            raw = self.core.snapshot()
            status = "error" if raw.get("error") or raw.get("closed") else "listening" if raw.get("connected") else "connecting"
            result = {**raw, "status": status, "detail": raw.get("status"), "confidence": copy.deepcopy(self.session.get("confidence")),
                      "memory": copy.deepcopy(self.session.get("memory")), "through": self.audio_end}
            self.session["seen"] = time.monotonic()
            return result

    def close(self):
        self.closed = True
        self.editor.close()
        self.core.close()
        if self.screen_chat is not None:
            self.screen_chat.close()

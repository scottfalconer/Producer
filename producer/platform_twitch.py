"""Official own-channel Twitch actions. No fallback, blind retry, or announcements."""

import os
import time
import requests
from twitch import authorization

SCOPES = {
    "chat": "user:write:chat",
    "reference": "user:write:chat",
    "marker": "channel:manage:broadcast",
    "title": "channel:manage:broadcast",
    "category": "channel:manage:broadcast",
    "poll": "channel:manage:polls",
}
SENDER_MODES = ("separate", "broadcaster_labelled")
PRODUCER_ATTRIBUTION = "(From Producer) "


class TwitchAdapter:
    platform = "twitch"

    def __init__(self, auth=authorization, transport=requests.request, *,
                 sender_auth=None, sender_token_file=None, sender_mode="separate"):
        if sender_mode not in SENDER_MODES:
            raise ValueError("Choose separate or broadcaster_labelled chat sender mode.")
        self.sender_mode = sender_mode
        self.auth, self.transport = auth, transport
        sender_path = sender_token_file or os.environ.get("TWITCH_PRODUCER_SENDER_TOKEN_FILE")
        self.sender_auth = sender_auth or (
            (lambda: authorization(sender_path)) if sender_path else None
        )
        self.identity = None
        self.headers = None
        self.sender_identity = None
        self.sender_headers = None
        self.sender_checked = 0
        self.sender_user_id = None
        self.checked = 0
        self.backoff_until = 0
        self.chat_vote = None
        self.chat_ready = lambda: False

    def refresh(self):
        if self.identity is None or time.monotonic() - self.checked > 300:
            _, identity, headers = self.auth()
            if self.identity and identity.get("user_id") != self.identity.get("user_id"):
                raise ValueError("The broadcaster account changed; reconnect before continuing.")
            self.identity, self.headers = identity, headers
            self.checked = time.monotonic()

    def refresh_sender(self):
        """Validate the explicitly selected sender; never fall back between accounts."""
        self.refresh()
        if self.sender_mode == "broadcaster_labelled":
            self.sender_identity, self.sender_headers = None, None
            if not self.identity.get("user_id") or not self.identity.get("login"):
                raise ValueError("The broadcaster chat identity is unavailable.")
            if SCOPES["chat"] not in self.identity.get("scopes", []):
                raise ValueError("The broadcaster account needs user:write:chat.")
            self.sender_identity, self.sender_headers = self.identity, self.headers
            self.sender_user_id = self.identity["user_id"]
            self.sender_checked = self.checked
            return
        if self.sender_auth is None:
            raise ValueError("Connect a separate Producer chat account before posting.")
        if self.sender_identity is None or time.monotonic() - self.sender_checked > 300:
            self.sender_identity, self.sender_headers = None, None
            _, identity, headers = self.sender_auth()
            uid = identity.get("user_id")
            if not uid or not identity.get("login") or uid == self.identity["user_id"]:
                raise ValueError("Producer chat must use a different account from the broadcaster.")
            if self.sender_user_id is not None and uid != self.sender_user_id:
                raise ValueError("The Producer chat account changed; reconnect before posting.")
            if SCOPES["chat"] not in identity.get("scopes", []):
                raise ValueError("The Producer chat account needs user:write:chat.")
            self.sender_user_id = uid
            self.sender_identity, self.sender_headers = identity, headers
            self.sender_checked = time.monotonic()

    def sender_status(self):
        try:
            self.refresh_sender()
        except (OSError, requests.RequestException, ValueError, KeyError):
            return {
                "connected": False, "sender_mode": self.sender_mode,
                "detail": "The broadcaster account with user:write:chat is not connected."
                if self.sender_mode == "broadcaster_labelled" else
                "A separate Producer chat account with user:write:chat is not connected.",
            }
        return {
            "connected": True, "user_id": self.sender_identity["user_id"],
            "login": self.sender_identity["login"], "sender_mode": self.sender_mode,
        }

    def request(self, method, route, *, sender=False, **kwargs):
        self.refresh()
        if sender:
            self.refresh_sender()
        if time.time() < self.backoff_until:
            raise ValueError("Twitch rate limit is cooling down; no action attempted.")
        r = self.transport(
            method,
            "https://api.twitch.tv/helix/" + route,
            headers=self.sender_headers if sender else self.headers,
            timeout=(5, 10),
            allow_redirects=False,
            **kwargs,
        )
        if r.status_code == 429:
            try:
                self.backoff_until = max(
                    time.time() + 1,
                    float(r.headers.get("Ratelimit-Reset", time.time() + 60)),
                )
            except (ValueError, TypeError):
                self.backoff_until = time.time() + 60
        return r

    def read(self, route, params):
        r = self.request("GET", route, params=params)
        if r.status_code != 200:
            raise ValueError(f"Twitch {route} check returned HTTP {r.status_code}.")
        return r.json()["data"]

    def capabilities(self):
        self.refresh()
        uid = self.identity["user_id"]
        user = self.read("users", {"id": uid})
        streams = self.read("streams", {"user_id": uid})
        shared = self.read("shared_chat/session", {"broadcaster_id": uid})
        scopes = self.identity.get("scopes", [])
        eligible = bool(
            user and user[0].get("broadcaster_type") in ("affiliate", "partner")
        )
        native = eligible and SCOPES["poll"] in scopes
        sender = self.sender_status()
        polls = (
            self.read("polls", {"broadcaster_id": uid, "first": 20}) if native else []
        )
        return {
            "platform": "twitch",
            "channel_id": uid,
            "channel": self.identity["login"],
            "online": bool(streams),
            "shared_chat": bool(shared),
            "capabilities": {k: sender["connected"] if k in ("chat", "reference") else s in scopes
                             for k, s in SCOPES.items()},
            "chat_sender": sender,
            "chat_read": "user:read:chat" in scopes,
            "native_polls": native,
            "poll_eligible": eligible,
            "active_poll": any(p.get("status") == "ACTIVE" for p in polls),
            "marker_note": "Requires live stream and Store past broadcasts; storage is not established by this API check.",
            "checked": time.time(),
        }

    def check_plans(self, plans):
        caps = self.capabilities()
        issues = []
        for p in plans:
            k = p["kind"]
            if k == "poll" and p.get("poll_mode") == "chat":
                if not caps["capabilities"]["reference"] or not caps["chat_read"]:
                    issues.append(
                        f"{p['id']}: chat voting needs chat-read and chat-write permissions."
                    )
                elif caps["shared_chat"]:
                    issues.append(f"{p['id']}: chat voting is disabled in shared chat.")
                continue
            if k in ("chat", "reference") and not caps["capabilities"][k]:
                issues.append(f"{p['id']}: {caps['chat_sender']['detail']}")
            elif not caps["capabilities"][k]:
                issues.append(f"{p['id']}: connect Twitch with {SCOPES[k]}.")
            elif k == "poll" and not caps["native_polls"]:
                issues.append(
                    f"{p['id']}: native polls require Affiliate or Partner status; prepare an explicit chat vote instead."
                )
            elif k == "poll" and caps["active_poll"]:
                issues.append(f"{p['id']}: another poll is already active.")
            elif k == "marker" and not caps["online"]:
                issues.append(
                    f"{p['id']}: stream markers require a live stream with past-broadcast storage."
                )
            elif k in ("chat", "reference") and caps["shared_chat"]:
                issues.append(
                    f"{p['id']}: shared chat would extend the approved audience; posting is disabled."
                )
        return caps, issues

    def execute(self, plan):
        try:
            self.refresh()
        except (OSError, requests.RequestException, ValueError, KeyError):
            return {"state": "needs_attention", "detail": "Broadcaster connection unavailable; nothing sent."}
        uid = self.identity["user_id"]
        k = plan["kind"]
        if k not in SCOPES:
            return {"state": "needs_attention", "detail": "Unknown Twitch action; nothing sent."}
        if k == "poll" and plan.get("poll_mode") == "chat":
            from production import vote_message

            if (
                not self.chat_vote
                or self.chat_vote.active
                or not self.chat_ready()
                or "user:read:chat" not in self.identity.get("scopes", [])
            ):
                return {
                    "state": "needs_attention",
                    "detail": "Chat vote unavailable or another vote is active.",
                }
            result = self.execute(
                {**plan, "kind": "reference", "text": vote_message(plan)}
            )
            if result["state"] == "done":
                self.chat_vote.start(plan)
                result.pop("remote_id", None)
                result["detail"] = (
                    "Chat vote invitation delivered; collecting one vote per person."
                )
            return result
        is_chat = k in ("chat", "reference")
        if is_chat:
            message = plan.get("text")
            if not isinstance(message, str) or not message.strip():
                return {"state": "needs_attention", "detail": "Chat text must contain 1–500 characters; nothing sent."}
            if self.sender_mode == "broadcaster_labelled":
                message = PRODUCER_ATTRIBUTION + message
            if len(message) > 500:
                return {"state": "needs_attention", "detail": "Chat text including its Producer attribution must fit 500 characters; nothing sent."}
            try:
                self.refresh_sender()
            except (OSError, requests.RequestException, ValueError, KeyError):
                return {
                    "state": "needs_attention",
                    "detail": "Broadcaster chat identity or user:write:chat unavailable; nothing sent."
                    if self.sender_mode == "broadcaster_labelled" else
                    "Separate Producer chat identity unavailable; nothing sent under the broadcaster account.",
                }
        elif SCOPES[k] not in self.identity.get("scopes", []):
            return {
                "state": "needs_attention",
                "detail": "Twitch permission is missing; nothing sent.",
            }
        if time.time() < self.backoff_until:
            return {
                "state": "needs_attention",
                "detail": "Twitch rate limit; nothing sent. No automatic retry.",
            }
        # Read preconditions before public writes. Failed checks cannot imply a successful action.
        try:
            if is_chat:
                if self.read("shared_chat/session", {"broadcaster_id": uid}):
                    return {
                        "state": "needs_attention",
                        "detail": "Shared chat active; approved channel boundary preserved.",
                    }
                route, method, body, params = (
                    "chat/messages",
                    "POST",
                    {"broadcaster_id": uid, "sender_id": self.sender_identity["user_id"], "message": message},
                    None,
                )
            elif k == "marker":
                route, method, body, params = (
                    "streams/markers",
                    "POST",
                    {"user_id": uid, "description": plan["text"]},
                    None,
                )
            elif k == "poll":
                polls = self.read("polls", {"broadcaster_id": uid, "first": 20})
                if any(p.get("status") == "ACTIVE" for p in polls):
                    return {
                        "state": "needs_attention",
                        "detail": "An existing poll is active; nothing created.",
                    }
                route, method, body, params = (
                    "polls",
                    "POST",
                    {
                        "broadcaster_id": uid,
                        "title": plan["text"],
                        "choices": [{"title": c} for c in plan["choices"]],
                        "duration": plan["duration"],
                        "channel_points_voting_enabled": False,
                    },
                    None,
                )
            else:
                route, method, params = "channels", "PATCH", {"broadcaster_id": uid}
                body = (
                    {"title": plan["text"]}
                    if k == "title"
                    else {"game_id": plan["category_id"]}
                )
        except (requests.RequestException, ValueError, KeyError):
            return {
                "state": "needs_attention",
                "detail": "Twitch precondition check failed; nothing sent.",
            }
        try:
            r = self.request(
                method, route, sender=is_chat, json=body, **({"params": params} if params else {})
            )
        except (requests.RequestException, ValueError):
            return {
                "state": "uncertain",
                "detail": "Write outcome unknown. Reconcile before any new write.",
            }
        if r.status_code >= 500:
            return {
                "state": "uncertain",
                "detail": f"Twitch HTTP {r.status_code}; outcome unknown. No automatic retry.",
            }
        if r.status_code not in (200, 204):
            return {
                "state": "needs_attention",
                "detail": f"Twitch HTTP {r.status_code}; action not confirmed. No fallback or automatic retry.",
            }
        if k in ("title", "category"):
            return {"state": "done", "detail": "Twitch confirmed the channel update."}
        try:
            records = r.json().get("data", [])
            record = records[0] if records else {}
            if not isinstance(record, dict):
                raise ValueError("Invalid action receipt")
        except (ValueError, TypeError, AttributeError):
            return {"state": "uncertain", "detail": "Twitch response was not a usable action receipt; no automatic retry."}
        if is_chat:
            if record.get("is_sent") is not True:
                return {
                    "state": "needs_attention",
                    "detail": "Message not delivered: "
                    + str((record.get("drop_reason") or {}).get("code", "unknown"))[
                        :100
                    ],
                }
            return {
                "state": "done",
                "detail": "Message delivered.",
                "remote_id": record.get("message_id"),
                "sender_id": self.sender_identity["user_id"],
                "sender_login": self.sender_identity["login"],
                "sender_mode": self.sender_mode,
                "message": body["message"],
            }
        if not record.get("id"):
            return {
                "state": "uncertain",
                "detail": "Twitch response did not establish an action identity.",
            }
        return {
            "state": "done",
            "detail": "Twitch confirmed " + k + ".",
            "remote_id": record["id"],
            **(
                {"position_seconds": record.get("position_seconds")}
                if k == "marker"
                else {}
            ),
        }

    def reconcile(self, row, plan):
        """Read-only reconciliation. Absence never authorizes another write."""
        self.refresh()
        uid, kind = self.identity["user_id"], plan["kind"]
        if kind in ("title", "category"):
            data = self.read("channels", {"broadcaster_id": uid})
            field, expected = (
                ("title", plan["text"])
                if kind == "title"
                else ("game_id", plan["category_id"])
            )
            if data and data[0].get(field) == expected:
                return {
                    "state": "done",
                    "detail": "Desired channel state verified; attribution to the timed-out write is uncertain.",
                }
        # Chat has no documented historical-message lookup; marker/poll time matching
        # without a returned ID is ambiguous. Keep uncertain, never infer absence.
        return {
            "state": "uncertain",
            "detail": "No conclusive delivery evidence. Inspect Twitch; this action will not resend.",
        }

    def poll_result(self, poll_id):
        self.refresh()
        rows = self.read(
            "polls", {"broadcaster_id": self.identity["user_id"], "id": poll_id}
        )
        if not rows or rows[0].get("status") != "COMPLETED":
            return None
        choices = rows[0].get("choices", [])
        counts = [c.get("votes", 0) for c in choices]
        best = max(counts, default=0)
        winners = [c["title"] for c in choices if c.get("votes", 0) == best]
        return {
            "id": poll_id,
            "line": "No votes received"
            if not best
            else "Vote tied"
            if len(winners) != 1
            else "Audience chose: " + winners[0],
            "choices": [
                {"title": c["title"], "votes": c.get("votes", 0)} for c in choices
            ],
        }

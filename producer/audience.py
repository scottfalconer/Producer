"""Official bounded EventSub inbox. No raw messages written to disk."""

import copy
import json
import threading
import time
from datetime import datetime
from urllib.parse import urlparse

import requests
from websockets.sync.client import connect
from twitch import authorization

EVENTS = (
    "channel.chat.message",
    "channel.chat.message_delete",
    "channel.chat.clear",
    "channel.chat.clear_user_messages",
)


class Audience:
    def __init__(self):
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.socket = None
        self.thread = None
        self.owner = None
        self.questions = []
        self.seen = {}
        self.status = "Audience input off."
        self.polled = 0
        self.channel = None
        self.vote = None
        self.connected = False
        self.selection = None

    def start(self, sid):
        self.stop()
        token, identity, headers = authorization()
        if "user:read:chat" not in identity.get("scopes", []):
            raise ValueError("The existing Twitch grant needs chat-read access.")
        self.owner = sid
        self.channel = identity["user_id"]
        self.stop_event = threading.Event()
        self.polled = time.monotonic()
        self.status = "Connecting to @" + identity["login"]
        self.thread = threading.Thread(
            target=self.run, args=(identity, headers), daemon=True
        )
        self.thread.start()
        return {"status": self.status}

    def stop(self):
        self.connected = False
        self.stop_event.set()
        if self.socket:
            self.socket.close()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=10)
            if self.thread.is_alive():
                raise ValueError(
                    "Audience connection is still closing. Try again shortly."
                )
        with self.lock:
            if self.vote:
                self.vote.clear()
                self.vote = None
            self.questions.clear()
            self.seen.clear()
            self.selection = None
            self.owner = None
            self.status = "Audience input off."

    def accept(self, frame, received=None):
        received = time.time() if received is None else received
        metadata = frame.get("metadata", {})
        payload = frame.get("payload", {})
        sub = payload.get("subscription", {})
        event = payload.get("event", {})
        kind = sub.get("type")
        if (
            kind not in EVENTS
            or sub.get("version") != "1"
            or event.get("broadcaster_user_id") != self.channel
        ):
            raise ValueError("Unexpected audience event.")
        ident = metadata.get("message_id")
        stamp = datetime.fromisoformat(
            metadata["message_timestamp"].replace("Z", "+00:00")
        ).timestamp()
        if not ident or stamp > received + 10 or received - stamp > 60:
            return
        with self.lock:
            self.seen = {k: v for k, v in self.seen.items() if received - v < 600}
            if ident in self.seen:
                return
            if len(self.seen) >= 10000:
                raise ValueError("Audience event limit reached.")
            self.seen[ident] = received
            if self.vote:
                self.vote.accept(kind, event, received)
            if kind == "channel.chat.clear":
                self.questions.clear()
                if self.selection:
                    self.selection["valid"] = False
            elif kind == "channel.chat.message_delete":
                if self.selection and self.selection["message_id"] == event.get(
                    "message_id"
                ):
                    self.selection["valid"] = False
                self.questions = [
                    q for q in self.questions if q["id"] != event.get("message_id")
                ]
            elif kind == "channel.chat.clear_user_messages":
                if self.selection and self.selection["user_id"] == event.get(
                    "target_user_id"
                ):
                    self.selection["valid"] = False
                self.questions = [
                    q
                    for q in self.questions
                    if q["user_id"] != event.get("target_user_id")
                ]
            else:
                message = event.get("message", {}).get("text")
                if (
                    isinstance(message, str)
                    and message.strip()
                    and event.get("message_id")
                    and event.get("chatter_user_id")
                ):
                    self.questions.append(
                        {
                            "id": event["message_id"],
                            "user_id": event["chatter_user_id"],
                            "author": str(event.get("chatter_user_name", "Viewer"))[
                                :100
                            ],
                            "text": message[:500],
                            "received": received,
                        }
                    )
                    self.questions = self.questions[-20:]

    def poll(self, sid):
        if sid != self.owner:
            raise ValueError("Audience input is not connected to this show.")
        self.polled = time.monotonic()
        with self.lock:
            self.questions = [
                q for q in self.questions if time.time() - q["received"] < 120
            ]
            return {
                "status": self.status,
                "questions": [
                    {k: v for k, v in q.items() if k != "user_id"}
                    for q in copy.deepcopy(self.questions)
                ],
                "selection": {
                    "message_id": self.selection["message_id"],
                    "valid": self.selection["valid"] and self.connected,
                }
                if self.selection
                else None,
            }

    def pin_question(self, sid, ident):
        """Retain only selected identity/moderation metadata beyond inbox age limits."""
        if sid != self.owner:
            raise ValueError("Audience input is not connected to this show.")
        if ident is None:
            return self.unpin_question(sid)
        with self.lock:
            question = next((q for q in self.questions if q["id"] == ident), None)
            if not question or not self.connected:
                return False
            self.selection = {
                "message_id": question["id"],
                "user_id": question["user_id"],
                "valid": True,
            }
            return True

    def unpin_question(self, sid):
        if sid != self.owner:
            raise ValueError("Audience input is not connected to this show.")
        with self.lock:
            self.selection = None
        return True

    def dismiss(self, sid, ident):
        if sid != self.owner:
            raise ValueError("Audience input is not connected to this show.")
        with self.lock:
            self.questions = [q for q in self.questions if q["id"] != ident]
            if self.selection and self.selection["message_id"] == ident:
                self.selection["valid"] = False

    def run(self, identity, headers):
        started = time.monotonic()
        endpoint = "wss://eventsub.wss.twitch.tv/ws?keepalive_timeout_seconds=30"
        migrating = False
        try:
            while not self.stop_event.is_set():
                socket = connect(
                    endpoint,
                    open_timeout=8,
                    close_timeout=1,
                    ping_interval=None,
                    proxy=None,
                    max_size=1048576,
                )
                old = self.socket
                self.socket = socket
                welcome = json.loads(socket.recv(timeout=8))
                if welcome.get("metadata", {}).get("message_type") != "session_welcome":
                    raise ValueError("No Twitch welcome.")
                session = welcome["payload"]["session"]
                keepalive = session.get("keepalive_timeout_seconds") or 30
                if migrating:
                    if old:
                        old.close()
                else:
                    for kind in EVENTS:
                        if self.stop_event.is_set():
                            return
                        r = requests.post(
                            "https://api.twitch.tv/helix/eventsub/subscriptions",
                            headers=headers,
                            json={
                                "type": kind,
                                "version": "1",
                                "condition": {
                                    "broadcaster_user_id": identity["user_id"],
                                    "user_id": identity["user_id"],
                                },
                                "transport": {
                                    "method": "websocket",
                                    "session_id": session["id"],
                                },
                            },
                            timeout=8,
                            allow_redirects=False,
                        )
                        if r.status_code != 202:
                            raise ValueError(
                                "Audience subscription unavailable (HTTP "
                                + str(r.status_code)
                                + ")."
                            )
                self.status = "Audience connected to @" + identity["login"]
                self.connected = True
                last = time.monotonic()
                while not self.stop_event.is_set():
                    if time.monotonic() - self.polled > 120:
                        raise ValueError(
                            "Audience input stopped after the monitor disconnected."
                        )
                    if time.monotonic() - started >= min(
                        3500, identity.get("expires_in", 3600) - 15
                    ):
                        raise ValueError(
                            "Reconnect audience input to renew its authorization check."
                        )
                    try:
                        frame = json.loads(socket.recv(timeout=1))
                    except TimeoutError:
                        if time.monotonic() - last > keepalive:
                            raise ValueError(
                                "Audience connection lost. Reconnect when ready."
                            )
                        continue
                    kind = frame.get("metadata", {}).get("message_type")
                    if kind in ("notification", "session_keepalive"):
                        last = time.monotonic()
                    if kind == "notification":
                        self.accept(frame)
                    elif kind == "session_reconnect":
                        endpoint = frame["payload"]["session"]["reconnect_url"]
                        parsed = urlparse(endpoint)
                        if (
                            parsed.scheme != "wss"
                            or parsed.hostname != "eventsub.wss.twitch.tv"
                            or parsed.path != "/ws"
                            or parsed.username
                            or parsed.password
                            or parsed.port
                            or parsed.fragment
                        ):
                            raise ValueError("Unexpected Twitch reconnect destination.")
                        migrating = True
                        self.connected = False
                        break
                    elif kind == "revocation":
                        raise ValueError(
                            "Twitch revoked audience access. Reconnect after checking your grant."
                        )
                    elif kind != "session_keepalive":
                        raise ValueError("Unexpected Twitch connection event.")
        except ValueError as exc:
            self.status = str(exc)
        except Exception:
            self.status = "Audience connection ended. Reconnect when ready."
        finally:
            self.connected = False
            if self.socket:
                self.socket.close()
            with self.lock:
                self.questions.clear()
                if self.selection:
                    self.selection["valid"] = False


audience = Audience()

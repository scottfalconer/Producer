"""Optional, read-only LAN display of the presenter's current cue.

This listener is separate from the loopback control server. Creating it is an
explicit host choice; the module never starts a server on import. Its fresh
pairing token authorizes one tiny state resource, not a proxy to the helper.
"""

import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import secrets
import threading
from urllib.parse import urlsplit


STATIC = Path(__file__).with_name("static")
ASSETS = {
    "/": ("phone-monitor.html", "text/html; charset=utf-8"),
    "/phone-monitor.css": ("phone-monitor.css", "text/css; charset=utf-8"),
    "/phone-monitor.js": ("phone-monitor.js", "text/javascript; charset=utf-8"),
}
KINDS = frozenset(("signal", "warning", "editorial", "audience", "question",
                   "timing", "recovery", "guidance"))
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(value) for value in
                         ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8"))


def phone_state(value):
    """Allowlist current display fields even if the callback adds other data."""
    result = {"confidence": None, "presence": "off"}
    if not isinstance(value, dict):
        return result
    presence = value.get("presence")
    if presence not in ("active", "paused"):
        return result
    result["presence"] = presence
    cue = value.get("confidence")
    if presence == "active" and isinstance(cue, dict):
        line, kind = cue.get("line"), cue.get("kind")
        # The director already validates this cue. A mirror must not silently
        # drop a full audience question using a second editorial length limit.
        if isinstance(line, str) and line.strip() and isinstance(kind, str) and kind in KINDS:
            result["confidence"] = {"line": line, "kind": kind}
    return result


class _PhoneHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 8

    def __init__(self, *args, **kwargs):
        self._clients = threading.BoundedSemaphore(16)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._clients.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._clients.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._clients.release()

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(3)
        return connection, address


class _PhoneHandler(BaseHTTPRequestHandler):
    server_version = "ProducerPhone"
    sys_version = ""

    def log_message(self, *_args):
        # Pairing material and cue text must not enter HTTP access logs.
        pass

    def _reply(self, code, body=b"", content_type="text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'self'; style-src 'self'; "
                         "connect-src 'self'; base-uri 'none'; form-action 'none'; "
                         "frame-ancestors 'none'")
        self.send_header("Permissions-Policy",
                         "camera=(), microphone=(), geolocation=(), display-capture=()")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD":
            self.wfile.write(body)

    def _valid_request(self):
        host, port = self.server.server_address
        allowed_hosts = {f"{host}:{port}"}
        if port == 80:
            allowed_hosts.add(host)
        if self.headers.get("Host") not in allowed_hosts:
            self._reply(403)
            return False
        if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Length", "0") != "0":
            self._reply(400)
            return False
        if self.server.monitor_closed.is_set():
            self._reply(503)
            return False
        return True

    def do_GET(self):
        if not self._valid_request():
            return
        try:
            target = urlsplit(self.path)
        except ValueError:
            self._reply(404)
            return
        if target.query or target.fragment or target.scheme or target.netloc:
            self._reply(404)
            return
        if target.path in ASSETS:
            filename, mime = ASSETS[target.path]
            self._reply(200, (STATIC / filename).read_bytes(), mime)
            return
        if target.path != "/api/presenter":
            self._reply(404)
            return
        authorization = self.headers.get("Authorization", "")
        expected = "Bearer " + self.server.pairing_token
        if not authorization.isascii() or len(authorization) != len(expected) or not hmac.compare_digest(authorization, expected):
            self._reply(401)
            return
        try:
            state = phone_state(self.server.snapshot_callback())
        except Exception:
            # A stopped/failed source is quiet; do not expose exception details.
            state = phone_state(None)
        if self.server.monitor_closed.is_set():
            self._reply(503)
            return
        self._reply(200, json.dumps(state, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8")

    def _reject_write(self):
        self._reply(405)

    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = _reject_write


class PhoneMonitor:
    """An explicitly started display; closing it revokes its ephemeral link."""

    def __init__(self, bind, port, snapshot_callback):
        try:
            address = ipaddress.IPv4Address(bind)
        except (ipaddress.AddressValueError, TypeError) as exc:
            raise ValueError("Choose this Mac's explicit private IPv4 address.") from exc
        if not any(address in network for network in PRIVATE_NETWORKS) or address.is_unspecified:
            raise ValueError("Phone display must bind to a private IPv4 address, not every interface.")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("Choose a valid phone display port.")
        if not callable(snapshot_callback):
            raise ValueError("A presenter snapshot callback is required.")
        self._http = _PhoneHTTPServer((str(address), port), _PhoneHandler)
        self._http.snapshot_callback = snapshot_callback
        self._http.pairing_token = secrets.token_urlsafe(32)
        self._http.monitor_closed = threading.Event()
        self._thread = threading.Thread(target=self._http.serve_forever,
                                        kwargs={"poll_interval": 0.1},
                                        name="producer-phone-monitor", daemon=True)
        self._thread.start()

    @property
    def address(self):
        return self._http.server_address

    @property
    def pairing_url(self):
        if self._http.monitor_closed.is_set():
            return None
        host, port = self.address
        return f"http://{host}:{port}/#" + self._http.pairing_token

    def close(self):
        if self._http.monitor_closed.is_set():
            return
        self._http.monitor_closed.set()
        self._http.pairing_token = ""
        self._http.shutdown()
        self._http.server_close()
        self._thread.join(timeout=2)


def start_phone_monitor(bind, port, snapshot_callback):
    return PhoneMonitor(bind, port, snapshot_callback)

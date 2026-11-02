"""Read-only phone display authority; loopback fixtures, no real show/media."""

from contextlib import redirect_stderr
import io
import json
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from phone_monitor import phone_state, start_phone_monitor


class PhoneStateTests(unittest.TestCase):
    def test_allowlist_drops_private_material_and_extra_cue_fields(self):
        result = phone_state({"presence": "active", "confidence": {
            "line": "Ask for a concrete example.", "kind": "editorial", "secret": "private"},
            "brief": "private", "transcript": "private", "id": "session", "token": "secret"})
        self.assertEqual(result, {"presence": "active", "confidence": {
            "line": "Ask for a concrete example.", "kind": "editorial"}})

    def test_off_paused_and_invalid_state_cannot_keep_a_cue(self):
        for value in (None, [], {"presence": "off"}, {"presence": "invalid"},
                      {"presence": "paused", "confidence": {"line": "stale", "kind": "editorial"}},
                      {"presence": "active", "confidence": {"line": "private", "kind": "unknown"}},
                      {"presence": "active", "confidence": {"line": "private", "kind": []}}):
            self.assertIsNone(phone_state(value)["confidence"])

    def test_preserves_question_body_without_summarizing(self):
        body = ("Can you show the code?\nThe text is too small here. "
                "I can see the first couple of lines, but I can't make out "
                "the example in the lower-right corner. Could you enlarge "
                "that section before you explain how the two versions differ? "
                "I'm following along on my phone.")
        self.assertGreater(len(body), 240)
        self.assertEqual(phone_state({"presence": "active", "confidence": {
            "kind": "question", "line": body}})["confidence"]["line"], body)


class PhoneHTTPTests(unittest.TestCase):
    def setUp(self):
        self.calls = 0
        self.current = {"confidence": {"line": "Return to the example", "kind": "editorial"},
                        "presence": "active", "private": "never-send"}

        def snapshot():
            self.calls += 1
            return self.current

        self.monitor = start_phone_monitor("127.0.0.1", 0, snapshot)
        self.base, self.token = self.monitor.pairing_url.split("#")
        self.base = self.base.rstrip("/")

    def tearDown(self):
        self.monitor.close()

    def request(self, path="/api/presenter", *, token=None, method="GET", headers=None):
        headers = dict(headers or {})
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        try:
            response = urlopen(Request(self.base + path, headers=headers, method=method), timeout=3)
        except HTTPError as response:
            return response.code, response.headers, response.read()
        with response:
            return response.status, response.headers, response.read()

    def test_no_or_wrong_token_does_not_read_the_callback(self):
        for token in (None, "wrong", "x" * 10000, self.token + " ", "é" * len(self.token)):
            code, headers, body = self.request(token=token)
            self.assertEqual(code, 401)
            self.assertEqual(body, b"")
            self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(self.calls, 0)

    def test_valid_token_reads_only_current_display_and_never_caches(self):
        code, headers, body = self.request(token=self.token)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body), phone_state(self.current))
        self.assertNotIn(b"never-send", body)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.current = {"presence": "off", "confidence": {"line": "stale", "kind": "editorial"}}
        self.assertIsNone(json.loads(self.request(token=self.token)[2])["confidence"])

    def test_static_shell_contains_no_token_or_show_data(self):
        for path in ("/", "/phone-monitor.js", "/phone-monitor.css"):
            code, headers, body = self.request(path)
            self.assertEqual(code, 200)
            self.assertNotIn(self.token.encode(), body)
            self.assertNotIn(b"never-send", body)
            self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertEqual(self.calls, 0)

    def test_no_control_proxy_write_cors_or_traversal_routes(self):
        for path in ("/api/start", "/api/stop", "/api/room", "/api/on-air", "/api/replay",
                     "/../server.py", "/%2e%2e/server.py", "/api/presenter?token=" + self.token):
            self.assertEqual(self.request(path, token=self.token)[0], 404)
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"):
            self.assertEqual(self.request(token=self.token, method=method)[0], 405)
        self.assertEqual(self.calls, 0)

    def test_host_header_and_request_body_are_rejected(self):
        self.assertEqual(self.request(token=self.token, headers={"Host": "attacker.example"})[0], 403)
        self.assertEqual(self.request(token=self.token, headers={"Content-Length": "1"})[0], 400)
        self.assertEqual(self.request(token=self.token, headers={"Transfer-Encoding": "chunked"})[0], 400)
        self.assertEqual(self.calls, 0)

    def test_default_http_port_accepts_browser_host_header(self):
        original = self.monitor._http.server_address
        try:
            self.monitor._http.server_address = (original[0], 80)
            self.assertEqual(self.request(token=self.token, headers={"Host": original[0]})[0], 200)
            self.assertEqual(self.request(token=self.token, headers={"Host": original[0] + ":80"})[0], 200)
            self.assertEqual(self.request(token=self.token, headers={"Host": "attacker.example"})[0], 403)
        finally:
            self.monitor._http.server_address = original

    def test_callback_failure_goes_quiet_without_leaking_error(self):
        def broken():
            raise RuntimeError("private-path-secret")
        self.monitor._http.snapshot_callback = broken
        self.assertEqual(json.loads(self.request(token=self.token)[2]), phone_state(None))

    def test_access_logs_never_print_pairing_material(self):
        output = io.StringIO()
        with redirect_stderr(output):
            self.request(token=self.token)
            self.request("/" + self.token)
        self.assertEqual(output.getvalue(), "")

    def test_fresh_listener_has_different_token_and_close_revokes_link(self):
        other = start_phone_monitor("127.0.0.1", 0, lambda: self.current)
        try:
            self.assertNotEqual(other.pairing_url.split("#")[1], self.token)
            self.assertEqual(len(self.token), 43)
        finally:
            other.close()
        self.assertIsNone(other.pairing_url)
        other.close()

    def test_explicit_private_ipv4_only(self):
        for bind in ("0.0.0.0", "::", "localhost", "8.8.8.8", "224.0.0.1", "169.254.1.2"):
            with self.assertRaises(ValueError):
                start_phone_monitor(bind, 0, lambda: self.current)


if __name__ == "__main__":
    unittest.main()

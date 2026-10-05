import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch, Mock
import websocket
from market_making.execution import websocket_order_session as ws
from market_making.market_data.check_connection import PrimaryError
from test_order_reports import report


class Clock:
    def __init__(self):
        self.now = 0
    def __call__(self):
        return self.now
    def sleep(self, value):
        self.now += value


class Sink:
    def __init__(self):
        self.events = []
        self.closed = False
    def write(self, event):
        self.events.append(event)
    def close(self):
        self.closed = True


class Socket:
    def __init__(self, clock, frames=()):
        self.clock = clock
        self.frames = list(frames)
        self.sent = []
        self.closed = False
        self.timeouts = []
        self.pings = 0
    def settimeout(self, value):
        self.timeout = value
        self.timeouts.append(value)
    def send(self, frame):
        self.sent.append(json.loads(frame))
    def ping(self):
        self.pings += 1
    def recv_data(self, control_frame):
        self.clock.sleep(min(self.timeout, .5))
        if self.frames:
            item = self.frames.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        raise websocket.WebSocketTimeoutException()
    def close(self):
        self.closed = True


def text(message):
    return websocket.ABNF.OPCODE_TEXT, json.dumps(message)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.sink = Sink()
        self.http = Mock()
        self.auth = Mock(return_value="TOKEN_SECRET")

    def run_session(self, sockets, **kwargs):
        iterator = iter(sockets)
        def connect(token, timeout):
            sock = next(iterator)
            if isinstance(sock, Exception):
                raise sock
            return sock
        return ws.run_session("DEMO_ACCOUNT", self.sink, "USER_SECRET", "PASS_SECRET",
            duration=kwargs.pop("duration", 3), interval=kwargs.pop("interval", 1),
            reconnects=kwargs.pop("reconnects", 2), clock=self.clock,
            sleep=self.clock.sleep, connect_fn=connect, authenticate_fn=self.auth,
            session_factory=lambda: self.http, **kwargs)

    def test_os_only_report_and_cleanup(self):
        sock = Socket(self.clock, [text(report(cumQty=20))])
        result = self.run_session([sock])
        self.assertEqual(sock.sent, [{"type": "os", "account": {"id": "DEMO_ACCOUNT"}}])
        self.assertEqual(result["qualifying_reports"], 1)
        self.assertEqual(result["orders_sent"], 0)
        self.assertEqual(result["readiness"], "unverified")
        self.assertTrue(result["fill_observed"])
        self.assertTrue(sock.closed)
        self.assertTrue(self.sink.closed)
        self.http.close.assert_called_once()
        self.assertFalse(self.http.trust_env)
        self.assertLessEqual(max(sock.timeouts), 1)
        for secret in ("DEMO_ACCOUNT", "TOKEN_SECRET", "USER_SECRET", "PASS_SECRET"):
            self.assertNotIn(secret, json.dumps(self.sink.events))

    def test_silence_and_heartbeats_never_readiness(self):
        for frames in ([], [(websocket.ABNF.OPCODE_PONG, b""), text({"type": "heartbeat"})]):
            with self.subTest(frames=frames):
                self.setUp()
                result = self.run_session([Socket(self.clock, frames)])
                self.assertEqual(result["qualifying_reports"], 0)
                self.assertEqual(result["readiness"], "unverified")
                self.assertEqual(result["continuity"], "unverified")

    def test_invalid_json_binary_and_account_gap(self):
        sock = Socket(self.clock, [(1, b"\xff"), (1, '{"x":NaN}'), (2, b"abc"),
                                  text(report(accountId={"id": "OTHER"})), text(report(cumQty=20))])
        result = self.run_session([sock])
        for gap in ("INVALID_JSON", "UNSUPPORTED_FRAME", "UNCORRELATED_REPORT"):
            self.assertIn(gap, result["gaps"])
        self.assertTrue(result["fill_observed"])

    def test_reconnect_preserves_fill_and_gaps(self):
        first = Socket(self.clock, [text(report(cumQty=25)), (8, b"")])
        second = Socket(self.clock, [text(report(status="CANCELLED", cumQty=0))])
        result = self.run_session([first, second], duration=5)
        self.assertEqual(result["generation"], 2)
        self.assertIn("CONNECTION_GENERATION_GAP", result["gaps"])
        self.assertIn("DISCONNECTION_GAP", result["gaps"])
        self.assertTrue(result["fill_observed"])
        self.assertTrue(first.closed and second.closed)
        self.assertEqual(first.sent, second.sent)

    def test_token_renewal_reconnects_and_remains_unverified(self):
        sockets = [Socket(self.clock, [text(report(cumQty=25))]), Socket(self.clock), Socket(self.clock)]
        self.auth.side_effect = ["FIRST_SECRET", "SECOND_SECRET", "THIRD_SECRET"]
        result = self.run_session(sockets, duration=3, refresh_after=1)
        self.assertEqual(self.auth.call_count, 3)
        self.assertIn("TOKEN_RENEWAL_GAP", result["gaps"])
        self.assertTrue(result["fill_observed"])
        self.assertTrue(all(sock.closed for sock in sockets))

    def test_server_errors_and_auth_are_terminal(self):
        for message in ({"type": "error", "text": "TOKEN_SECRET"},
                        {"status": 401, "text": "PASS_SECRET"},
                        {"message": "invalid token TOKEN_SECRET"}):
            self.setUp()
            sock = Socket(self.clock, [text(message)])
            with self.assertRaises(PrimaryError):
                self.run_session([sock])
            self.assertTrue(sock.closed and self.sink.closed)
            self.assertEqual(self.auth.call_count, 1)
            self.assertNotIn("TOKEN_SECRET", json.dumps(self.sink.events))

    def test_non101_and_auth_failure_no_retry(self):
        for failure in (PrimaryError("upgrade rejected"),
                        websocket.WebSocketBadStatusException("secret", 302)):
            self.setUp()
            with self.assertRaises(PrimaryError):
                self.run_session([failure])
            self.assertTrue(self.sink.closed)
            self.assertEqual(self.auth.call_count, 1)
        self.setUp()
        self.auth.side_effect = RuntimeError("TOKEN_SECRET")
        with self.assertRaisesRegex(PrimaryError, "readiness unverified"):
            self.run_session([])
        self.assertTrue(self.sink.closed)

    def test_interrupt_closes_everything_and_preserves_evidence(self):
        sock = Socket(self.clock, [text(report(cumQty=25)), KeyboardInterrupt()])
        with self.assertRaises(KeyboardInterrupt):
            self.run_session([sock])
        self.assertTrue(sock.closed and self.sink.closed)
        self.assertEqual(self.sink.events[-1]["event"], "interrupted")
        self.assertTrue(self.sink.events[-1]["fill_observed"])

    def test_sink_failure_terminal_no_reconnect_or_repeated_write(self):
        original = self.sink.write
        calls = []
        def fail(event):
            calls.append(event["event"])
            if event["event"] == "report_observed":
                raise OSError("TOKEN_SECRET")
            original(event)
        self.sink.write = fail
        sock = Socket(self.clock, [text(report(cumQty=25))])
        with self.assertRaises(PrimaryError):
            self.run_session([sock])
        self.assertTrue(sock.closed and self.sink.closed)
        self.assertEqual(calls, ["session", "connected", "subscription_sent", "report_observed"])
        self.assertEqual(self.auth.call_count, 1)

    def test_reconnect_budget_and_backoff_bounded(self):
        sockets = [Socket(self.clock, [(8, b"")]) for _ in range(3)]
        with self.assertRaises(PrimaryError):
            self.run_session(sockets, duration=20, reconnects=2)
        self.assertLess(self.clock(), 20)
        self.assertTrue(all(sock.closed for sock in sockets))

    def test_invalid_bounds_no_auth_and_cleanup(self):
        for changes in ({"duration": float("nan")}, {"duration": 0}, {"duration": 3601},
                        {"interval": 31}, {"reconnects": -1}, {"refresh_after": 0}):
            self.setUp()
            with self.assertRaises(PrimaryError):
                self.run_session([], **changes)
            self.auth.assert_not_called()
            self.assertTrue(self.sink.closed)

    def test_connect_security_options_and_non101_close(self):
        sock = Mock(handshake_response=SimpleNamespace(status=101))
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://hostile:99"}), patch.object(ws.websocket, "create_connection", return_value=sock) as create:
            self.assertIs(ws.connect("TOKEN_SECRET", 1), sock)
            kwargs = create.call_args.kwargs
            self.assertEqual(create.call_args.args, (ws.WS_URL,))
            self.assertEqual(kwargs["http_no_proxy"], ["*"])
            self.assertEqual(kwargs["redirect_limit"], 0)
            from websocket._url import get_proxy_info
            self.assertEqual(get_proxy_info("api.remarkets.primary.com.ar", True,
                                           no_proxy=kwargs["http_no_proxy"]), (None, 0, None))
        sock.handshake_response.status = 302
        with patch.object(ws.websocket, "create_connection", return_value=sock), self.assertRaises(PrimaryError):
            ws.connect("TOKEN_SECRET", 1)
        sock.close.assert_called_once()

    def test_http_only_auth_fixed_host_no_proxy_redirect(self):
        response = Mock(status_code=200, headers={"X-Auth-Token": "TOKEN_SECRET"})
        self.http.post.return_value = response
        self.assertEqual(ws.authenticate(self.http, "user", "password", 2), "TOKEN_SECRET")
        self.assertFalse(self.http.trust_env)
        self.http.post.assert_called_once_with(ws.BASE_URL + "/auth/getToken", timeout=2,
            allow_redirects=False, headers={"X-Username": "user", "X-Password": "password"})
        response.close.assert_called_once()
        self.http.request.assert_not_called()
        self.http.get.assert_not_called()

    def test_malformed_frame_after_fill_cannot_erase_evidence(self):
        sock = Socket(self.clock, [text(report(cumQty=20)), (1, '{"x":1,"x":2}'),
                                  text(report(cumQty=0, status="CANCELLED"))])
        result = self.run_session([sock])
        self.assertTrue(result["fill_observed"])
        self.assertIn("INVALID_JSON", result["gaps"])
        self.assertIn("REPORT_ANOMALY", result["gaps"])

    def test_sink_failure_before_connect_no_auth(self):
        self.sink.write = Mock(side_effect=OSError("SECRET"))
        with self.assertRaises(PrimaryError):
            self.run_session([])
        self.auth.assert_not_called()
        self.assertTrue(self.sink.closed)
        self.sink.write.assert_called_once()

    def test_cleanup_failure_is_terminal(self):
        sock = Socket(self.clock, [text(report())])
        sock.close = Mock(side_effect=OSError("SECRET"))
        with self.assertRaises(PrimaryError):
            self.run_session([sock])
        self.assertTrue(self.sink.closed)
        self.assertIn("SOCKET_CLOSE_FAILED", self.sink.events[-1]["gaps"])
        self.setUp()
        self.sink.close = Mock(side_effect=OSError("SECRET"))
        sock = Socket(self.clock, [text(report())])
        with self.assertRaisesRegex(PrimaryError, "cleanup failed"):
            self.run_session([sock])
        self.assertTrue(sock.closed)

    def test_frame_budget_bounds_nonadvancing_transport(self):
        sock = Socket(self.clock)
        sock.recv_data = lambda control_frame: (websocket.ABNF.OPCODE_PONG, b"")
        with self.assertRaises(PrimaryError):
            self.run_session([sock])
        self.assertTrue(sock.closed and self.sink.closed)
        self.assertIn("FRAME_BUDGET_EXHAUSTED", self.sink.events[-1]["gaps"])

    def test_connection_budget_includes_rapid_renewals(self):
        sockets = [Socket(self.clock) for _ in range(100)]
        for sock in sockets:
            def recv(control_frame):
                self.clock.sleep(.0001)
                raise websocket.WebSocketTimeoutException()
            sock.recv_data = recv
        with self.assertRaises(PrimaryError):
            self.run_session(sockets, refresh_after=.0001)
        self.assertTrue(all(sock.closed for sock in sockets))
        self.assertIn("CONNECTION_BUDGET_EXHAUSTED", self.sink.events[-1]["gaps"])

    def test_upgrade_and_http_auth_failure_secrets_not_printed(self):
        for status in (301, 401, 403, 500):
            response = Mock(status_code=status, headers={"X-Auth-Token": "TOKEN_SECRET"})
            self.http.post.return_value = response
            with self.assertRaises(PrimaryError):
                ws.authenticate(self.http, "USER_SECRET", "PASS_SECRET", 1)
            response.close.assert_called_once()

    def test_report_envelope_auth_phrases_do_not_suppress_fill(self):
        for key in ("text", "message", "description", "error"):
            for phrase in ("invalid token", "authentication failed", "access denied"):
                with self.subTest(key=key, phrase=phrase):
                    self.setUp()
                    message = report(cumQty=20)
                    message[key] = phrase + " TOKEN_SECRET PASS_SECRET"
                    sock = Socket(self.clock, [text(message)])
                    result = self.run_session([sock])
                    self.assertTrue(result["fill_observed"])
                    self.assertEqual(result["qualifying_reports"], 1)
                    self.assertTrue(sock.closed and self.sink.closed)
                    serialized = json.dumps(self.sink.events)
                    self.assertNotIn(phrase, serialized)
                    self.assertNotIn("TOKEN_SECRET", serialized)
                    self.assertNotIn("PASS_SECRET", serialized)
                    self.assertNotIn("AUTHENTICATION_REJECTED", result["gaps"])

    def test_genuine_error_envelopes_terminal_after_preserving_prior_fill(self):
        for error in ({"type": "error", "text": "invalid token TOKEN_SECRET"},
                      {"type": "error", "description": "server failure PASS_SECRET"},
                      {"code": 401, "message": "TOKEN_SECRET"},
                      {"status": "ERROR", "text": "PASS_SECRET"},
                      {"message": "authentication failed TOKEN_SECRET"}):
            with self.subTest(error=error):
                self.setUp()
                sock = Socket(self.clock, [text(report(cumQty=20)), text(error)])
                with self.assertRaises(PrimaryError):
                    self.run_session([sock])
                self.assertTrue(sock.closed and self.sink.closed)
                self.assertTrue(self.sink.events[-1]["fill_observed"])
                self.assertEqual(self.auth.call_count, 1)
                self.assertNotIn("TOKEN_SECRET", json.dumps(self.sink.events))
                self.assertNotIn("PASS_SECRET", json.dumps(self.sink.events))

    def test_report_metadata_then_genuine_auth_failure_still_sticky(self):
        message = report(cumQty=20)
        message["message"] = "invalid token TOKEN_SECRET"
        sock = Socket(self.clock, [text(message), text({"status": 401})])
        with self.assertRaises(PrimaryError):
            self.run_session([sock])
        self.assertTrue(self.sink.events[-1]["fill_observed"])
        self.assertIn("AUTHENTICATION_REJECTED", self.sink.events[-1]["gaps"])

    def test_extreme_optional_report_numbers_do_not_terminate_or_erase_fill(self):
        # Includes the exact reviewer reproduction, without extra required fields.
        reproduction = ('{"type":"or","orderReport":{"accountId":{"id":"DEMO_ACCOUNT"},'
                        '"orderId":"one","status":"PARTIALLY_FILLED","cumQty":20,'
                        '"avgPx":1e9999999999999999999}}')
        for raw in (reproduction,
                    reproduction[:-1] + ',"metadata":[1e9999999999999999999]}',
                    reproduction.replace('"avgPx":1e9999999999999999999',
                                         '"nested":{"details":[1e-9999999999999999999]}')):
            with self.subTest(raw=raw):
                self.setUp()
                sock = Socket(self.clock, [(1, raw), text(report(status="CANCELLED", cumQty=0))])
                result = self.run_session([sock])
                self.assertTrue(result["fill_observed"])
                self.assertGreaterEqual(result["qualifying_reports"], 1)
                observed = [e for e in self.sink.events if e["event"] == "report_observed"]
                self.assertTrue(observed[0]["report_fill_observed"])
                self.assertIn("UNSUPPORTED_JSON_NUMBER", observed[0]["anomalies"])
                self.assertEqual(self.sink.events[-1]["event"], "ended")
                self.assertTrue(sock.closed and self.sink.closed)
                self.assertNotIn("9999999999999999999", json.dumps(self.sink.events))

    def test_unsupported_core_numeric_token_does_not_invent_session_fill(self):
        raw = ('{"type":"or","orderReport":{"accountId":{"id":"DEMO_ACCOUNT"},'
               '"orderId":"one","status":"NEW","cumQty":1e9999999999999999999}}')
        sock = Socket(self.clock, [(1, raw)])
        result = self.run_session([sock])
        self.assertFalse(result["fill_observed"])
        self.assertEqual(result["qualifying_reports"], 0)
        observed = next(e for e in self.sink.events if e["event"] == "report_observed")
        self.assertIn("UNSUPPORTED_cumQty", observed["anomalies"])
        self.assertNotIn("cumQty", observed["quantities"])
        self.assertIn("REPORT_ANOMALY", result["gaps"])


if __name__ == "__main__":
    unittest.main()

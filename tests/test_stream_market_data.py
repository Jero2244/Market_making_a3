import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stderr
from io import StringIO

import websocket

import market_making.market_data.stream_market_data as md


SYMBOL = "RFX20/OCT26"


class Clock:
    def __init__(self):
        self.value = 0

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class Socket:
    def __init__(self, clock, frames):
        self.clock = clock
        self.frames = iter(frames)
        self.sent = []
        self.closed = False

    def settimeout(self, value):
        self.timeout = value

    def send(self, text):
        self.sent.append(json.loads(text))

    def ping(self):
        pass

    def recv_data(self, control_frame=False):
        self.clock.value += 1
        try:
            frame = next(self.frames)
        except StopIteration:
            raise websocket.WebSocketTimeoutException()
        if isinstance(frame, Exception):
            raise frame
        return frame

    def close(self):
        self.closed = True


class StreamTests(unittest.TestCase):
    def test_nonstandard_constants_never_persist_or_prove_selected_data(self):
        from test_websocket_session import nonstandard_frames
        for text in nonstandard_frames():
            with self.subTest(text=text), tempfile.TemporaryDirectory() as folder:
                clock = Clock()
                sock = Socket(clock, [(websocket.ABNF.OPCODE_TEXT, text)])
                output = Path(folder) / "new.jsonl"
                with patch.object(md, "authenticate", side_effect=lambda s, u, p: s.headers.update({"X-Auth-Token": "SECRET"})), \
                     patch.object(md, "get_json", return_value={"instruments": [{"instrumentId": {"marketId": "ROFX", "symbol": SYMBOL}, "cficode": "FXXXSX"}]}):
                    md.run(SYMBOL, "ROFX", output, "user", "password", duration=2,
                           connect_fn=lambda token, timeout: sock, clock=clock, sleep=clock.sleep)
                saved = output.read_text()
                rows = [json.loads(line) for line in saved.splitlines()]
                kinds = [row["event"] for row in rows]
                self.assertNotIn("message", kinds)
                self.assertNotIn("selected_instrument_observed", kinds)
                self.assertEqual(kinds.count("control_message"), 1)
                self.assertFalse(any("raw" in row for row in rows))
                self.assertNotIn(text, saved)
                self.assertTrue(sock.closed)

    def test_invalid_utf8_is_withheld_and_recorder_remains_durable(self):
        clock = Clock()
        raw = '{"type":"Md","instrumentId":{"marketId":"ROFX","symbol":"RFX20/OCT26"},"marketData":{"BI":[]}}'
        sock = Socket(clock, [(websocket.ABNF.OPCODE_TEXT, b"\xff"),
                              (websocket.ABNF.OPCODE_TEXT, raw)])
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "new.jsonl"
            with patch.object(md, "authenticate", side_effect=lambda s, u, p: s.headers.update({"X-Auth-Token": "SECRET"})), \
                 patch.object(md, "get_json", return_value={"instruments": [{"instrumentId": {"marketId": "ROFX", "symbol": SYMBOL}, "cficode": "FXXXSX"}]}):
                md.run(SYMBOL, "ROFX", output, "user", "password", duration=3,
                       connect_fn=lambda token, timeout: sock, clock=clock, sleep=clock.sleep)
            rows = [json.loads(line) for line in output.read_text().splitlines()]
            self.assertEqual(sum(r["event"] == "control_message" for r in rows), 1)
            self.assertEqual([r["raw"] for r in rows if r["event"] == "message"], [raw])
            self.assertEqual(rows[-1]["event"], "ended")
            self.assertTrue(sock.closed)
            self.assertEqual([message["type"] for message in sock.sent], ["smd"])

    def test_validation(self):
        catalog = [{"instrumentId": {"marketId": "ROFX", "symbol": SYMBOL}, "cficode": "FXXXSX"}]
        md.validate_symbol(catalog, "ROFX", SYMBOL)
        for item in ([], [{**catalog[0], "cficode": "OCAFXS"}], catalog * 2):
            with self.assertRaises(md.PrimaryError):
                md.validate_symbol(item, "ROFX", SYMBOL)
        with self.assertRaises(md.PrimaryError):
            md.validate_symbol(catalog, "ROFX", "RFX20/OTHER")

    def test_freshness_independent_missing_empty_stale_reconnect(self):
        f = md.Freshness(3)
        f.reset()
        f.observe({"type": "Md", "instrumentId": {"marketId": "ROFX", "symbol": "OTHER"},
                   "marketData": {"BI": [{"price": 1}]}}, 1, "ROFX", SYMBOL)
        self.assertEqual(f.snapshot(1)["instrument"]["state"], "missing")
        f.observe({"type": "Md", "instrumentId": {"marketId": "ROFX", "symbol": SYMBOL},
                   "marketData": {"BI": [], "LA": {"price": 1}}}, 2, "ROFX", SYMBOL)
        self.assertEqual(f.snapshot(2)["entries"]["BI"]["state"], "empty")
        self.assertEqual(f.snapshot(2)["entries"]["OF"]["state"], "missing")
        f.observe({"type": "Md", "instrumentId": {"marketId": "ROFX", "symbol": SYMBOL},
                   "marketData": {"OF": [{"price": 2}]}}, 6, "ROFX", SYMBOL)
        self.assertEqual(f.snapshot(6)["entries"]["BI"]["state"], "stale")
        self.assertEqual(f.snapshot(6)["entries"]["OF"]["state"], "fresh")
        f.reset()
        self.assertEqual(f.snapshot(7)["entries"]["OF"]["state"], "missing")

    def test_record_reconnect_resubscribe_and_raw_timestamps(self):
        clock = Clock()
        raw = json.dumps({"type": "Md", "instrumentId": {"marketId": "ROFX", "symbol": SYMBOL},
                          "marketData": {"BI": [{"price": 5, "date": "server-date"}]}})
        sockets = [Socket(clock, [(websocket.ABNF.OPCODE_TEXT, raw), ConnectionError("drop")]),
                   Socket(clock, [(websocket.ABNF.OPCODE_TEXT, raw)])]
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "session.jsonl"
            with patch.object(md, "authenticate", side_effect=lambda s, u, p: s.headers.update({"X-Auth-Token": "SECRET"})), \
                 patch.object(md, "get_json", return_value={"instruments": [{"instrumentId": {"marketId": "ROFX", "symbol": SYMBOL}, "cficode": "FXXXSX"}]}):
                md.run(SYMBOL, "ROFX", output, "user", "password", duration=6, stale_after=10,
                       interval=2, reconnects=1, connect_fn=lambda token, timeout: sockets.pop(0),
                       clock=clock, sleep=clock.sleep)
            rows = [json.loads(line) for line in output.read_text().splitlines()]
            self.assertEqual(len([r for r in rows if r["event"] == "subscription_sent"]), 2)
            self.assertEqual(len([r for r in rows if r["event"] == "selected_instrument_observed"]), 2)
            self.assertEqual(len([r for r in rows if r["event"] == "message"]), 2)
            self.assertEqual([r for r in rows if r["event"] == "message"][0]["raw"], raw)
            self.assertEqual([r for r in rows if r["event"] == "message"][0]["server_timestamps"],
                             {"marketData.BI[0].date": "server-date"})
            self.assertNotIn("SECRET", output.read_text())
            with self.assertRaises(FileExistsError):
                md.Recorder(output)

    def test_auth_rejection_and_write_error(self):
        self.assertTrue(md.auth_rejected({"status": 401}))
        self.assertTrue(md.auth_rejected({"error": "Invalid token"}))
        self.assertTrue(md.auth_rejected({"type": "Md", "status": 401}))
        with tempfile.TemporaryDirectory() as folder:
            recorder = md.Recorder(Path(folder) / "new.jsonl")
            recorder.file.close()
            with self.assertRaisesRegex(md.PrimaryError, "Recording write failed"):
                recorder.write({"event": "message"})

    def test_proactive_renewal_does_not_use_retry_budget(self):
        clock = Clock()
        sockets = []
        logins = []

        def login(session, user, password):
            logins.append(clock())
            session.headers["X-Auth-Token"] = "token" + str(len(logins))

        def connector(token, timeout):
            sock = Socket(clock, [])
            sockets.append((token, sock))
            return sock

        with tempfile.TemporaryDirectory() as folder:
            with patch.object(md, "authenticate", side_effect=login), \
                 patch.object(md, "get_json", return_value={"instruments": [{"instrumentId": {"marketId": "ROFX", "symbol": SYMBOL}, "cficode": "FXXXSX"}]}):
                md.run(SYMBOL, "ROFX", Path(folder) / "session.jsonl", "u", "p", duration=5,
                       stale_after=20, interval=2, refresh_after=2, reconnects=0,
                       connect_fn=connector, clock=clock, sleep=clock.sleep)
            self.assertGreaterEqual(len(sockets), 2)
            self.assertEqual(sockets[0][0], "token1")
            self.assertEqual(sockets[1][0], "token2")
            self.assertTrue(all(s.closed for _, s in sockets))

    def test_auth_rejection_stops_without_reconnect(self):
        clock = Clock()
        rejection = json.dumps({"status": 401, "error": "invalid token"})
        connections = []

        def connector(token, timeout):
            connections.append(Socket(clock, [(websocket.ABNF.OPCODE_TEXT, rejection)]))
            return connections[-1]

        with tempfile.TemporaryDirectory() as folder:
            with patch.object(md, "authenticate", side_effect=lambda s, u, p: s.headers.update({"X-Auth-Token": "secret"})), \
                 patch.object(md, "get_json", return_value={"instruments": [{"instrumentId": {"marketId": "ROFX", "symbol": SYMBOL}, "cficode": "FXXXSX"}]}):
                with self.assertRaises(md.PrimaryError):
                    md.run(SYMBOL, "ROFX", Path(folder) / "session.jsonl", "u", "p", duration=5,
                           stale_after=20, reconnects=5, connect_fn=connector, clock=clock, sleep=clock.sleep)
            self.assertEqual(len(connections), 1)
            self.assertTrue(connections[0].closed)

    def test_redirect_denied_and_only_one_token_handshake(self):
        class RedirectSocket:
            handshake_response = type("Response", (), {"status": 302})()
            closed = False

            def close(self):
                self.closed = True

        sock = RedirectSocket()
        with patch.object(md.websocket, "create_connection", return_value=sock) as create:
            with self.assertRaises(md.PrimaryError):
                md.connect("secret-token", 5)
            self.assertTrue(sock.closed)
            self.assertEqual(create.call_count, 1)
            self.assertEqual(create.call_args.kwargs["redirect_limit"], 0)
            self.assertEqual(create.call_args.kwargs["header"], {"X-Auth-Token": "secret-token"})
        sock.handshake_response.status = 101
        with patch.object(md.websocket, "create_connection", return_value=sock):
            self.assertIs(md.connect("secret-token", 5), sock)

    def test_503_handshake_retries_but_401_terminal(self):
        clock = Clock()
        attempts = []

        def connector(token, timeout):
            attempts.append(clock())
            raise websocket.WebSocketBadStatusException("HTTP %s %s", 503, "Unavailable")

        catalog = {"instruments": [{"instrumentId": {"marketId": "ROFX", "symbol": SYMBOL}, "cficode": "FXXXSX"}]}
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(md, "authenticate", side_effect=lambda s, u, p: s.headers.update({"X-Auth-Token": "token"})), \
                 patch.object(md, "get_json", return_value=catalog):
                with self.assertRaisesRegex(md.PrimaryError, "Reconnect budget"):
                    md.run(SYMBOL, "ROFX", Path(folder) / "retry.jsonl", "u", "p", duration=30,
                           reconnects=2, connect_fn=connector, clock=clock, sleep=clock.sleep)
                self.assertEqual(attempts, [0, 1, 3])
                rows = [json.loads(line) for line in (Path(folder) / "retry.jsonl").read_text().splitlines()]
                self.assertEqual(len([r for r in rows if r["event"] == "disconnected"]), 3)

                def denied(token, timeout):
                    raise websocket.WebSocketBadStatusException("HTTP %s %s", 401, "Unauthorized")

                with self.assertRaisesRegex(md.PrimaryError, "authentication rejected"):
                    md.run(SYMBOL, "ROFX", Path(folder) / "auth.jsonl", "u", "p", duration=30,
                           reconnects=2, connect_fn=denied, clock=clock, sleep=clock.sleep)

    def test_control_token_echo_withheld_and_subscription_error_terminal(self):
        clock = Clock()
        control = json.dumps({"type": "error", "status": "ERROR", "message": "token secret-token rejected"})
        sockets = []

        def connector(token, timeout):
            sockets.append(Socket(clock, [(websocket.ABNF.OPCODE_TEXT, control)]))
            return sockets[-1]

        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "control.jsonl"
            with patch.object(md, "authenticate", side_effect=lambda s, u, p: s.headers.update({"X-Auth-Token": "secret-token"})), \
                 patch.object(md, "get_json", return_value={"instruments": [{"instrumentId": {"marketId": "ROFX", "symbol": SYMBOL}, "cficode": "FXXXSX"}]}):
                with self.assertRaisesRegex(md.PrimaryError, "subscription/server rejected"):
                    md.run(SYMBOL, "ROFX", output, "u", "p", duration=10,
                           connect_fn=connector, clock=clock, sleep=clock.sleep)
            self.assertEqual(len(sockets), 1)
            self.assertNotIn("secret-token", output.read_text())
            self.assertNotIn("selected_instrument_observed", output.read_text())
            self.assertIn("subscription_sent", output.read_text())

    def test_recorder_flush_fsync_failure_not_retried(self):
        clock = Clock()
        sock = Socket(clock, [(websocket.ABNF.OPCODE_TEXT, json.dumps({"type": "Md", "instrumentId": {"marketId": "ROFX", "symbol": SYMBOL}, "marketData": {"BI": []}}))])
        connects = []
        original = md.Recorder.write

        def failure_on_message(recorder, event):
            if event["event"] == "message":
                with patch.object(md.os, "fsync", side_effect=OSError("disk error")):
                    return original(recorder, event)
            return original(recorder, event)

        with tempfile.TemporaryDirectory() as folder:
            with patch.object(md, "authenticate", side_effect=lambda s, u, p: s.headers.update({"X-Auth-Token": "token"})), \
                 patch.object(md, "get_json", return_value={"instruments": [{"instrumentId": {"marketId": "ROFX", "symbol": SYMBOL}, "cficode": "FXXXSX"}]}), \
                 patch.object(md.Recorder, "write", failure_on_message):
                with self.assertRaisesRegex(md.PrimaryError, "Recording write failed"):
                    md.run(SYMBOL, "ROFX", Path(folder) / "disk.jsonl", "unique-user-value", "unique-password", duration=10,
                           reconnects=5, connect_fn=lambda token, timeout: (connects.append(1) or sock),
                           clock=clock, sleep=clock.sleep)
        self.assertEqual(len(connects), 1)
        self.assertTrue(sock.closed)

    def test_md_decoded_escaped_secrets_and_malformed_frame_withheld_end_to_end(self):
        clock = Clock()
        identity = json.dumps({"marketId": "ROFX", "symbol": SYMBOL})
        frames = [
            # JSON escaped slash and mixed Unicode escape decode to the real token.
            '{"type":"Md","instrumentId":' + identity + ',"marketData":{"BI":[{"date":"sec\\u0072et\\/token"}]}}',
            '{"type":"Md","instrumentId":' + identity + ',"marketData":{"BI":["sec\\u0072et\\/token"]}}',
            '{"type":"Md","instrumentId":' + identity + ',"marketData":{"sec\\u0072et\\/token":[]}}',
            '{"type":"Md","instrumentId":' + identity + ',"marketData":{"BI":[{"nested":{"private-pass":1,"account":"value"}}]}}',
            '{"type":"Md","instrumentId":' + identity + ',"marketData":{},"error":"secret/token"',
            '{"type":"Md","instrumentId":' + identity + ',"marketData":{},"error":"sec\\u0072et\\/token","error":"ok"}',
            '{"type":"Md","instrumentId":' + identity + ',"marketData":{"OF":[]}}',
        ]
        socket = Socket(clock, [(websocket.ABNF.OPCODE_TEXT, frame) for frame in frames])
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "escaped.jsonl"
            with patch.object(md, "authenticate", side_effect=lambda s, u, p: s.headers.update({"X-Auth-Token": "secret/token"})), \
                 patch.object(md, "get_json", return_value={"instruments": [{"instrumentId": {"marketId": "ROFX", "symbol": SYMBOL}, "cficode": "FXXXSX"}]}):
                md.run(SYMBOL, "ROFX", output, "account", "private-pass", duration=8,
                       stale_after=20, connect_fn=lambda token, timeout: socket,
                       clock=clock, sleep=clock.sleep)
            recorded = output.read_text()
            rows = [json.loads(line) for line in recorded.splitlines()]
            self.assertEqual(len([r for r in rows if r["event"] == "control_message"]), 6)
            self.assertEqual(len([r for r in rows if r["event"] == "message"]), 1)
            self.assertEqual([r for r in rows if r["event"] == "message"][0]["raw"], frames[-1])
            for secret in ("secret/token", "sec\\u0072et\\/token", "private-pass", "account"):
                self.assertNotIn(secret, recorded)
            self.assertTrue(socket.closed)

    def test_nonfinite_and_nonpositive_timing_rejected_before_auth_or_file(self):
        for name in ("duration", "stale_after", "refresh_after", "interval"):
            for value in (float("nan"), float("inf"), -float("inf"), 0.0, -1.0):
                with self.subTest(name=name, value=value), tempfile.TemporaryDirectory() as folder:
                    output = Path(folder) / "should_not_exist.jsonl"
                    with patch.object(md.requests, "Session", side_effect=AssertionError("auth started")):
                        with self.assertRaises(ValueError):
                            md.run(SYMBOL, "ROFX", output, "u", "p", **{name: value})
                    self.assertFalse(output.exists())
        with patch.dict(md.os.environ, {"PRIMARY_USER": "u", "PRIMARY_PASSWORD": "private-pass"}), \
             patch.object(md.requests, "Session", side_effect=AssertionError("auth started")):
            stderr = StringIO()
            with redirect_stderr(stderr):
                result = md.main(["--no-env", "--symbol", SYMBOL, "--output", "unused.jsonl",
                                  "--duration", "nan"])
            self.assertEqual(result, 1)
            self.assertNotIn("private-pass", stderr.getvalue())

    def test_cli_write_error_nonzero_without_secret_on_stderr(self):
        with patch.dict(md.os.environ, {"PRIMARY_USER": "u", "PRIMARY_PASSWORD": "private-password"}), \
             patch.object(md, "run", side_effect=md.PrimaryError("Recording write failed")):
            stderr = StringIO()
            with redirect_stderr(stderr):
                code = md.main(["--no-env", "--symbol", SYMBOL, "--output", "unused.jsonl"])
        self.assertEqual(code, 1)
        self.assertNotIn("private-password", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()

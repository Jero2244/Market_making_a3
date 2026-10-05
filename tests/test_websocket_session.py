import json
import unittest
from unittest.mock import patch
import requests
import websocket
from test_stream_market_data import Clock, Socket, SYMBOL
from market_making.market_data import websocket_session as ws


def raw(data=None, **extra):
    return json.dumps({"type": "Md", "instrumentId": {"marketId": "ROFX", "symbol": SYMBOL},
                       "marketData": {"BI": []} if data is None else data, **extra})


def nonstandard_frames():
    for constant in ("NaN", "Infinity", "-Infinity"):
        for text in (raw(extra="INVALID_CONSTANT"),
                     raw({"BI": [], "extra": {"nested": [{"value": "INVALID_CONSTANT"}]}})):
            yield text.replace('"INVALID_CONSTANT"', constant)


class Sink:
    def __init__(self):
        self.events = []
        self.closed = False

    def write(self, event):
        self.events.append(event)

    def close(self):
        self.closed = True


class SessionTests(unittest.TestCase):
    def test_nonstandard_constants_parser_and_lifecycle_withhold_entire_frame(self):
        for text in nonstandard_frames():
            with self.subTest(text=text):
                self.assertEqual(ws.parse_market_frame(text), (None, False))
                self.run_fake([(websocket.ABNF.OPCODE_TEXT, text)])
                kinds = [event["event"] for event in self.sink.events]
                self.assertNotIn("message", kinds)
                self.assertNotIn("selected_instrument_observed", kinds)
                self.assertEqual(kinds.count("control_message"), 1)
                self.assertNotIn(text, json.dumps(self.sink.events))
                self.assertTrue(self.sockets[0].closed and self.sink.closed)

    def run_fake(self, frames=(), sink=None, connector=None, **kwargs):
        self.clock = Clock()
        self.sink = sink or Sink()
        self.sockets = []
        self.secrets = []
        self.logins = 0
        def auth(session, u, p):
            self.logins += 1
            session.headers["X-Auth-Token"] = "TOKEN" + str(self.logins)
        def connect(token, timeout):
            sock = Socket(self.clock, frames)
            self.sockets.append(sock)
            return sock
        options = dict(duration=4, clock=self.clock, sleep=self.clock.sleep,
            connect_fn=connector or connect, session_factory=requests.Session,
            authenticate_fn=auth, shared_secrets=self.secrets,
            get_json_fn=lambda s, path: {"instruments": [{"instrumentId": {
                "marketId": "ROFX", "symbol": SYMBOL}, "cficode": "FXXXSX"}]})
        options.update(kwargs)
        ws.run_session(SYMBOL, "ROFX", lambda: self.sink, "USERNAME", "PASSWORD", **options)

    def test_deadline_raw_and_smd_only_depth(self):
        text = raw({"OF": [], "LA": {"price": 100}})
        self.run_fake([(websocket.ABNF.OPCODE_TEXT, text)], depth=5)
        self.assertEqual([e["raw"] for e in self.sink.events if e["event"] == "message"], [text])
        self.assertEqual(self.sockets[0].sent[0]["depth"], 5)
        self.assertEqual([m["type"] for s in self.sockets for m in s.sent], ["smd"])
        self.assertTrue(self.sockets[0].closed and self.sink.closed)
        self.assertTrue(self.sink.events[-1]["deadline_reached"])

    def test_consumer_oserror_does_not_reconnect(self):
        sink = Sink()
        def write(event):
            if event["event"] == "message":
                raise OSError("TOKEN1")
        sink.write = write
        with self.assertRaisesRegex(ws.PrimaryError, "consumer failed"):
            self.run_fake([(websocket.ABNF.OPCODE_TEXT, raw())], sink=sink)
        self.assertEqual(len(self.sockets), 1)
        self.assertTrue(self.sockets[0].closed and sink.closed)

    def test_interrupt_closes_without_reconnect(self):
        with patch.object(Socket, "recv_data", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.run_fake()
        self.assertTrue(self.sockets[0].closed and self.sink.closed)

    def test_exhaustion_is_terminal(self):
        with self.assertRaisesRegex(ws.PrimaryError, "budget exhausted"):
            self.run_fake([ConnectionError()], reconnects=0)
        self.assertTrue(self.sockets[0].closed)

    def test_reconnect_and_resubscribe(self):
        self.run_fake([ConnectionError()], reconnects=2, duration=3)
        self.assertEqual(len(self.sockets), 2)
        self.assertTrue(all(s.closed for s in self.sockets))
        self.assertEqual([s.sent[0]["type"] for s in self.sockets], ["smd", "smd"])

    def test_renewal_registry_and_separate_budget(self):
        def connector(token, timeout):
            frames = [(websocket.ABNF.OPCODE_TEXT, raw({"BI": [], "echo": token})),
                      (websocket.ABNF.OPCODE_TEXT, raw({"OF": [], "nested": ["TOKEN1"]}))]
            sock = Socket(self.clock, frames)
            self.sockets.append(sock)
            return sock
        self.run_fake(connector=connector, duration=5, refresh_after=2, reconnects=0)
        self.assertGreaterEqual(self.logins, 2)
        self.assertIn("TOKEN1", self.secrets)
        self.assertIn("TOKEN2", self.secrets)
        self.assertFalse(any(e["event"] == "message" for e in self.sink.events))
        self.assertTrue(all(s.closed for s in self.sockets))

    def test_malformed_duplicate_utf8_and_nested_echo_withheld(self):
        bad = [b"\xff", "{", raw().replace('"BI": []', '"BI": [], "BI": []'),
               raw({"BI": [], "nested": [{"x": "TOKEN1"}]}),
               raw({"BI": [], "x": "PASSWORD"}).replace("PASSWORD", "\\u0050ASSWORD")]
        self.run_fake([(websocket.ABNF.OPCODE_TEXT, r) for r in bad], duration=6)
        self.assertFalse(any(e["event"] == "message" for e in self.sink.events))
        self.assertEqual(sum(e["event"] == "control_message" for e in self.sink.events), 5)

    def test_auth_and_subscription_errors_terminal(self):
        for error in ({"status": 401}, {"status": 403}, {"type": "error"}):
            with self.subTest(error=error), self.assertRaises(ws.PrimaryError):
                self.run_fake([(websocket.ABNF.OPCODE_TEXT, json.dumps(error))])
            self.assertEqual(len(self.sockets), 1)
            self.assertTrue(self.sockets[0].closed)

    def test_validation_before_auth_or_consumer(self):
        for option in ({"duration": float("nan")}, {"depth": 6}, {"depth": True}, {"interval": 0}):
            with self.subTest(option=option), self.assertRaises(ValueError):
                self.run_fake(**option)
            self.assertEqual(self.logins, 0)
            self.assertEqual(self.sink.events, [])

    def test_plain_escaped_nested_secret_keys_and_all_registry_tokens(self):
        secrets = ["USERNAME", "PASSWORD", "OLD_TOKEN", "NEW_TOKEN"]
        for secret in secrets:
            for data in ({"BI": [], "x": secret}, {"OF": [], "x": [{secret: "value"}]}):
                text = raw(data)
                for encoded in (text, text.replace(secret, "".join("\\u%04x" % ord(c) for c in secret))):
                    with self.subTest(secret=secret, data=data):
                        self.assertFalse(ws.parse_market_frame(encoded, secrets)[1])

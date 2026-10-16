"""Offline shared-stream checks: frame merging, health, fallback and leases."""
from datetime import datetime, timezone
import json
from pathlib import Path
import time
import unittest
from unittest.mock import Mock, patch

import websocket

from market_making.comparison.controller import ComparisonController, Options
from market_making.comparison.models import SYMBOLS
from market_making.ppi import monitor, remarkets, remarkets_stream
from market_making.ppi.remarkets_stream import Stream


def md(symbol, bid=5809, ask=5811, **extra):
    data = {"BI": [{"price": bid, "size": 4}], "OF": [{"price": ask, "size": 3}]}
    data.update(extra)
    return json.dumps({"type": "Md", "instrumentId": {"marketId": "ROFX", "symbol": symbol},
                       "marketData": data, "timestamp": 1})


class FakeSocket:
    """Delivers queued frames, then ends the session through the owning stream."""

    def __init__(self, stream, frames):
        self.stream, self.frames, self.sent, self.pings = stream, list(frames), [], 0

    def settimeout(self, _):
        pass

    def send(self, data):
        self.sent.append(json.loads(data))

    def ping(self, *_):
        self.pings += 1

    def recv_data(self, control_frame=True):
        if not self.frames:
            self.stream.stopped.set()
            raise websocket.WebSocketTimeoutException()
        return self.frames.pop(0)

    def abort(self):
        pass

    def close(self, timeout=0):
        pass


class FakeLease:
    def __init__(self, raws, healthy=True):
        self.raws, self._healthy, self.released, self.version = raws, healthy, False, 1

    def healthy(self):
        return self._healthy

    def latest(self, symbol):
        raw = self.raws.get(symbol)
        return None if raw is None else (raw, 100, datetime.now(timezone.utc))

    def wait(self, version, timeout):
        return self.version

    def release(self):
        self.released = True


def text(data):
    return websocket.ABNF.OPCODE_TEXT, data.encode()


class StreamTests(unittest.TestCase):
    def stream(self, clock=lambda: 100):
        client = Mock(_token="token")
        stream = Stream(Mock(), client_factory=Mock(return_value=client), clock=clock)
        stream.add(["GGAL/OCT26", "GGAL/DIC26"])
        return stream

    def test_frames_replace_carried_entries_and_ignore_other_symbols(self):
        stream = self.stream()
        stream._mark(connected=True)
        stream.confirmed = (100, datetime.now(timezone.utc))
        books = stream._frame(md("GGAL/OCT26", LA={"price": 5810}), 100, None)
        stream._mark(books=books, bump=True)
        # A later frame carrying only BI keeps the previous OF and LA entries.
        update = json.dumps({"type": "Md", "instrumentId": {"marketId": "ROFX", "symbol": "GGAL/OCT26"},
                             "marketData": {"BI": [{"price": 5808, "size": 1}]}})
        stream._mark(books=stream._frame(update, 100, None), bump=True)
        raw = stream.latest("GGAL/OCT26")[0]
        self.assertEqual(raw["marketData"]["BI"][0]["price"], 5808)
        self.assertEqual(raw["marketData"]["OF"][0]["price"], 5811)
        self.assertEqual(raw["marketData"]["LA"]["price"], 5810)
        book = remarkets.parse_snapshot(remarkets.Snapshot(raw, remarkets.RequestIdentity("ROFX", "GGAL/OCT26")),
                                        {"symbol": "GGAL/OCT26", "market_id": "ROFX"})
        self.assertEqual(book.identity_basis, "response-confirmed")
        self.assertIsNone(stream._frame(md("WTI/NOV26"), 100, None))
        self.assertIsNone(stream._frame(json.dumps({"type": "error"}), 100, None))

    def test_pong_confirms_unchanged_books_and_silence_is_unhealthy(self):
        clock = [100]
        stream = self.stream(lambda: clock[0])
        stream._mark(connected=True)
        stream._mark(books=stream._frame(md("GGAL/OCT26"), 100, "frame"), bump=True)
        stream.confirmed = (103, "pong")
        clock[0] = 104
        self.assertEqual(stream.latest("GGAL/OCT26")[1:], (103, "pong"))
        clock[0] = 103 + remarkets_stream.HEALTHY_SECONDS + .1
        self.assertFalse(stream.healthy())
        self.assertIsNone(stream.latest("GGAL/OCT26"))

    def test_session_subscribes_merges_and_disconnect_clears_books(self):
        stream = self.stream(time.monotonic)
        sockets = []

        def connect(url, **kwargs):
            self.assertEqual(url, remarkets_stream.WS_URL)
            self.assertEqual(kwargs["header"], {"X-Auth-Token": "token"})
            sockets.append(FakeSocket(stream, [text(md("GGAL/OCT26")), text(md("GGAL/DIC26", 6100, 6120)),
                                               (websocket.ABNF.OPCODE_PONG, b"")]))
            return sockets[0]

        stream.connect = connect
        seen = {}
        original = stream._mark

        def mark(**kwargs):
            original(**kwargs)
            if kwargs.get("bump"):
                seen.update({s: stream.latest(s) for s in ("GGAL/OCT26", "GGAL/DIC26")})

        stream._mark = mark
        stream._run()
        self.assertEqual(sockets[0].sent[0]["products"],
                         [{"symbol": "GGAL/DIC26", "marketId": "ROFX"}, {"symbol": "GGAL/OCT26", "marketId": "ROFX"}])
        self.assertEqual(seen["GGAL/DIC26"][0]["marketData"]["BI"][0]["price"], 6100)
        self.assertFalse(stream.connected)
        self.assertIsNone(stream.latest("GGAL/OCT26"))

    def test_leases_share_one_stream_until_the_last_release(self):
        credentials = remarkets.Credentials(dict.fromkeys(remarkets.KEYS, "user-and-password"))
        with patch.object(Stream, "start"), patch.object(Stream, "stop") as stop:
            first = remarkets_stream.acquire(credentials, ["GGAL/OCT26"])
            second = remarkets_stream.acquire(credentials, ["WTI/NOV26"])
            self.assertIs(first.stream, second.stream)
            self.assertEqual(first.stream.symbols, {"GGAL/OCT26", "WTI/NOV26"})
            first.release()
            first.release()
            stop.assert_not_called()
            second.release()
            stop.assert_called_once()
        self.assertEqual(remarkets_stream._streams, {})


class ConsumerTests(unittest.TestCase):
    def test_comparison_publishes_streamed_books_without_rest(self):
        lease = FakeLease({s: {"status": "OK", "instrumentId": {"marketId": "ROFX", "symbol": s},
                               "marketData": json.loads(md(s))["marketData"]} for s in SYMBOLS})
        factory = Mock(side_effect=AssertionError("REST"))
        controller = ComparisonController(clock=lambda: 100, remarkets_factory=factory,
                                          stream_source=Mock(return_value=lease))
        stop = controller._stop

        def wait(version, timeout):
            stop.set()
            return 1

        lease.wait = wait
        with patch.object(remarkets, "load_credentials", return_value=Mock(ready=True)):
            controller._simulated(0, stop, Options(env_file=Path("unused")))
        factory.assert_not_called()
        self.assertTrue(lease.released)
        self.assertEqual(set(controller.quotes["reMarkets"]), set(SYMBOLS))
        self.assertTrue(all(not q.problems for q in controller.quotes["reMarkets"].values()))

    def test_unhealthy_stream_falls_back_to_rest_polling(self):
        from test_real_comparison import raw_snapshot
        lease = FakeLease({}, healthy=False)
        client = Mock()
        client.snapshot.side_effect = lambda instrument, **_: raw_snapshot(instrument["symbol"])
        controller = ComparisonController(clock=lambda: 100, remarkets_factory=Mock(return_value=client),
                                          stream_source=Mock(return_value=lease))
        with patch.object(remarkets, "load_credentials", return_value=Mock(ready=True)), \
                patch.object(controller._stop, "wait", return_value=True):
            controller._simulated(0, controller._stop, Options(env_file=Path("unused")))
        self.assertEqual(client.snapshot.call_count, len(SYMBOLS))
        self.assertTrue(lease.released)

    def test_watch_session_uses_streamed_futures_and_manual_spot(self):
        config = monitor.default_watch_config(30)
        symbols = [item["symbol"] for item in config["futures"]]
        lease = FakeLease({s: {"status": "OK", "instrumentId": {"marketId": "ROFX", "symbol": s},
                               "marketData": json.loads(md(s, 6000, 6010))["marketData"]} for s in symbols})
        session = monitor.WatchSession(Mock(ready=False), Mock(ready=True), stream=lease)
        with patch.object(monitor, "RemarketsClient") as rest:
            reports = session.read(config, 1, manual_spot=monitor.ManualSpotQuote(5900, 5905))
            session.close()
        rest.return_value.snapshot.assert_not_called()
        rest.return_value.login.assert_not_called()
        self.assertEqual([r["books"]["future"]["bids"][0]["price"] for r in reports], [6000, 6000])
        self.assertIsNotNone(reports[0]["quote_summary"]["implied_yield_tna"])


class LoginSpacingTests(unittest.TestCase):
    def test_real_session_logins_are_spaced(self):
        credentials = remarkets.Credentials(dict.fromkeys(remarkets.KEYS, "user-and-password"))
        times = []
        with patch.object(remarkets, "LOGIN_SPACING", .3), \
                patch.object(remarkets.Client, "_request", side_effect=lambda **_: times.append(time.monotonic())):
            for _ in range(2):
                client = remarkets.Client(credentials, live=True)
                client.login()
                client.close()
        self.assertGreaterEqual(times[1] - times[0], .29)


if __name__ == "__main__":
    unittest.main()

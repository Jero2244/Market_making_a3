import json
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout
from io import StringIO
import websocket
from test_stream_market_data import Clock, Socket, SYMBOL
from test_websocket_session import raw, nonstandard_frames
from market_making.market_data.websocket_observation import Observation
from market_making.market_data import view_order_book as viewer


class ObservationTests(unittest.TestCase):
    def test_nonstandard_constants_never_make_observation_eligible(self):
        for text in nonstandard_frames():
            with self.subTest(text=text):
                self.ob.write({"event": "message", "raw": text})
                self.assertFalse(self.ob.eligible)
                self.assertIsNone(self.ob.latest)
                self.assertNotIn(text, json.dumps(self.ob.snapshot()))

    def setUp(self):
        self.clock = Clock()
        self.ob = Observation("ROFX", SYMBOL, clock=self.clock, stale_after=2, depth=1)
        self.ob.write({"event": "connected"})

    def message(self, data):
        self.ob.write({"event": "message", "raw": raw(data)})

    def test_no_cross_frame_merge_empty_and_omitted(self):
        self.message({"BI": [{"price": 100, "size": 0}, {"price": 99}]})
        self.assertEqual(len(self.ob.snapshot()["entries"]["BI"]["raw_observation"]), 1)
        self.message({"OF": []})
        state = self.ob.snapshot()
        self.assertEqual(state["entries"]["BI"], {"state": "omitted"})
        self.assertEqual(state["entries"]["OF"]["state"], "empty")
        self.assertTrue(state["partial"])
        self.assertIn("unverified", state["exchange_freshness"])

    def test_stale_disconnect_generation_reset(self):
        self.message({"BI": []})
        self.clock.value = 3
        self.assertEqual(self.ob.snapshot()["entries"]["BI"]["state"], "stale")
        self.ob.write({"event": "disconnected"})
        self.assertEqual(self.ob.snapshot()["entries"]["BI"]["state"], "disconnected")
        self.ob.write({"event": "connected"})
        self.assertEqual(self.ob.snapshot()["generation"], 2)
        self.assertEqual(self.ob.snapshot()["entries"]["BI"]["state"], "generation-reset")
        self.assertIsNone(self.ob.snapshot()["receipt_age_seconds"])

    def test_wrong_identity_empty_dictionary_and_secrets_ineligible(self):
        wrong = raw({"BI": []}).replace(SYMBOL, "OTHER")
        self.ob.write({"event": "message", "raw": wrong})
        self.message({})
        self.assertFalse(self.ob.eligible)
        self.ob.secrets.append("NEW_TOKEN")
        self.message({"BI": [], "echo": "NEW_TOKEN"})
        self.assertFalse(self.ob.eligible)


class ViewerTests(unittest.TestCase):
    def test_only_nonstandard_constants_returns_nonzero_without_evidence_or_payload(self):
        from market_making.market_data import websocket_session as shared
        for text in nonstandard_frames():
            with self.subTest(text=text):
                clock = Clock()
                sock = Socket(clock, [(websocket.ABNF.OPCODE_TEXT, text)])
                observations, events = [], []
                def run(symbol, market_id, factory, user, password, **kwargs):
                    observation = factory()
                    observations.append(observation)
                    original_write = observation.write
                    def write(event):
                        events.append(event)
                        original_write(event)
                    observation.write = write
                    return shared.run_session(symbol, market_id, lambda: observation, user, password,
                        **kwargs, clock=clock, sleep=clock.sleep,
                        connect_fn=lambda token, timeout: sock,
                        get_json_fn=lambda s, path: {"instruments": [{"instrumentId": {
                            "marketId": "ROFX", "symbol": SYMBOL}, "cficode": "FXXXSX"}]},
                        authenticate_fn=lambda s, u, p: s.headers.update({"X-Auth-Token": "TOKEN"}))
                stdout = StringIO()
                with patch.object(viewer, "load_dotenv"), patch.dict(viewer.os.environ, {"PRIMARY_USER": "USER", "PRIMARY_PASSWORD": "PASS"}), \
                     patch.object(viewer, "run_session", side_effect=run), patch.object(viewer, "fetch_book") as fetch, \
                     patch.object(viewer, "resolve") as rules, redirect_stdout(stdout):
                    self.assertEqual(viewer.main(["--live", "--transport", "websocket", "--duration", "2"]), 2)
                    fetch.assert_not_called()
                    rules.assert_not_called()
                self.assertFalse(observations[0].eligible)
                self.assertFalse(any(event["event"] in ("message", "selected_instrument_observed") for event in events))
                self.assertIn("NOT_OBSERVED", stdout.getvalue())
                self.assertNotIn(text, stdout.getvalue())
                self.assertTrue(sock.closed)

    def test_default_and_websocket_without_live_never_network_or_env(self):
        for argv in ([], ["--transport", "websocket"]):
            with patch.object(viewer, "load_dotenv") as env, patch.object(viewer, "authenticate") as auth, \
                 patch.object(viewer, "run_session") as run, redirect_stdout(StringIO()):
                self.assertEqual(viewer.main(argv), 2)
                env.assert_not_called()
                auth.assert_not_called()
                run.assert_not_called()

    def test_rest_default_unchanged(self):
        with patch.object(viewer, "load_dotenv"), patch.dict(viewer.os.environ, {"PRIMARY_USER": "U", "PRIMARY_PASSWORD": "P"}), \
             patch.object(viewer, "authenticate"), patch.object(viewer, "resolve") as rules, \
             patch.object(viewer, "fetch_book") as fetch, patch.object(viewer, "run_session") as run, redirect_stdout(StringIO()):
            fetch.return_value.summary.return_value = {}
            self.assertEqual(viewer.main(["--live"]), 0)
            rules.assert_called_once()
            fetch.assert_called_once()
            run.assert_not_called()

    def test_websocket_no_rest_marketdata_and_deadline_exit(self):
        from market_making.market_data import websocket_session as shared
        for frames, code in (([], 2), ([(websocket.ABNF.OPCODE_TEXT, raw({"BI": []}))], 0)):
            clock = Clock()
            sock = Socket(clock, frames)
            paths = []
            def get(session, path):
                paths.append(path)
                return {"instruments": [{"instrumentId": {"marketId": "ROFX", "symbol": SYMBOL}, "cficode": "FXXXSX"}]}
            def run(*args, **kwargs):
                return shared.run_session(*args, **kwargs, clock=clock, sleep=clock.sleep,
                    connect_fn=lambda token, timeout: sock, get_json_fn=get,
                    authenticate_fn=lambda s, u, p: s.headers.update({"X-Auth-Token": "TOKEN"}))
            with patch.object(viewer, "load_dotenv"), patch.dict(viewer.os.environ, {"PRIMARY_USER": "USER", "PRIMARY_PASSWORD": "PASS"}), \
                 patch.object(viewer, "run_session", side_effect=run), patch.object(viewer, "fetch_book") as fetch, \
                 patch.object(viewer, "resolve") as rules, redirect_stdout(StringIO()):
                self.assertEqual(viewer.main(["--live", "--transport", "websocket", "--duration", "2"]), code)
                fetch.assert_not_called()
                rules.assert_not_called()
            self.assertEqual(paths, ["/rest/instruments/all"])
            self.assertTrue(sock.closed)

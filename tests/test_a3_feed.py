"""Public viewer protocol, exact contract mapping, and snapshot refresh tests."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import websocket
from market_making.comparison import a3
from market_making.comparison.models import SYMBOLS

# Public after-hours sample captured from the viewer on 2026-10-14.
FRAME = "M:rx_DUAL_GGAL_OCT26|402552|6|5812|5820|9|5820|2026-10-14T19:59:32Z|4024|2367439500|402400|5793|5985|5939|9908|5820|2026-10-14|5820|2026-10-14|6006|2026-10-13"


def reference():
    return json.loads((Path(__file__).parent / "fixtures" / "a3_comparison_contracts.json").read_text())


class A3FeedTests(unittest.TestCase):
    def test_single_and_batched_frames_and_last_timestamp_scope(self):
        for raw in (FRAME, FRAME.encode(), json.dumps([FRAME, FRAME.replace("OCT26", "DIC26")])):
            observations = a3.decode_frame(raw, now=100)
            first = observations[0]
            self.assertEqual((first.bid.price, first.bid.quantity, first.ask.price, first.ask.quantity),
                             (5812, 6, 5820, 9))
            self.assertEqual(first.last, 5820)
            self.assertEqual(first.last_time.isoformat(), "2026-10-14T19:59:32+00:00")
            self.assertEqual(first.received_monotonic, 100)

    def test_empty_fields_replace_prior_values(self):
        fields = FRAME.split("|")
        fields[2:6] = ["", "", "0", "0"]
        q = a3.decode_frame("|".join(fields), now=105)[0]
        self.assertIsNone(q.bid)
        self.assertIsNone(q.ask)
        self.assertFalse(q.problems)

    def test_rfx20_and_wti_frames_keep_their_prices_and_identity(self):
        frames = []
        for a3_id, bid, ask in (("rx_DUAL_RFX20_OCT26", "620000", "620100"),
                                ("rx_DUAL_WTI_NOV26", "70.12", "70.13")):
            fields = FRAME.split("|")
            fields[0], fields[3], fields[4] = "M:" + a3_id, bid, ask
            frames.append("|".join(fields))
        rfx20, wti = a3.decode_frame(json.dumps(frames), now=100)
        self.assertEqual((rfx20.symbol, rfx20.bid.price, rfx20.ask.price),
                         ("RFX20/OCT26", 620000, 620100))
        self.assertEqual((wti.symbol, wti.bid.price, wti.ask.price), ("WTI/NOV26", 70.12, 70.13))
        self.assertFalse(rfx20.problems or wti.problems)

    def test_invalid_numbers_and_crossed_quotes_disable_contract(self):
        for value in ("nan", "inf", "-1", "bad", "0"):
            fields = FRAME.split("|")
            fields[3] = value
            q = a3.decode_frame("|".join(fields), now=100)[0]
            self.assertTrue(q.problems)
        fields = FRAME.split("|")
        fields[3] = "5820"
        self.assertIn("crossed_or_locked_book", a3.decode_frame("|".join(fields), now=100)[0].problems)

    def test_heartbeat_depth_and_unselected_contract_do_not_refresh_quotes(self):
        for raw in ("pong", "{}", "B:rx_DUAL_GGAL_OCT26!1!|||", "[]", FRAME.replace("GGAL", "PAMP")):
            self.assertEqual(a3.decode_frame(raw, now=100), ())

    def test_changed_protocol_stops_decoding_without_raw_payload(self):
        for raw in ('["broken"', '[{"SECRET":"value"}]', b"\xff", FRAME.rsplit("|", 1)[0],
                    FRAME.replace("402552", "SECRET")):
            with self.assertRaises(a3.FeedError) as ctx:
                a3.decode_frame(raw, now=100)
            self.assertNotIn("SECRET", str(ctx.exception))
        with self.assertRaises(a3.TransientFeedError):
            a3.decode_frame("Q:reload", now=100)

    def test_exact_unique_contracts_and_units(self):
        result = a3.catalog(reference())
        self.assertEqual(set(result), set(SYMBOLS))
        self.assertEqual((result[SYMBOLS[0]].tick, result[SYMBOLS[0]].multiplier), (1, 100))
        self.assertEqual((result["RFX20/OCT26"].tick, result["RFX20/OCT26"].multiplier), (100, 1))
        self.assertEqual((result["WTI/NOV26"].tick, result["WTI/NOV26"].multiplier,
                          result["WTI/NOV26"].currency), (.01, 10, "USG"))
        for field, value in (("id", "wrong"), ("currency", "USD"), ("factor", "100"),
                             ("contractMultiplier", "1"), ("minPriceIncrement", "nan"),
                             ("cfiCode", "OCEFXS"), ("maturityDate", "20271030")):
            data = reference()
            data["securities"][0][field] = value
            with self.assertRaises(a3.FeedError):
                a3.catalog(data)
        data = reference()
        data["securities"].append(copy.deepcopy(data["securities"][0]))
        with self.assertRaises(a3.FeedError):
            a3.catalog(data)

    def test_new_contract_metadata_must_match_and_missing_contract_is_named(self):
        for symbol in ("RFX20/OCT26", "WTI/NOV26"):
            for field, value in (("currency", "USD"), ("contractMultiplier", "100"),
                                 ("factor", "10"), ("maturityDate", "20261230"),
                                 ("isProduct", True)):
                with self.subTest(symbol=symbol, field=field):
                    data = reference()
                    row = next(row for row in data["securities"] if row["symbol"] == symbol)
                    row[field] = value
                    with self.assertRaises(a3.FeedError):
                        a3.catalog(data)
            data = reference()
            data["securities"] = [row for row in data["securities"] if row["symbol"] != symbol]
            with self.assertRaisesRegex(a3.FeedError, symbol):
                a3.catalog(data)

    def test_repeated_subscription_reobserves_equal_quotes(self):
        client = a3.Client(session=Mock())
        client.ws = Mock()
        client.ws.recv.return_value = FRAME
        client.next_refresh = 0
        client.last_transport = 100
        try:
            with patch.object(a3.time, "monotonic", return_value=100):
                first = client.read()[0]
            with patch.object(a3.time, "monotonic", return_value=105):
                second = client.read()[0]
            self.assertEqual(first.bid, second.bid)
            self.assertEqual((first.received_monotonic, second.received_monotonic), (100, 105))
            subscriptions = [call.args[0] for call in client.ws.send.call_args_list if call.args[0] != "ping"]
            self.assertEqual(len(subscriptions), 2)
            self.assertEqual(json.loads(subscriptions[0])["topics"],
                             ["md.rx_DUAL_GGAL_OCT26", "md.rx_DUAL_GGAL_DIC26",
                              "md.rx_DUAL_RFX20_OCT26", "md.rx_DUAL_WTI_NOV26"])
        finally:
            client.close()

    def test_missing_transport_disconnect_and_cancel(self):
        client = a3.Client(session=Mock())
        client.ws = Mock()
        client.ws.recv.side_effect = websocket.WebSocketTimeoutException()
        client.next_refresh = 200
        client.last_transport = 100
        with patch.object(a3.time, "monotonic", return_value=116):
            with self.assertRaises(a3.TransientFeedError):
                client.read()
        client.ws.recv.side_effect = None
        client.ws.recv.return_value = ""
        with self.assertRaises(a3.TransientFeedError):
            client.read()
        client.close()
        self.assertTrue(client.stopped.is_set())
        client.ws.abort.assert_called()

    def test_bootstrap_only_uses_public_get_routes_and_no_redirects(self):
        session = Mock(cookies=[])
        root = Mock(status_code=200)
        root.iter_content.return_value = [b"public page"]
        ref = Mock(status_code=200)
        ref.iter_content.return_value = [json.dumps(reference()).encode()]
        session.get.side_effect = [root, ref]
        ws = Mock()
        client = a3.Client(session=session, socket_factory=Mock(return_value=ws))
        try:
            self.assertEqual(set(client.connect()), set(SYMBOLS))
            self.assertEqual([call.args[0] for call in session.get.call_args_list],
                             [a3.ORIGIN + "/", a3.ORIGIN + "/api/v2/ref-data"])
            self.assertTrue(all(call.kwargs["allow_redirects"] is False for call in session.get.call_args_list))
            self.assertEqual(ws.connect.call_args.kwargs["redirect_limit"], 0)
        finally:
            client.close()

    def test_http_throttle_and_denied_access(self):
        for status, expected in ((429, a3.TransientFeedError), (503, a3.TransientFeedError),
                                 (401, a3.FeedError), (302, a3.FeedError)):
            session = Mock()
            session.get.return_value = Mock(status_code=status)
            client = a3.Client(session=session)
            try:
                with self.assertRaises(expected):
                    client._get("/")
            finally:
                client.close()


if __name__ == "__main__":
    unittest.main()

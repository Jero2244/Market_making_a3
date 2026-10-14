"""Offline REMARKETS transport and mixed-provider routing regressions."""
import json
import copy
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from contextlib import redirect_stdout
from datetime import datetime

from market_making.ppi import monitor, remarkets
from market_making.ppi.client import PPIError
from market_making.ppi.monitor_config import validate_config
from test_ppi_monitor import config, NOW, raw_book, primary_client


def snapshot():
    return dict(status="OK", instrumentId={"marketId": "ROFX", "symbol": config()["futures"][0]["symbol"]},
                marketData={"BI": [{"price": 109, "size": 1}, {"price": 110, "size": 10}],
                            "OF": [{"price": 112, "size": 1}, {"price": 111, "size": 10}]})


def credentials():
    return remarkets.Credentials({"PRIMARY_USER": "private-user", "PRIMARY_PASSWORD": "private-password"})


def response(data=None, status=200, token=None):
    r = Mock(status_code=status, headers={"X-Auth-Token": token} if token else {})
    r.iter_content.return_value = [json.dumps(data).encode()]
    return r


def historical(name):
    return json.loads((Path(__file__).parent / "fixtures" / ("remarkets_ggal_" + name + ".json")).read_text())


class RequestBoundBooksTests(unittest.TestCase):
    def test_historical_full_levels_identity_and_no_mutation(self):
        expected = [([(6125, 1), (6120, 15), (6112, 2), (6000, 1)],
                     [(6130, 30), (6176, 1), (6207, 5)]),
                    ([(6339, 6), (6204, 6)], [(6405, 6), (6486, 2), (6498, 1)])]
        for i, name in enumerate(("oct26", "dic26")):
            fixture = historical(name)
            raw = fixture["snapshot"]
            before = copy.deepcopy(raw)
            instrument = config()["futures"][i]
            receipt = datetime.fromisoformat(fixture["received_at_utc"])
            context = remarkets.RequestIdentity("ROFX", instrument["symbol"])
            unbound = remarkets.parse_snapshot(raw, instrument, receipt=receipt)
            self.assertIn("remarkets_instrument_identity_missing", unbound.blockers)
            book = remarkets.parse_snapshot(raw, instrument, receipt=receipt, request_identity=context)
            self.assertEqual(book.identity_basis, "request-bound")
            self.assertEqual([(x.price, x.quantity) for x in book.bids], expected[i][0])
            self.assertEqual([(x.price, x.quantity) for x in book.offers], expected[i][1])
            self.assertIs(book.bid, book.bids[0])
            self.assertIs(book.ask, book.offers[0])
            self.assertEqual(book.requested_depth, 5)
            self.assertEqual(book.receipt_time, receipt)
            self.assertIsNone(book.source_time)
            self.assertIsNone(book.last_trade_time)
            self.assertEqual(raw, before)
            self.assertNotIn("instrumentId", raw)

    def test_response_identity_must_not_be_overridden(self):
        instrument = config()["futures"][0]
        context = remarkets.RequestIdentity("ROFX", instrument["symbol"])
        for identity in (None, [], {}, "GGAL/OCT26", {"marketId": "MERV", "symbol": instrument["symbol"]},
                         {"marketId": "ROFX", "symbol": "GGAL/DIC26"}):
            raw = {**historical("oct26")["snapshot"], "instrumentId": identity}
            book = remarkets.parse_snapshot(raw, instrument, request_identity=context)
            self.assertIn("remarkets_instrument_identity_mismatch", book.blockers)
            self.assertIsNone(book.identity_basis)
        confirmed = remarkets.parse_snapshot(snapshot(), instrument, request_identity=context)
        self.assertEqual(confirmed.identity_basis, "response-confirmed")
        for context in ({"marketId": "ROFX", "symbol": instrument["symbol"]},
                        remarkets.RequestIdentity("ROFX", "GGAL/DIC26")):
            book = remarkets.parse_snapshot(historical("oct26")["snapshot"], instrument, request_identity=context)
            self.assertIn("remarkets_request_identity_mismatch", book.blockers)

    def test_exact_config_mapping(self):
        for symbol in ("GGAL/DIC26", "DLR/OCT26", "GGAL/OCT26 "):
            cfg = config()
            cfg["futures"][0]["symbol"] = symbol
            with self.assertRaises(ValueError):
                validate_config(cfg)
        cfg = config()
        cfg["futures"].reverse()
        self.assertEqual(validate_config(cfg), cfg)

    def test_real_transport_mock_cycle_books_visible_in_strict_and_manual(self):
        for manual in (False, True):
            session = Mock()
            responses = [response(token="private-token"), response(historical("oct26")["snapshot"]),
                         response(historical("dic26")["snapshot"])]
            session.request.side_effect = responses
            primary = remarkets.Client(credentials(), live=True, session=session)
            spot = Mock()
            spot.get.return_value = raw_book(6100, 6105, quantity=10000)
            with patch.object(monitor, "Client", return_value=spot), \
                    patch.object(monitor, "RemarketsClient", return_value=primary), \
                    patch.object(monitor, "datetime", wraps=datetime) as clock:
                clock.now.return_value = NOW
                reports = monitor.live_cycle(config(), Mock(ready=True), 1,
                                             primary_credentials=credentials(), manual_check=manual)
            self.assertEqual(spot.get.call_count, 1)
            self.assertEqual(session.request.call_count, 3)
            self.assertEqual(primary.remaining, 0)
            self.assertIsNone(primary._token)
            for i, call in enumerate(session.request.call_args_list[1:]):
                self.assertEqual(call.kwargs["params"], {"marketId": "ROFX", "symbol": config()["futures"][i]["symbol"],
                                                       "entries": "BI,OF", "depth": 5})
            spot.close.assert_called_once()
            session.close.assert_called_once()
            for r in responses:
                r.close.assert_called_once()
            if not manual:
                self.assertTrue(all(r["status"] == monitor.UNAVAILABLE for r in reports))
            else:
                # Lower levels cannot increase best-level capacity: OCT best bid is just one contract.
                self.assertEqual(reports[0]["directions"]["cash_carry"]["capacity_contracts"], 1)
                self.assertEqual(reports[1]["directions"]["cash_carry"]["capacity_contracts"], 6)
            for i, report in enumerate(reports):
                self.assertFalse(report["executable"])
                self.assertFalse(report["live_freshness_established"])
                book = report["books"]["future"]
                self.assertEqual(book["symbol"], config()["futures"][i]["symbol"])
                self.assertEqual(book["market"], "ROFX")
                self.assertEqual(book["provider"], "remarkets")
                self.assertTrue(book["simulated"])
                self.assertEqual(book["identity_basis"], "request-bound")
                self.assertIsNone(book["source_timestamp"])
                self.assertFalse(book["source_timestamp_verified"])
                self.assertEqual(book["receipt_timestamp"], NOW.isoformat())
                self.assertEqual(book["bid_count"], (4, 2)[i])
                self.assertEqual(book["offer_count"], 3)
            output = io.StringIO()
            with redirect_stdout(output):
                monitor.emit(reports)
            text = output.getvalue()
            self.assertEqual(len(text.splitlines()), 2)
            for fragment in ("remarkets book: bid 6125.00 | ask 6130.00, ppi ggal price: bid 6100.00 | ask 6105.00",
                             "remarkets book: bid 6339.00 | ask 6405.00", "implied yield 5.20% caucion 36.00%",
                             "implied yield 16.65% caucion 36.00%"):
                self.assertIn(fragment, text)
            output = io.StringIO()
            with redirect_stdout(output):
                monitor.emit(reports, verbose=True)
            text = output.getvalue()
            for fragment in ("GGAL/OCT26 ROFX REMARKETS simulated", "BI[4]=6125x1,6120x15,6112x2,6000x1",
                             "OF[3]=6405x6,6486x2,6498x1", "identity=request-bound", "source=null (unverified)"):
                self.assertIn(fragment, text)
            output = io.StringIO()
            with redirect_stdout(output):
                monitor.emit(reports, True)
            self.assertEqual([json.loads(line) for line in output.getvalue().splitlines()], reports)


class NormalizationTests(unittest.TestCase):
    def parse(self, data):
        return remarkets.parse_snapshot(data, config()["futures"][0], receipt=NOW)

    def test_best_levels_and_no_timestamp_invention(self):
        for extra in ({}, {"timestamp": 1791374400000}, {"date": NOW.isoformat()}):
            data = snapshot()
            data.update(extra)
            data["marketData"]["LA"] = {"price": 0, "date": NOW.isoformat()}
            book = self.parse(data)
            self.assertEqual((book.bid.price, book.bid.quantity, book.ask.price), (110, 10, 111))
            self.assertIsNone(book.source_time)
            self.assertIsNone(book.last_trade_time)
            cfg = config()
            spot = monitor.parse_book(raw_book(), receipt=NOW)
            self.assertEqual(monitor.evaluate(spot, book, cfg, cfg["futures"][0], now=NOW)["status"], monitor.UNAVAILABLE)
            self.assertEqual(monitor.evaluate(spot, book, cfg, cfg["futures"][0], now=NOW,
                                             manual_check=True)["status"], monitor.EDGE)

    def test_malformed_status_identity_numbers_depth_duplicates_cross(self):
        cases = [None, [], {}, {**snapshot(), "status": "ERROR"},
                 {**snapshot(), "instrumentId": {"marketId": "ROFX", "symbol": "wrong"}},
                 {**snapshot(), "marketData": None}]
        for rows in ([], None, [{"price": 110}], [{"price": True, "size": 1}],
                     [{"price": float("nan"), "size": 1}], [{"price": 110, "size": float("inf")}],
                      [{"price": 110, "size": 0}], [{"price": "110", "size": 1}],
                      [{"price": 110, "size": 1.5}],
                     [{"price": 110, "size": 1}] * 2,
                     [{"price": 100 + i, "size": 1} for i in range(6)],
                     [{"price": 111, "size": 1}]):
            data = snapshot()
            data["marketData"]["BI"] = rows
            cases.append(data)
        cfg = config()
        for data in cases:
            with self.subTest(data=data):
                result = monitor.evaluate(monitor.parse_book(raw_book(), receipt=NOW), self.parse(data),
                                          cfg, cfg["futures"][0], now=NOW, manual_check=True)
                self.assertEqual(result["status"], monitor.UNAVAILABLE)

    def test_legacy_wrong_provider_and_market_rejected(self):
        for changes in ({"ticker": "legacy"}, {"provider": "ppi"}, {"market_id": "MERV"}):
            cfg = config()
            cfg["futures"][0].update(changes)
            with self.assertRaises(ValueError):
                validate_config(cfg)


class TransportTests(unittest.TestCase):
    def test_fixed_requests_headers_budgets_and_closure(self):
        session = Mock()
        responses = [response(token="private-token"), response(snapshot()), response(snapshot())]
        session.request.side_effect = responses
        client = remarkets.Client(credentials(), live=True, session=session)
        client.login()
        for _ in range(2):
            client.snapshot(config()["futures"][0])
        with self.assertRaisesRegex(PPIError, "request_budget_exhausted"):
            client.snapshot(config()["futures"][0])
        self.assertFalse(session.trust_env)
        calls = session.request.call_args_list
        self.assertEqual(calls[0].args, ("POST", remarkets.BASE_URL + "/auth/getToken"))
        self.assertEqual(calls[0].kwargs["headers"]["X-Username"], "private-user")
        for call in calls[1:]:
            self.assertEqual(call.args, ("GET", remarkets.BASE_URL + "/rest/marketdata/get"))
            self.assertEqual(call.kwargs["params"], {"marketId": "ROFX", "symbol": config()["futures"][0]["symbol"],
                                                     "entries": "BI,OF", "depth": 5})
            self.assertNotIn("X-Password", call.kwargs["headers"])
        for call in calls:
            self.assertFalse(call.kwargs["allow_redirects"])
            self.assertTrue(call.kwargs["verify"])
            self.assertEqual(call.kwargs["timeout"], (5, 10))
        for r in responses:
            r.close.assert_called_once()
        client.close()
        self.assertIsNone(client._token)
        session.close.assert_called_once()

    def test_redirect_auth_timeout_size_echo_json_sanitized_no_retry(self):
        for failure, code in ((response(status=302), "http_failure_302"),
                              (response(status=401), "http_failure_401"),
                              (response(status=403), "http_failure_403"),
                              (RuntimeError("private-password"), "transport_or_json_failure")):
            session = Mock()
            session.request.side_effect = [failure]
            client = remarkets.Client(credentials(), live=True, session=session)
            with self.assertRaisesRegex(PPIError, code):
                client.login()
            self.assertEqual(session.request.call_count, 1)
        for payload, code in ((b'{"nested":"private-password"}', "sensitive_response_withheld"),
                              (b'{"x":1,"x":2}', "transport_or_json_failure"),
                              (b"x" * 2_000_001, "response_limit_exceeded")):
            session = Mock()
            r = response()
            r.iter_content.return_value = [payload]
            session.request.side_effect = [response(token="private-token"), r]
            client = remarkets.Client(credentials(), live=True, session=session)
            client.login()
            with self.assertRaisesRegex(PPIError, code):
                client.snapshot(config()["futures"][0])
            r.close.assert_called_once()
        session = Mock()
        client = remarkets.Client(credentials(), live=True, session=session)
        client.deadline = 0
        with self.assertRaisesRegex(PPIError, "request_budget_exhausted"):
            client.login()
        session.request.assert_not_called()

    def test_credentials_precedence_redaction_no_ppi_reuse(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / ".env"
            path.write_text("PRIMARY_USER=file-user\nPRIMARY_PASSWORD=file-password\nPPI_API_KEY=other\n")
            creds = remarkets.load_credentials(path, {"PRIMARY_USER": "environment-user"})
            self.assertEqual(creds.values["PRIMARY_USER"], "environment-user")
            self.assertEqual(creds.values["PRIMARY_PASSWORD"], "file-password")
            self.assertNotIn("file-password", repr(creds))
            self.assertFalse(remarkets.load_credentials(path, {"PRIMARY_PASSWORD": ""}).ready)
        self.assertFalse(remarkets.Credentials({"PPI_API_KEY": "other"}).ready)


class RoutingTests(unittest.TestCase):
    def test_malformed_expiry_does_not_suppress_valid_manual_pair(self):
        spot, primary = Mock(), primary_client()
        spot.get.return_value = raw_book()
        primary.snapshot.side_effect = [snapshot(), {}]
        with patch.object(monitor, "Client", return_value=spot), \
                patch.object(monitor, "RemarketsClient", return_value=primary), \
                patch.object(monitor, "datetime", wraps=datetime) as clock:
            clock.now.return_value = NOW
            reports = monitor.live_cycle(config(), Mock(ready=True), 1,
                                         primary_credentials=Mock(ready=True), manual_check=True)
        self.assertEqual(reports[0]['status'], monitor.EDGE)
        self.assertEqual(reports[1]['status'], monitor.UNAVAILABLE)
        self.assertIn('malformed_remarkets_market_data', reports[1]['blockers'])
        self.assertIsNotNone(reports[1]['books'])
        self.assertEqual(spot.get.call_count, 1)
        self.assertEqual(primary.snapshot.call_count, 2)
        spot.close.assert_called_once()
        primary.close.assert_called_once()

    def test_preflight_partial_construction_login_and_read_failures(self):
        cfg = config()
        with patch.object(monitor, "Client") as factory:
            with self.assertRaises(PPIError):
                monitor.live_cycle(cfg, Mock(ready=True), 1, primary_credentials=Mock(ready=False))
            factory.assert_not_called()
        for stage in ("construction", "ppi_login", "primary_login", "second_snapshot", "interrupt"):
            spot, primary = Mock(), primary_client()
            spot.get.return_value = raw_book()
            if stage == "ppi_login":
                spot.login.side_effect = PPIError("http_failure_401")
            elif stage == "primary_login":
                primary.login.side_effect = PPIError("http_failure_403")
            elif stage in ("second_snapshot", "interrupt"):
                primary.snapshot.side_effect = [snapshot(), KeyboardInterrupt() if stage == "interrupt" else PPIError("sensitive_response_withheld")]
            with patch.object(monitor, "Client", return_value=spot), \
                    patch.object(monitor, "RemarketsClient", side_effect=RuntimeError() if stage == "construction" else None,
                                 return_value=primary), self.assertRaises((PPIError, RuntimeError, KeyboardInterrupt)):
                monitor.live_cycle(cfg, Mock(ready=True), 1, primary_credentials=Mock(ready=True))
            spot.close.assert_called_once()
            if stage != "construction":
                primary.close.assert_called_once()
            self.assertLessEqual(spot.get.call_count, 1)
            if spot.get.called:
                self.assertEqual(spot.get.call_args.args[1]["Type"], "ACCIONES")

    def test_demo_constructs_neither_client_reads_neither_credentials(self):
        from market_making.ppi.cli import main
        with patch.object(monitor, "Client", side_effect=AssertionError), \
                patch.object(monitor, "RemarketsClient", side_effect=AssertionError), \
                patch.object(monitor, "load_credentials", side_effect=AssertionError), \
                patch.object(monitor, "load_primary_credentials", side_effect=AssertionError), \
                patch("builtins.print"):
            self.assertEqual(main(["watch", "--demo", "--iterations", "1"]), 0)

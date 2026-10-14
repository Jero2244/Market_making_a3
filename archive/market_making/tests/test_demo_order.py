import copy
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import io
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import requests

from market_making.market_data.check_connection import ALLOWED_OPERATIONS, PrimaryError, request
from market_making.execution.demo_execution import DemoClient, HOST, MonitoringBlocked, cleanup, durable, lifecycle, quantity_evidence
from market_making.market_data.instrument_rules import SYMBOL, resolve, validate_detail
from market_making.market_data.order_book import parse_snapshot
from market_making.execution.smoke_demo_order import choose_price, diagnostic_preflight, final_send_gate, main, session_gate, timestamp_gate

NOW = datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc)
IDS = {"clOrdId": "demo123", "proprietary": "PBCP"}


def synthetic_book(data, at=NOW):
    """Offline gate mechanics ONLY, not a supported Primary timestamp adapter."""
    book = parse_snapshot(data, at, received_monotonic=0)
    return replace(book, quote_age=book.timestamp_age, clock_uncertainty=0, timestamp_authoritative=True)


def detail():
    return {"instrumentId": {"marketId": "ROFX", "symbol": SYMBOL}, "cficode": "FXXXSX",
            "maturityDate": "20261030", "minPriceIncrement": 100.0, "tickSize": 1,
            "minTradeVol": 1, "maxTradeVol": 1000, "roundLot": 1,
            "lowLimitPrice": 380000, "highLimitPrice": 510000,
            "orderTypes": ["LIMIT"], "timesInForce": ["DAY"],
            "tickPriceRanges": {"0": {"tick": 100}}}


def snapshot():
    return {"status": "OK", "instrumentId": {"marketId": "ROFX", "symbol": SYMBOL},
            "timestamp": NOW.isoformat(),
            "marketData": {"BI": [{"price": 440000, "size": 2}, {"price": 441000, "size": 1}],
                           "OF": [{"price": 444000, "size": 1}, {"price": 443000, "size": 3}]}}


def report(status="CANCELLED", cum="0", leaves="0"):
    return {**IDS, "status": status, "cumQty": cum, "leavesQty": leaves, "orderId": "123"}


class RulesTests(unittest.TestCase):
    def test_decimal_exact_tick_and_one(self):
        rules = validate_detail(detail(), NOW)
        self.assertEqual(rules.tick, Decimal(100))
        self.assertEqual(rules.validate_price("439000"), Decimal(439000))
        for p in ["439050", "NaN", "Infinity", 100, True]:
            with self.subTest(price=p), self.assertRaises(PrimaryError):
                rules.validate_price(p)

    def test_bad_metadata(self):
        changes = [{"minPriceIncrement": 0.1}, {"minPriceIncrement": "NaN"},
                   {"tickSize": 2}, {"minTradeVol": 2}, {"maxTradeVol": 0},
                   {"roundLot": 2}, {"cficode": "OCAFXS"}, {"maturityDate": "20261229"},
                   {"maturityDate": "20261002"}, {"timesInForce": ["IOC"]},
                   {"instrumentId": {"marketId": "ROFX", "symbol": "RFX20/DIC26"}},
                   {"tickPriceRanges": {"0": {"tick": 50}}}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(PrimaryError):
                validate_detail({**detail(), **change}, NOW)

    @patch("market_making.market_data.instrument_rules.get_json")
    def test_resolve_no_december_fallback(self, get):
        get.return_value = {"status": "OK", "instruments": [
            {"instrumentId": {"marketId": "ROFX", "symbol": "RFX20/DIC26"}, "cficode": "FXXXSX"}]}
        with self.assertRaises(PrimaryError):
            resolve(Mock(), now=NOW)
        self.assertEqual(get.call_count, 1)

    @patch("market_making.market_data.instrument_rules.get_json")
    def test_resolve_exact_live_pair(self, get):
        get.side_effect = [{"instruments": [detail()]}, {"instrument": detail()}]
        self.assertEqual(resolve(Mock(), now=NOW).symbol, SYMBOL)
        self.assertEqual(get.call_args.kwargs, {"marketId": "ROFX", "symbol": SYMBOL})


class BookTests(unittest.TestCase):
    def test_best_whole_snapshot_depth_and_spread(self):
        book = synthetic_book(snapshot())
        self.assertEqual(book.bid, 441000)
        self.assertEqual(book.ask, 443000)
        self.assertEqual(book.summary()["spread_ticks"], "20")
        book.gate(NOW, monotonic_now=0)
        with patch("market_making.market_data.order_book.datetime") as clock, patch("market_making.market_data.order_book.time.monotonic", return_value=0):
            clock.now.return_value = NOW
            self.assertEqual(choose_price(validate_detail(detail(), NOW), book), 440000)
        self.assertEqual(len(parse_snapshot({**snapshot(), "marketData": {"BI": [], "OF": []}}, NOW).bids), 0)

    def test_missing_crossed_stale_unknown(self):
        payloads = [snapshot() for _ in range(4)]
        payloads[0]["marketData"]["BI"] = []
        payloads[1]["marketData"]["OF"] = [{"price": 440000, "size": 1}]
        payloads[2]["timestamp"] = (NOW-timedelta(seconds=30)).isoformat()
        payloads[3].pop("timestamp")
        for data in payloads:
            with self.subTest(data=data), self.assertRaises(PrimaryError):
                parse_snapshot(data, NOW).gate(NOW)
        with self.assertRaises(PrimaryError):
            parse_snapshot(snapshot(), NOW).gate(NOW+timedelta(seconds=10))

    def test_invalid_levels_and_wrong_identity(self):
        for value in ["NaN", "Infinity", -1, 440050, True]:
            data = snapshot()
            data["marketData"]["BI"][0]["price"] = value
            with self.subTest(value=value), self.assertRaises(PrimaryError):
                parse_snapshot(data, NOW)
        for value in [0, -1, "NaN", 0.5, True]:
            data = snapshot()
            data["marketData"]["BI"][0]["size"] = value
            with self.subTest(size=value), self.assertRaises(PrimaryError):
                parse_snapshot(data, NOW)
        data = snapshot()
        data["instrumentId"]["symbol"] = "RFX20/DIC26"
        with self.assertRaises(PrimaryError):
            parse_snapshot(data, NOW)

    def test_no_identity_requires_exact_request_context(self):
        data = snapshot()
        data.pop("instrumentId")
        with self.assertRaises(PrimaryError):
            parse_snapshot(data, NOW)
        self.assertEqual(parse_snapshot(data, NOW, ("ROFX", SYMBOL)).state, "two_sided")


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.session._primary_secrets = ["private-token", "private-user", "private-pass"]
        self.response = Mock(status_code=200)
        self.response.json.return_value = {"status": "OK", "order": {"clientId": "demo123", "proprietary": "PBCP"}}
        self.session.request.return_value = self.response
        self.client = DemoClient(self.session, "REM123")

    def test_read_only_allowlist_unchanged(self):
        self.assertEqual(len(ALLOWED_OPERATIONS), 5)
        with self.assertRaises(PrimaryError):
            request(self.session, "GET", "/rest/order/newSingleOrder")
        self.session.request.assert_not_called()

    def test_fixed_host_no_redirects_no_retry_and_no_replacement(self):
        self.assertEqual(self.client.submit(440000), IDS)
        args, kwargs = self.session.request.call_args
        self.assertEqual(args, ("GET", HOST+"/rest/order/newSingleOrder"))
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["params"]["orderQty"], 1)
        self.assertEqual(kwargs["params"]["cancelPrevious"], "false")
        self.assertEqual(self.session.mount.call_args.args[1].max_retries.total, 0)
        with self.assertRaises(PrimaryError):
            self.client.call("/rest/order/replaceById")
        self.assertEqual(self.session.request.call_count, 1)

    def test_timeout_and_echo_not_exposed(self):
        self.session.request.side_effect = requests.Timeout("private-token")
        with self.assertRaises(PrimaryError) as exc:
            self.client.submit(440000)
        self.assertNotIn("private-token", str(exc.exception))
        self.assertEqual(self.session.request.call_count, 1)
        self.session.request.side_effect = None
        for echo in [{"text": "private-token"}, {"nested": {"password": "unknown"}},
                     {"private-pass": "key echo"}]:
            self.response.json.return_value = {"status": "OK", **echo}
            with self.assertRaises(PrimaryError):
                self.client.call("/rest/order/all", accountId="REM123")

    def test_http_redirect_api_failure(self):
        for status in [301, 302, 401, 403, 500]:
            self.response.status_code = status
            with self.assertRaises(PrimaryError):
                self.client.submit(440000)
        self.response.status_code = 200
        self.response.json.return_value = {"status": "ERROR", "text": "private-user"}
        with self.assertRaises(PrimaryError):
            self.client.submit(440000)

    def test_correlated_status(self):
        full = {**report(), "accountId": "REM123", "instrumentId": {"marketId": "ROFX", "symbol": SYMBOL},
                "side": "BUY", "ordType": "LIMIT", "timeInForce": "DAY", "price": 440000, "orderQty": 1}
        self.response.json.return_value = {"status": "OK", "order": full}
        self.assertEqual(self.client.status(IDS, 440000)["status"], "CANCELLED")
        for key, bad in [("accountId", "OTHER"), ("clOrdId", "other")]:
            self.response.json.return_value = {"status": "OK", "order": {**full, key: bad}}
            with self.subTest(key=key), self.assertRaises(PrimaryError):
                self.client.status(IDS, 440000)
        for key, bad in [("price", 440100), ("status", "UNKNOWN")]:
            self.response.json.return_value = {"status": "OK", "order": {**full, key: bad}}
            with self.subTest(key=key):
                self.assertTrue(self.client.status(IDS, 440000)["protocol_anomaly"])
        self.response.json.return_value = {"status": "OK", "order": {**full, "cumQty": "NaN", "status": "PARTIALLY_FILLED"}}
        observed = self.client.status(IDS, 440000)
        self.assertEqual(observed["status"], "PARTIALLY_FILLED")
        self.assertFalse(observed["quantities_valid"])
        self.assertIsNone(observed["cumQty"])

    def test_monitor_requires_history_and_account_access(self):
        self.response.json.return_value = {"status": "OK", "orders": []}
        with self.assertRaises(PrimaryError):
            self.client.monitor_ready()
        self.response.json.return_value = {"status": "OK", "orders": [{"accountId": "OTHER"}]}
        with self.assertRaises(PrimaryError):
            self.client.orders(active=True)

    def test_real_client_partial_zero_then_cancel_zero_cannot_pass(self):
        base = {**IDS, "accountId": "REM123", "instrumentId": {"marketId": "ROFX", "symbol": SYMBOL},
                "side": "BUY", "ordType": "LIMIT", "timeInForce": "DAY", "price": 440000,
                "orderQty": 1, "orderId": "123"}
        responses = [
            {"status": "OK"},
            {"status": "OK", "order": {**base, "status": "PARTIALLY_FILLED", "cumQty": 0, "leavesQty": 1}},
            {"status": "OK", "order": {**base, "status": "CANCELLED", "cumQty": 0, "leavesQty": 0}},
            {"status": "OK", "orders": []}]
        self.response.json.side_effect = responses
        with tempfile.TemporaryDirectory() as temp:
            journal = Path(temp)/"state.json"
            state = {"phase": "acknowledged"}
            self.assertFalse(cleanup(self.client, IDS, 440000, journal, state, sleep=lambda _: None))
            saved = json.loads(journal.read_text())
        self.assertEqual(saved["result"], "FAIL_FILL")
        self.assertTrue(saved["quantity_anomaly_observed"])
        self.assertTrue(saved["cleanup_proven"])
        paths = [call.args[1] for call in self.session.request.call_args_list]
        self.assertNotIn(HOST+"/rest/order/newSingleOrder", paths)


class RealClientMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = Path(self.temp.name)/"state.json"
        self.session = Mock()
        self.session._primary_secrets = ["private-token"]
        self.response = Mock(status_code=200)
        self.session.request.return_value = self.response
        self.client = DemoClient(self.session, "REM123")
        self.state = {"phase": "acknowledged", "ids": IDS, "price": "440000"}

    def full(self, status="CANCELLED", cum=0, leaves=0, **extra):
        return {**IDS, "accountId": "REM123", "instrumentId": {"marketId": "ROFX", "symbol": SYMBOL},
                "side": "BUY", "ordType": "LIMIT", "timeInForce": "DAY", "price": 440000,
                "orderQty": 1, "orderId": "123", "status": status, "cumQty": cum, "leavesQty": leaves, **extra}

    def transport(self, *reports):
        self.response.json.side_effect = [{"status": "OK"}] + [
            {"status": "OK", "order": r} for r in reports] + [{"status": "OK", "orders": []}]

    def run_cleanup(self, state=None, polls=5):
        return cleanup(self.client, IDS, 440000, self.journal,
                       self.state if state is None else state, polls=polls, sleep=lambda _: None)

    def test_malformed_optional_id_partial_and_filled_then_cancel_never_pass(self):
        for status, cum, leaves in [("PARTIALLY_FILLED", 0, 1), ("FILLED", 1, 0)]:
            for bad in ["malformed / id", {}, ["123"], False, ""]:
                self.state = {"phase": "acknowledged"}
                self.transport(self.full(status, cum, leaves, orderId=bad), self.full())
                with self.subTest(status=status, orderId=bad):
                    self.assertFalse(self.run_cleanup())
                    saved = json.loads(self.journal.read_text())
                    self.assertEqual(saved["result"], "FAIL_FILL")
                    self.assertTrue(saved["cleanup_proven"])
                    self.assertTrue(saved["protocol_anomaly_observed"])
                    self.assertEqual(saved["first_fill_report"]["status"], status)
                    self.assertIsNone(saved["first_fill_report"]["orderId"])
                    self.assertIn("OPTIONAL_ORDER_ID_INVALID", saved["first_fill_report"]["protocol_codes"])
        self.assertTrue(all(call.args[1] != HOST+"/rest/order/newSingleOrder"
                            for call in self.session.request.call_args_list))

    def test_analogous_intent_metadata_cannot_erase_correlated_fill(self):
        for change in [{"instrumentId": []}, {"side": []}, {"ordType": {}},
                       {"timeInForce": False}, {"price": {}}, {"orderQty": None}]:
            self.state = {"phase": "acknowledged"}
            self.transport(self.full("FILLED", 1, 0, **change), self.full())
            with self.subTest(change=change):
                self.assertFalse(self.run_cleanup())
                self.assertEqual(self.state["result"], "FAIL_FILL")
                self.assertTrue(self.state["protocol_anomaly_observed"])
                self.assertTrue(self.state["cleanup_proven"])

    def test_ignored_optional_fields_do_not_raise_or_erase_status(self):
        self.response.json.return_value = {"status": "OK", "order": self.full(
            "PARTIALLY_FILLED", 0, 1, execId={}, avgPx=[], transactTime=False, text={"irrelevant": []})}
        self.response.json.side_effect = None
        observed = self.client.status(IDS, 440000)
        self.assertEqual(observed["status"], "PARTIALLY_FILLED")
        for key in ["execId", "avgPx", "transactTime", "text"]:
            self.assertNotIn(key, observed)

    def test_transient_evidence_write_failure_then_restart_stays_failed(self):
        self.transport(self.full("PARTIALLY_FILLED", 0, 1, orderId={}), self.full())
        original = durable
        failed = False

        def disk(path, value, exclusive=False):
            nonlocal failed
            if value.get("fill_observed") and not failed:
                failed = True
                raise OSError("simulated storage failure")
            return original(path, value, exclusive)

        with patch("market_making.execution.demo_execution.durable", side_effect=disk):
            self.assertFalse(self.run_cleanup())
        restored = json.loads(self.journal.read_text())
        self.assertTrue(restored["fill_observed"])
        self.transport(self.full())
        self.assertFalse(self.run_cleanup(restored))
        self.assertEqual(restored["result"], "FAIL_FILL")
        self.assertTrue(restored["cleanup_proven"])

    def test_lost_fill_evidence_storage_failure_restart_never_passes(self):
        self.transport(self.full("FILLED", 1, 0, orderId={}), self.full())
        original = durable

        def disk(path, value, exclusive=False):
            if value.get("fill_observed"):
                raise OSError("persistent storage failure")
            return original(path, value, exclusive)

        with patch("market_making.execution.demo_execution.durable", side_effect=disk), self.assertRaises(OSError):
            self.run_cleanup(polls=1)
        restored = json.loads(self.journal.read_text())
        self.assertTrue(restored["reconciliation_in_progress"])
        self.assertNotIn("fill_observed", restored)
        self.transport(self.full())
        self.assertFalse(self.run_cleanup(restored))
        self.assertEqual(restored["result"], "FAIL_PROTOCOL_ANOMALY")
        self.assertEqual(restored["first_protocol_anomaly"]["code"], "RECOVERY_OBSERVATION_CONTINUITY_UNPROVEN")
        self.assertTrue(restored["cleanup_proven"])

    def test_sensitive_optional_echo_withheld_is_sticky_protocol_failure(self):
        self.transport(self.full("FILLED", 1, 0, orderId="private-token"), self.full())
        self.assertFalse(self.run_cleanup())
        saved_text = self.journal.read_text()
        self.assertNotIn("private-token", saved_text)
        self.assertEqual(self.state["result"], "FAIL_PROTOCOL_ANOMALY")
        self.assertEqual(self.state["first_protocol_anomaly"]["code"], "SENSITIVE_RESPONSE_WITHHELD")
        self.assertTrue(self.state["cleanup_proven"])

    def test_metadata_anomaly_without_fill_is_not_a_success(self):
        self.transport(self.full(orderId={}))
        self.assertFalse(self.run_cleanup())
        self.assertEqual(self.state["result"], "FAIL_PROTOCOL_ANOMALY")
        self.assertTrue(self.state["cleanup_proven"])

    def test_extreme_or_structural_quantities_cannot_erase_fill_status(self):
        for bad in ["1e-999999999999999999", "1e999999999999999999", "NaN", {}, [], True]:
            self.state = {"phase": "acknowledged"}
            self.transport(self.full("PARTIALLY_FILLED", bad, 1, orderId={}), self.full())
            with self.subTest(cum=bad):
                self.assertFalse(self.run_cleanup())
                self.assertEqual(self.state["result"], "FAIL_FILL")
                self.assertTrue(self.state["cleanup_proven"])


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = Path(self.temp.name)/"intent.json"
        self.client = Mock()
        self.client.submit.return_value = IDS
        self.client.status.side_effect = [report("NEW", leaves="1"), report()]
        self.client.orders.return_value = []
        self.no_sleep = lambda _: None

    def run_cycle(self, recheck=lambda: None):
        return lifecycle(self.client, Decimal(440000), self.journal, recheck, lambda: None, self.no_sleep)

    def state(self):
        return json.loads(self.journal.read_text())

    def test_success_intent_before_submit_and_no_duplicates(self):
        def submit(price):
            self.assertEqual(self.state()["phase"], "submit_intent")
            return IDS
        self.client.submit.side_effect = submit
        self.assertTrue(self.run_cycle())
        self.assertEqual(self.state()["result"], "PASS_CANCELLED_ZERO")
        self.client.submit.assert_called_once()
        self.client.cancel.assert_called_once_with(IDS)
        methods = [call[0] for call in self.client.method_calls]
        self.assertLess(methods.index("cancel"), methods.index("status"))
        with self.assertRaises(FileExistsError):
            self.run_cycle()
        self.assertEqual(self.client.submit.call_count, 1)

    def test_failed_recheck_no_submit(self):
        with self.assertRaises(PrimaryError):
            self.run_cycle(lambda: (_ for _ in ()).throw(PrimaryError("moved")))
        self.client.submit.assert_not_called()
        self.assertEqual(self.state()["phase"], "prepared")

    def test_uncertain_submit_no_retry(self):
        self.client.submit.side_effect = requests.Timeout("sensitive")
        self.assertFalse(self.run_cycle())
        self.assertEqual(self.state()["result"], "BLOCKED_UNCERTAIN_SUBMIT")
        self.assertNotIn("sensitive", self.journal.read_text())
        self.client.submit.assert_called_once()
        self.client.cancel.assert_not_called()

    def test_report_timeout_still_finally_cancels(self):
        self.client.status.side_effect = [PrimaryError("status timeout"), report()]
        self.assertFalse(self.run_cycle())
        self.assertEqual(self.state()["result"], "BLOCKED_EVIDENCE_GAP")
        self.client.cancel.assert_called_once_with(IDS)

    def test_interrupt_still_finally_cancels(self):
        self.client.status.side_effect = [KeyboardInterrupt(), report()]
        self.assertFalse(self.run_cycle())
        self.assertEqual(self.state()["result"], "BLOCKED_EVIDENCE_GAP")
        self.client.cancel.assert_called_once()

    def test_fill_or_rejection_never_passes(self):
        for initial in [report("PARTIALLY_FILLED", "1", "0"), report("REJECTED")]:
            if self.journal.exists():
                self.journal.unlink()
            self.client.status.side_effect = [initial, report()]
            self.assertFalse(self.run_cycle())
            self.assertTrue(self.state()["result"].startswith("FAIL_"))

    def test_cancel_timeout_requires_proof(self):
        self.client.cancel.side_effect = PrimaryError("timeout")
        self.client.status.side_effect = [report("NEW", leaves="1")] + [PrimaryError("timeout")]*5
        self.assertFalse(self.run_cycle())
        self.assertEqual(self.state()["result"], "BLOCKED_UNCERTAIN_CANCEL")
        self.client.submit.assert_called_once()

    def test_active_or_nonzero_leaves_cannot_pass(self):
        self.client.orders.return_value = [{**IDS, "accountId": "REM123"}]
        self.client.status.side_effect = None
        self.client.status.return_value = report()
        self.assertFalse(self.run_cycle())
        self.journal.unlink()
        self.client.orders.return_value = []
        self.client.status.return_value = report(leaves="1")
        self.assertFalse(self.run_cycle())

    def test_persist_ids_failure_still_cancels(self):
        original = durable
        failed = False

        def disk(path, value, exclusive=False):
            nonlocal failed
            if value.get("phase") == "acknowledged" and not failed:
                failed = True
                raise OSError("disk")
            return original(path, value, exclusive)

        self.client.status.side_effect = None
        self.client.status.return_value = report()
        with patch("market_making.execution.demo_execution.durable", side_effect=disk):
            self.assertFalse(self.run_cycle())
        self.client.cancel.assert_called_once()

    def test_cancel_only_cleanup_never_submits(self):
        state = {"phase": "acknowledged"}
        self.client.status.side_effect = None
        self.client.status.return_value = report()
        self.assertFalse(cleanup(self.client, IDS, 440000, self.journal, state, sleep=self.no_sleep))
        self.assertEqual(state["result"], "BLOCKED_ACCEPTANCE_UNPROVEN")
        self.assertTrue(state["cleanup_proven"])
        self.client.submit.assert_not_called()

    def test_cancelled_alone_never_proves_acceptance_or_clears_on_resume(self):
        self.client.status.side_effect = None
        self.client.status.return_value = report()
        self.assertFalse(self.run_cycle())
        state = self.state()
        self.assertEqual(state["result"], "BLOCKED_ACCEPTANCE_UNPROVEN")
        self.assertTrue(state["cleanup_proven"])
        self.assertFalse(cleanup(self.client, IDS, 440000, self.journal, state, sleep=self.no_sleep))
        self.client.submit.assert_called_once()

    def test_pending_new_is_not_acceptance(self):
        self.client.status.side_effect = [report("PENDING_NEW", leaves="1"), report()]
        self.assertFalse(self.run_cycle())
        self.assertEqual(self.state()["result"], "BLOCKED_ACCEPTANCE_UNPROVEN")

    def test_gap_after_new_is_sticky_across_resume(self):
        self.client.status.side_effect = [report("NEW", leaves="1"), PrimaryError("lost"), report()]
        self.assertFalse(self.run_cycle())
        state = self.state()
        self.assertTrue(state["acceptance_proven"])
        self.assertEqual(state["result"], "BLOCKED_EVIDENCE_GAP")
        self.client.status.side_effect = None
        self.client.status.return_value = report()
        self.assertFalse(cleanup(self.client, IDS, 440000, self.journal, state, sleep=self.no_sleep))
        self.assertEqual(state["result"], "BLOCKED_EVIDENCE_GAP")

    def test_fill_seen_during_transient_disk_error_cannot_later_pass(self):
        state = {"phase": "acknowledged"}
        self.client.status.side_effect = [report("PARTIALLY_FILLED", "1", "0"), report()]
        original = durable
        first = True

        def disk(path, value, exclusive=False):
            nonlocal first
            if first and value.get("fill_observed"):
                first = False
                raise OSError("disk")
            return original(path, value, exclusive)

        with patch("market_making.execution.demo_execution.durable", side_effect=disk):
            self.assertFalse(cleanup(self.client, IDS, 440000, self.journal, state, sleep=self.no_sleep))
        self.assertEqual(state["result"], "FAIL_FILL")

    def test_contradictory_partial_then_cancel_sticky_fail_and_persists_on_resume(self):
        self.client.status.side_effect = [report("PARTIALLY_FILLED", "0", "1"), report()]
        self.assertFalse(self.run_cycle())
        state = self.state()
        self.assertEqual(state["result"], "FAIL_FILL")
        self.assertTrue(state["fill_observed"])
        self.assertTrue(state["quantity_anomaly_observed"])
        self.assertEqual(state["fill_statuses_observed"], ["PARTIALLY_FILLED"])
        self.assertEqual(state["first_fill_report"]["cumQty"], "0")
        self.assertEqual(state["first_fill_report"]["leavesQty"], "1")
        self.assertEqual(state["first_quantity_anomaly"]["status"], "PARTIALLY_FILLED")
        self.assertTrue(state["cleanup_proven"])
        self.client.status.side_effect = None
        self.client.status.return_value = report()
        self.assertFalse(cleanup(self.client, IDS, 440000, self.journal, state, sleep=self.no_sleep))
        self.assertEqual(self.state()["result"], "FAIL_FILL")

    def test_fill_status_bad_quantities_never_disappear(self):
        for status in ["PARTIALLY_FILLED", "FILLED"]:
            for cum, leaves in [("0", "1"), ("NaN", "1"), ("1", "1"), ("0.5", "0.5")]:
                if self.journal.exists():
                    self.journal.unlink()
                self.client.status.side_effect = [report(status, cum, leaves), report()]
                with self.subTest(status=status, cum=cum, leaves=leaves):
                    self.assertFalse(self.run_cycle())
                    self.assertEqual(self.state()["result"], "FAIL_FILL")
                    self.assertTrue(self.state()["fill_observed"])

    def test_new_contradiction_then_zero_cancel_is_protocol_failure(self):
        self.client.status.side_effect = [report("NEW", "0", "0"), report()]
        self.assertFalse(self.run_cycle())
        self.assertEqual(self.state()["result"], "FAIL_QUANTITY_ANOMALY")
        self.assertTrue(self.state()["cleanup_proven"])

    def test_older_journal_partial_is_sticky_on_recovery(self):
        state = {"phase": "acknowledged", "last_report": report("PARTIALLY_FILLED", "0", "1")}
        self.client.status.side_effect = None
        self.client.status.return_value = report()
        self.assertFalse(cleanup(self.client, IDS, 440000, self.journal, state, sleep=self.no_sleep))
        self.assertEqual(state["result"], "FAIL_FILL")

    def test_state_quantity_combinations(self):
        valid = [("NEW", 0, 1), ("PENDING_NEW", 0, 1), ("PENDING_CANCEL", 0, 1),
                 ("FILLED", 1, 0), ("CANCELLED", 0, 0), ("CANCELLED", 1, 0),
                 ("REJECTED", 0, 0), ("EXPIRED", 0, 0)]
        invalid = [("NEW", 1, 0), ("NEW", 0, 0), ("PENDING_CANCEL", 0, 0),
                   ("PARTIALLY_FILLED", 0, 1), ("PARTIALLY_FILLED", 0.5, 0.5),
                   ("FILLED", 0, 0), ("FILLED", 1, 1), ("CANCELLED", 0, 1),
                   ("REJECTED", 1, 0), ("EXPIRED", 0, 1), ("NEW", "NaN", 1)]
        for state, cum, leaves in valid:
            with self.subTest(valid=(state, cum, leaves)):
                self.assertTrue(quantity_evidence(state, cum, leaves)["quantities_valid"])
        for state, cum, leaves in invalid:
            with self.subTest(invalid=(state, cum, leaves)):
                self.assertFalse(quantity_evidence(state, cum, leaves)["quantities_valid"])


class FinalSendGateTests(unittest.TestCase):
    def delayed_cycle(self, delay, start=NOW):
        with tempfile.TemporaryDirectory() as temp:
            journal = Path(temp)/"intent.json"
            evidence = Path(temp)/"session.json"
            evidence.write_text(json.dumps({"date": "2026-10-02", "trading_day": True,
                "remarkets_session_confirmed": True, "independently_reviewed": True,
                "server_snapshot_timestamp_verified": True}))
            data = snapshot()
            data["timestamp"] = start.isoformat()
            book = synthetic_book(data, start)
            current = [start]
            client = Mock()
            client.submit.return_value = IDS
            client.status.side_effect = [report("NEW", leaves="1"), report()]
            client.orders.return_value = []

            def slow_storage(path, state, exclusive=False):
                durable(path, state, exclusive)
                if state.get("phase") == "submit_intent":
                    current[0] += timedelta(seconds=delay)

            def final_check():
                self.assertEqual(json.loads(journal.read_text())["phase"], "submit_intent")
                final_send_gate(evidence, book)

            with patch("market_making.execution.demo_execution.durable", side_effect=slow_storage), \
                    patch("market_making.execution.smoke_demo_order.timestamp_gate"), \
                    patch("market_making.market_data.order_book.time.monotonic", side_effect=lambda: (current[0]-start).total_seconds()), \
                    patch("market_making.market_data.order_book.datetime") as book_clock:
                book_clock.now.side_effect = lambda *args: current[0]
                success = lifecycle(client, 440000, journal,
                    lambda: final_send_gate(evidence, book), final_check, sleep=lambda _: None)
            return success, client, json.loads(journal.read_text())

    def test_exactly_five_seconds_is_allowed(self):
        success, client, state = self.delayed_cycle(5)
        self.assertTrue(success)
        client.submit.assert_called_once()
        client.cancel.assert_called_once_with(IDS)
        self.assertTrue(state["acceptance_proven"])

    def test_over_five_seconds_and_six_second_storage_delay_abort_no_send(self):
        for delay in [5.000001, 6]:
            with self.subTest(delay=delay):
                success, client, state = self.delayed_cycle(delay)
                self.assertFalse(success)
                client.submit.assert_not_called()
                client.cancel.assert_not_called()
                client.status.assert_not_called()
                self.assertEqual(state["phase"], "aborted_no_send")
                self.assertEqual(state["orders_sent"], 0)
                self.assertEqual(state["result"], "BLOCKED_FINAL_SEND_GATE")
                self.assertNotIn("ids", state)

    def test_production_close_does_not_block_fresh_demo(self):
        # 16:59:59 BA passes preflight; +1s reaches excluded 17:00 boundary.
        start = datetime(2026, 10, 2, 19, 59, 59, tzinfo=timezone.utc)
        success, client, state = self.delayed_cycle(1, start)
        self.assertTrue(success)
        client.submit.assert_called_once()
        client.cancel.assert_called_once()


class CLITests(unittest.TestCase):
    def setup_cli(self, temp):
        client = Mock()
        client.submit.return_value = IDS
        client.status.side_effect = [report("NEW", leaves="1"), report()]
        client.orders.return_value = []
        patches = [patch("market_making.execution.smoke_demo_order.LOCK", Path(temp)/"lock.json"),
                   patch("market_making.execution.smoke_demo_order.read_review_evidence", return_value={"independently_reviewed": True}),
                   patch("market_making.execution.smoke_demo_order.load_dotenv"), patch("market_making.execution.smoke_demo_order.authenticate"),
                   patch("market_making.execution.smoke_demo_order.requests.Session"),
                   patch("market_making.execution.smoke_demo_order.DemoClient", return_value=client),
                   patch("market_making.execution.smoke_demo_order.preflight", return_value=(validate_detail(detail(), NOW),
                         parse_snapshot(snapshot(), NOW), Decimal(440000))),
                   patch("market_making.execution.smoke_demo_order.final_send_gate"),
                   patch("market_making.execution.smoke_demo_order.diagnostic_preflight", return_value={"status": "READY_FOR_INDEPENDENT_REVIEW",
                         "blockers": [], "orders_sent": 0, "mode": "read_only_diagnostic", "stages": {}}),
                   patch.dict("os.environ", {"PRIMARY_USER": "private-user", "PRIMARY_PASSWORD": "private-pass",
                                             "PRIMARY_ACCOUNT": "REM123"}),
                   patch("sys.stdout", new=io.StringIO())]
        for p in patches:
            started = p.start()
            if isinstance(started, Mock) and p.attribute == "Session":
                started.return_value.__enter__.return_value._primary_secrets = ["private-user", "private-pass", "private-token"]
            self.addCleanup(p.stop)
        return client

    def test_full_cli_preflight_never_submits_and_exclusive_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            client = self.setup_cli(temp)
            folder = Path(temp)/"preflight"
            argv = ["--preflight", "--evidence-dir", str(folder), "--session-evidence", "reviewed.json"]
            self.assertEqual(main(argv), 0)
            self.assertEqual(main(argv), 2)
            client.submit.assert_not_called()
            self.assertFalse((Path(temp)/"lock.json").exists())
            self.assertEqual(json.loads((folder/"preflight.json").read_text())["orders_sent"], 0)

    def test_full_cli_send_and_lock_release_only_terminal_proof(self):
        with tempfile.TemporaryDirectory() as temp:
            client = self.setup_cli(temp)
            folder = Path(temp)/"send"
            argv = ["--preflight", "--send", "--evidence-dir", str(folder), "--session-evidence", "reviewed.json"]
            self.assertEqual(main(argv), 0)
            client.submit.assert_called_once()
            self.assertFalse((Path(temp)/"lock.json").exists())
            text = (folder/"intent.json").read_text()
            self.assertEqual(json.loads(text)["result"], "PASS_CANCELLED_ZERO")
            for secret in ["private-user", "private-pass", "REM123"]:
                self.assertNotIn(secret, text)

    def test_full_cli_final_gate_abort_records_zero_and_releases_only_no_send_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            client = self.setup_cli(temp)
            folder = Path(temp)/"expired"
            with patch("market_making.execution.smoke_demo_order.final_send_gate", side_effect=PrimaryError("expired")):
                self.assertEqual(main(["--preflight", "--send", "--evidence-dir", str(folder),
                                      "--session-evidence", "reviewed.json"]), 2)
            client.submit.assert_not_called()
            client.cancel.assert_not_called()
            state = json.loads((folder/"intent.json").read_text())
            self.assertEqual(state["orders_sent"], 0)
            self.assertEqual(state["phase"], "aborted_no_send")
            self.assertFalse((Path(temp)/"lock.json").exists())

    def test_full_cli_uncertain_lock_blocks_second_send_and_idless_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            client = self.setup_cli(temp)
            client.submit.side_effect = PrimaryError("lost ack private-pass")
            folder = Path(temp)/"uncertain"
            argv = ["--preflight", "--send", "--evidence-dir", str(folder), "--session-evidence", "reviewed.json"]
            self.assertEqual(main(argv), 2)
            self.assertTrue((Path(temp)/"lock.json").exists())
            argv[argv.index(str(folder))] = str(Path(temp)/"second")
            self.assertEqual(main(argv), 2)
            self.assertEqual(main(["--cancel-only", "--evidence-dir", str(folder)]), 2)
            client.submit.assert_called_once()
            client.cancel.assert_not_called()
            self.assertNotIn("private-pass", (folder/"intent.json").read_text())

    def test_full_cli_cancel_only_valid_ids_never_submits(self):
        with tempfile.TemporaryDirectory() as temp:
            client = self.setup_cli(temp)
            client.status.side_effect = None
            client.status.return_value = report()
            folder = Path(temp)/"recover"
            folder.mkdir()
            durable(folder/"intent.json", {"phase": "acknowledged", "symbol": SYMBOL, "side": "BUY",
                    "qty": 1, "ordType": "LIMIT", "timeInForce": "DAY", "price": "440000", "ids": IDS}, True)
            durable(Path(temp)/"lock.json", {"evidence_dir": str(folder.resolve()),
                    "account_tag": hashlib.sha256(b"REM123").hexdigest()}, True)
            self.assertEqual(main(["--cancel-only", "--evidence-dir", str(folder)]), 2)
            client.submit.assert_not_called()
            client.cancel.assert_called_once_with(IDS)
            self.assertTrue((Path(temp)/"lock.json").exists())

    def test_default_no_network_no_env(self):
        with patch("market_making.execution.smoke_demo_order.requests.Session") as session, patch("market_making.execution.smoke_demo_order.load_dotenv") as env:
            with patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(main([]), 2)
            session.assert_not_called()
            env.assert_not_called()

    def test_session_evidence_requires_independent_timestamp_review(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/"session.json"
            data = {"date": "2026-10-02", "trading_day": True, "remarkets_session_confirmed": True,
                    "independently_reviewed": True, "server_snapshot_timestamp_verified": True}
            path.write_text(json.dumps(data))
            session_gate(path, NOW)
            for key in {"independently_reviewed"}:
                bad = copy.deepcopy(data)
                bad.pop(key)
                path.write_text(json.dumps(bad))
                with self.subTest(key=key), self.assertRaises(PrimaryError):
                    session_gate(path, NOW)
            data.pop("server_snapshot_timestamp_verified")
            path.write_text(json.dumps(data))
            session_gate(path, NOW)
            with self.assertRaises(PrimaryError):
                timestamp_gate(path)


class DiagnosticTests(unittest.TestCase):
    def missing_timestamp_book(self):
        data = snapshot()
        data.pop("timestamp")
        return parse_snapshot(data, datetime.now(timezone.utc))

    @patch("market_making.execution.smoke_demo_order.fetch_book")
    @patch("market_making.execution.smoke_demo_order.resolve")
    @patch("market_making.execution.smoke_demo_order.DemoClient")
    def test_missing_account_session_timestamp_still_discovers_all(self, constructor, resolve_mock, fetch):
        resolve_mock.return_value = validate_detail(detail(), NOW)
        fetch.return_value = self.missing_timestamp_book()
        session = Mock()
        result = diagnostic_preflight(session, "", None)
        resolve_mock.assert_called_once_with(session)
        fetch.assert_called_once_with(session)
        constructor.assert_not_called()
        self.assertEqual(result["stages"]["contract"]["status"], "PASS")
        self.assertEqual(result["stages"]["snapshot"]["status"], "PASS")
        self.assertIsNone(result["stages"]["snapshot"]["observation"]["quote_age_seconds"])
        for blocker in ["PRIMARY_ACCOUNT_MISSING_OR_INVALID", "INDEPENDENT_SAFETY_REVIEW_UNVERIFIED",
                        "SNAPSHOT_TIMESTAMP_SEMANTICS_UNVERIFIED", "EXCHANGE_SNAPSHOT_TIMESTAMP_MISSING_OR_INVALID",
                        "ORDER_STATUS_MONITORING_UNVERIFIED"]:
            self.assertIn(blocker, result["blockers"])
        self.assertEqual(result["orders_sent"], 0)

    @patch("market_making.execution.smoke_demo_order.fetch_book")
    @patch("market_making.execution.smoke_demo_order.resolve")
    @patch("market_making.execution.smoke_demo_order.DemoClient")
    def test_empty_history_records_blocked_alternative_never_writes(self, constructor, resolve_mock, fetch):
        resolve_mock.return_value = validate_detail(detail(), NOW)
        fetch.return_value = self.missing_timestamp_book()
        client = constructor.return_value
        client.orders.return_value = []
        client.monitor_ready.side_effect = MonitoringBlocked("history empty")
        result = diagnostic_preflight(Mock(), "REM123", None)
        self.assertEqual(result["stages"]["account_access"]["status"], "PASS")
        self.assertEqual(result["stages"]["active_orders"]["status"], "PASS")
        self.assertIn("EMPTY_HISTORY_MONITORING_UNVERIFIED", result["blockers"])
        self.assertIn("No verified", result["stages"]["monitoring"]["alternative"])
        client.submit.assert_not_called()
        client.cancel.assert_not_called()

    def test_no_auth_reports_remaining_gates_without_network(self):
        with patch("market_making.execution.smoke_demo_order.resolve") as resolve_mock, patch("market_making.execution.smoke_demo_order.fetch_book") as fetch:
            result = diagnostic_preflight(None, "", None)
        resolve_mock.assert_not_called()
        fetch.assert_not_called()
        self.assertIn("AUTHENTICATION_UNAVAILABLE", result["blockers"])
        self.assertIn("PRIMARY_ACCOUNT_MISSING_OR_INVALID", result["blockers"])
        self.assertIn("INDEPENDENT_SAFETY_REVIEW_UNVERIFIED", result["blockers"])

    @patch("market_making.execution.smoke_demo_order.fetch_book")
    @patch("market_making.execution.smoke_demo_order.resolve")
    @patch("market_making.execution.smoke_demo_order.session_gate")
    @patch("market_making.execution.smoke_demo_order.timestamp_gate")
    def test_attestation_does_not_invent_missing_timestamp(self, timestamp, session_gate_mock, resolve_mock, fetch):
        resolve_mock.return_value = validate_detail(detail(), NOW)
        fetch.return_value = self.missing_timestamp_book()
        result = diagnostic_preflight(Mock(), "", Path("reviewed.json"))
        self.assertEqual(result["stages"]["timestamp_semantics"]["status"], "PASS")
        self.assertIn("EXCHANGE_SNAPSHOT_TIMESTAMP_MISSING_OR_INVALID", result["blockers"])
        self.assertEqual(result["status"], "BLOCKED")


if __name__ == "__main__":
    unittest.main()

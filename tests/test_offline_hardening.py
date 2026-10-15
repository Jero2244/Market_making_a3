import copy
from decimal import Decimal
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import requests
import os
import subprocess
import sys
from market_making.http_policy import (MAX_JSON_BYTES, PrimaryError, READ_OPERATIONS,
    fixed_request, response_json, strict_json)
from market_making.market_data.check_connection import safe_payload
from market_making.market_data.check_connection import main as connection_main
from market_making.execution.demo_execution import durable
from market_making.execution.journal import context, journal_lease, read_journal, validate_recovery, release_owned_lock
from market_making.execution.execution_state import OfflineLedger, initial_state, reduce_event, reconnect
from market_making.execution.reconciliation import assess_evidence, validate_envelope
from market_making.validation.offline_readiness import inventory, main


def event(sequence=1, status="NEW", cum="0", leaves="10", event_id=None):
    return {"order_key": "synthetic-order", "event_id": event_id or f"e{sequence}",
            "sequence": sequence, "status": status, "total": "10", "cum": cum, "leaves": leaves}


class HttpTests(unittest.TestCase):
    def test_exact_numbers_and_duplicate_rejection(self):
        value = strict_json(b'{"a":0.10000000000000000001,"b":9007199254740993}')
        self.assertEqual(value["a"], Decimal("0.10000000000000000001"))
        self.assertEqual(value["b"], 9007199254740993)
        for raw in [b'{"status":"OK","status":"FAIL"}', b'{"a":{"b":1,"b":2}}',
                    b'{"a":NaN}', b'{"a":Infinity}', b'{"a":1e1001}', b'\xff',
                    b'[' * 34 + b'0' + b']' * 34, b'0' * (MAX_JSON_BYTES + 1)]:
            with self.subTest(raw=raw[:40]), self.assertRaises(PrimaryError):
                strict_json(raw)

    def test_body_limit_and_close_on_errors(self):
        response = Mock()
        response.iter_content.return_value = iter([b'x' * 65536] * 33)
        with self.assertRaises(PrimaryError):
            response_json(response)
        response.close.assert_called_once()

    def test_body_deadline(self):
        response = Mock()
        response.iter_content.return_value = [b'{"status":"OK"}']
        with patch("market_making.http_policy.time.monotonic", side_effect=[0, 31]):
            with self.assertRaises(PrimaryError):
                response_json(response)
        response.close.assert_called_once()
        response = Mock()
        response.iter_content.side_effect = requests.Timeout("SECRET")
        with self.assertRaises(PrimaryError) as error:
            response_json(response)
        self.assertNotIn("SECRET", str(error.exception))
        response.close.assert_called_once()

    def test_policy_and_no_retries_or_overrides(self):
        session = Mock()
        session.request.return_value.status_code = 200
        fixed_request(session, "GET", "/rest/instruments/all", operations=READ_OPERATIONS)
        self.assertFalse(session.trust_env)
        self.assertTrue(session.verify)
        self.assertEqual(session.mount.call_args.args[1].max_retries.total, 0)
        kw = session.request.call_args.kwargs
        self.assertEqual(kw["timeout"], (5, 10))
        self.assertTrue(kw["verify"])
        self.assertTrue(kw["stream"])
        self.assertFalse(kw["allow_redirects"])
        for kwargs in [{"verify": False}, {"proxies": {}}, {"timeout": None}, {"allow_redirects": True}]:
            with self.assertRaises(PrimaryError):
                fixed_request(session, "GET", "/rest/instruments/all", operations=READ_OPERATIONS, **kwargs)
        session.request.side_effect = requests.Timeout("SECRET")
        with self.assertRaises(PrimaryError):
            fixed_request(session, "GET", "/rest/instruments/all", operations=READ_OPERATIONS)
        self.assertEqual(session.request.call_count, 2)

    def test_nested_secret_keys_and_values_withheld(self):
        for value in [{"data": ["abcSECRETdef"]}, {"SECRET": 1},
                      {"data": {"X-Auth-Token": "unknown"}}, {"Authorization": "unknown"},
                      {"client_secret": "unknown"}, {"api-key": "unknown"}]:
            self.assertFalse(safe_payload(value, ("SECRET",)))
        self.assertTrue(safe_payload({"quantity": "1"}, ("SECRET",)))


class JournalTests(unittest.TestCase):
    def test_version_context_ids_and_intent(self):
        expected = context("a" * 64, "synthetic")
        state = {"schema_version": 1, "context": expected, "phase": "acknowledged",
                 "symbol": "RFX20/OCT26", "side": "BUY", "qty": 1, "ordType": "LIMIT",
                 "timeInForce": "DAY", "price": "440000", "ids": {"clOrdId": "a", "proprietary": "b"}}
        self.assertEqual(validate_recovery(state, expected), state["ids"])
        changes = [{"schema_version": 2}, {"schema_version": True}, {"context": {}},
                   {"phase": "submit_intent"}, {"qty": True}, {"price": "-100"},
                   {"price": "NaN"}, {"ids": None}, {"ids": {"clOrdId": "guess"}}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(PrimaryError):
                validate_recovery({**state, **change}, expected)

    def test_lease_concurrency_and_release(self):
        with tempfile.TemporaryDirectory() as folder:
            with journal_lease(folder):
                with self.assertRaises(PrimaryError):
                    with journal_lease(folder):
                        self.fail("concurrent writer allowed")
            with journal_lease(folder):
                pass
            self.assertTrue((Path(folder) / "journal.guard").exists())

    def test_process_crash_releases_os_lease_without_deleting_guard(self):
        with tempfile.TemporaryDirectory() as folder:
            program = "import os,sys; from market_making.execution.journal import journal_lease; lease=journal_lease(sys.argv[1]); lease.__enter__(); os._exit(0)"
            result = subprocess.run([sys.executable, "-c", program, folder], timeout=15, capture_output=True)
            self.assertEqual(result.returncode, 0)
            self.assertTrue((Path(folder) / "journal.guard").exists())
            with journal_lease(folder):
                pass

    def test_storage_failure_preserves_previous_and_temporary_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "intent.json"
            durable(path, {"old": 1}, True)
            with patch("market_making.execution.demo_execution.os.replace", side_effect=OSError()):
                with self.assertRaises(OSError):
                    durable(path, {"new": 2})
            self.assertEqual(read_journal(path), {"old": 1})
            self.assertEqual(read_journal(path.with_suffix(".json.tmp")), {"new": 2})
            with self.assertRaises(FileExistsError):
                durable(path, {"new": 3})
            self.assertEqual(read_journal(path), {"old": 1})

    def test_unowned_lock_never_deleted(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "lock.json"
            durable(path, {"owner": "other"}, True)
            with self.assertRaises(PrimaryError):
                release_owned_lock(path, {"owner": "ours"})
            self.assertEqual(read_journal(path), {"owner": "other"})


class ExecutionStateTests(unittest.TestCase):
    def test_duplicates_partial_fill_and_reordering(self):
        state = reduce_event(initial_state("synthetic-order"), event())
        partial = event(3, "PARTIALLY_FILLED", "4", "6")
        state = reduce_event(state, partial)
        self.assertEqual(reduce_event(state, partial), state)
        state = reduce_event(state, event(2, "PENDING_CANCEL"))
        self.assertEqual(state["cum"], "4")
        self.assertEqual(state["status"], "PARTIALLY_FILLED")
        self.assertIn("SEQUENCE_GAP_OBSERVED", state["blockers"])

    def test_cancel_fill_race_and_conflicts_preserve_exposure(self):
        state = reduce_event(initial_state("synthetic-order"), event(1, "PENDING_CANCEL"))
        state = reduce_event(state, event(2, "FILLED", "10", "0"))
        state = reduce_event(state, event(3, "CANCELLED", "0", "0"))
        self.assertEqual(state["cum"], "10")
        self.assertEqual(state["status"], "FILLED")
        self.assertIn("TERMINAL_STATE_CONFLICT", state["blockers"])
        self.assertIn("CUMULATIVE_FILL_REGRESSION", state["blockers"])
        state = reduce_event(state, event(3, "FILLED", "10", "0", "conflict"))
        self.assertIn("EVENT_ID_OR_SEQUENCE_CONFLICT", state["blockers"])
        self.assertTrue(state["uncertain"])

    def test_reject_terminal_regression_and_reconnect(self):
        state = reduce_event(initial_state("synthetic-order"), event(1, "REJECTED", "0", "0"))
        state = reduce_event(state, event(2))
        self.assertEqual(state["status"], "REJECTED")
        self.assertIn("TERMINAL_STATE_REGRESSION", state["blockers"])
        self.assertIn("RECONNECT_RECONCILIATION_REQUIRED", reconnect(state)["blockers"])

    def test_invalid_events_fail_closed_without_mutating_state(self):
        state = initial_state("synthetic-order")
        original = copy.deepcopy(state)
        for change in [{"cum": "NaN"}, {"cum": 0.1}, {"leaves": "11"},
                       {"status": "UNKNOWN"}, {"status": []}, {"sequence": True}, {"password": "SECRET"}]:
            with self.assertRaises(PrimaryError):
                reduce_event(state, {**event(), **change})
        self.assertEqual(state, original)

    def test_ledger_restart_concurrency_and_interrupted_write(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = OfflineLedger(Path(folder) / "ledger.json", "synthetic-order")
            self.assertFalse(ledger.path.exists())
            ledger.record(event())
            ledger.record(event(2, "PARTIALLY_FILLED", "2", "8"))
            self.assertEqual(ledger.load()["cum"], "2")
            self.assertIn("RESTART_RECONCILIATION_REQUIRED", ledger.load()["blockers"])
            with journal_lease(folder), self.assertRaises(PrimaryError):
                ledger.record(event(3, "FILLED", "10", "0"))
            with patch("market_making.execution.demo_execution.os.replace", side_effect=OSError()):
                with self.assertRaises(PrimaryError):
                    ledger.record(event(3, "FILLED", "10", "0"))
            with self.assertRaises(PrimaryError):
                ledger.load()


class ReadinessTests(unittest.TestCase):
    def test_offline_default_no_network_environment_or_state(self):
        with patch("requests.sessions.Session.request", side_effect=AssertionError("network forbidden")), \
             patch("market_making.market_data.check_connection.load_dotenv", side_effect=AssertionError("env forbidden")), \
             patch.object(Path, "open", side_effect=AssertionError("file access forbidden")), \
             patch("sys.stdout", new=io.StringIO()) as output:
            self.assertEqual(main([]), 2)
            report = json.loads(output.getvalue())
        self.assertEqual(report["status"], "BLOCKED")
        self.assertFalse(report["mutations_enabled"])
        self.assertEqual({b["category"] for b in report["blockers"]},
                         {"missing_code", "missing_broker_evidence", "operational_approval"})

    def test_connection_default_no_credentials_network_or_state(self):
        with patch("requests.sessions.Session.request", side_effect=AssertionError("network forbidden")), \
             patch("market_making.market_data.check_connection.load_dotenv", side_effect=AssertionError("env forbidden")), \
             patch("market_making.market_data.check_connection.os.getenv", side_effect=AssertionError("credentials forbidden")), \
             patch.object(Path, "open", side_effect=AssertionError("state forbidden")), \
             patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(connection_main([]), 2)

    def test_present_reconciliation_fixtures_never_confer_authority(self):
        for evidence in [{}, {k: {"complete": True, "rows": []} for k in
                               ("active_orders", "order_history", "positions", "balances", "margin")}]:
            report = assess_evidence(evidence)
            self.assertEqual(report["status"], "BLOCKED")
            self.assertFalse(report["mutations_permitted"])
            self.assertEqual(report["unrelated_orders_cancelled"], 0)

    def test_separate_context_bound_reconciliation_contracts(self):
        capture = {"environment": "REMARKETS", "host": "https://api.remarkets.primary.com.ar",
                   "account_tag": "a" * 64, "capture_id": "synthetic-capture"}
        evidence = {kind: {"schema_version": 1, "kind": kind, "context": capture,
                    "complete": True, "source_ref": "synthetic-doc", "rows": []}
                    for kind in ("active_orders", "order_history", "positions", "balances", "margin")}
        evidence["positions"]["rows"] = [{"symbol": "synthetic", "quantity": "-1"}]
        evidence["balances"]["rows"] = [{"currency": "ARS", "amount": "1.01"}]
        evidence["margin"]["rows"] = [{"currency": "ARS", "amount": "0"}]
        report = assess_evidence(evidence, capture)
        self.assertEqual(report["status"], "BLOCKED")
        self.assertTrue(all(v == "NORMALIZED_FIXTURE_VALID_NOT_BROKER_AUTHORITY" for v in report["observations"].values()))
        for change in [{"context": {**capture, "capture_id": "other"}}, {"complete": False},
                       {"rows": [{"symbol": "synthetic", "quantity": "NaN"}]}]:
            with self.assertRaises(PrimaryError):
                validate_envelope({**evidence["positions"], **change}, "positions", capture)

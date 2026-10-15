"""Adversarial synthetic contracts; these tests never establish broker authority."""
from dataclasses import asdict, replace
from pathlib import Path
import tempfile
import threading
import subprocess
import sys
import unittest
from unittest.mock import patch

from market_making.http_policy import PrimaryError
from market_making.execution.offline_contracts import (
    Identity, Clock, Book, Freshness, Metadata, Fill, WorkingOrder, HistoricalOrder, AccountEvidence, digest)
from market_making.execution.offline_risk import Policy, propose, check, validate_approval, policy_from_mapping
from market_making.execution.offline_coordinator import SimulationCoordinator


def fixture():
    identity = Identity("OFFLINE_SIMULATION", "account-1", "TEST", "spec-v1")
    now = Clock(1000, 1000, 1, 10000)
    book = Book(identity, 1, 1, True, "synthetic_full_book_source_utc_v1",
                "99", "101", 990, 990, now)
    metadata = Metadata(identity, "ARS", "contracts", "1", "1", "1", "1", "200", "10")
    evidence = AccountEvidence(identity, "snapshot-1", 990, now, metadata, "0", "0", "100",
                               "10000", "1000", "0", "0", (), (), (),
                               ("positions", "balances", "margin", "working_orders", "history", "fills", "metadata"), True)
    policy = Policy(1, identity, 9000, "10", "1000", "1000", "1000", "10", "1000",
                    "100", "100", 5, 1000, 100, False, False)
    approval = {"version": 1, "scope": "offline_simulation", "policy_hash": digest(asdict(policy)),
                "expires_utc": 8000, "requester": "requester", "reviewer": "independent-fixture-reviewer",
                "identity": asdict(identity), "production_enabled": False}
    freshness = Freshness(identity, 100)
    freshness.accept(book, now)
    proposal = propose(book, evidence, policy, approval, now, "BUY", "6", "99", 2000)
    return identity, now, book, evidence, policy, approval, freshness, proposal


def event(broker_id, seq, status, cum, leaves, total="6"):
    return {"order_key": broker_id, "event_id": "event-" + str(seq), "sequence": seq,
            "status": status, "total": total, "cum": cum, "leaves": leaves}


class FreshnessTests(unittest.TestCase):
    def test_baseline_uncertainty_future_both_sides_and_identity(self):
        identity, now, book, *_ = fixture()
        bad = [replace(book, bid_utc=None), replace(book, bid_utc=1001),
               replace(book, schema_version=True),
               replace(book, ask_utc=800), replace(book, bid_utc=800),
               replace(book, identity=replace(identity, instrument="OTHER")),
               replace(book, semantics="unverified"), replace(book, sequence=2),
               replace(book, received=replace(now, valid_until=999)),
               replace(book, received=replace(now, uncertainty=99))]
        for item in bad:
            with self.subTest(item=item), self.assertRaises((PrimaryError, TypeError)):
                Freshness(identity, 100).accept(item, now)

    def test_gap_conflicting_duplicate_rollback_and_reconnect(self):
        for mutation in ({"sequence": 3}, {"sequence": 1, "bid": "98"},
                         {"sequence": 2, "ask_utc": 989}, {"sequence": 0}):
            identity, now, book, _, _, _, fresh, _ = fixture()
            self.assertEqual(fresh.accept(book, now), book)
            with self.assertRaises(PrimaryError):
                fresh.accept(replace(book, baseline=False, **mutation), now)
            with self.assertRaises(PrimaryError):
                fresh.current(now)
        fresh.disconnect()
        with self.assertRaises(PrimaryError):
            fresh.accept(book, now)
        fresh.accept(replace(book, generation=2), now)
        self.assertEqual(fresh.current(now).generation, 2)

    def test_clock_rollback_disagreement_expiry_and_stale_current(self):
        identity, now, book, _, _, _, fresh, _ = fixture()
        for clock in (replace(now, utc=999), replace(now, mono=999),
                      replace(now, utc=1010), replace(now, utc=1100, mono=1100),
                      replace(now, valid_until=999)):
            with self.subTest(clock=clock), self.assertRaises(PrimaryError):
                book.validate(identity, clock, 100)
        with self.assertRaises(PrimaryError):
            fresh.current(replace(now, utc=1100, mono=1100))


class EvidenceRiskTests(unittest.TestCase):
    def test_incomplete_wrong_identity_units_stale_and_position_conflict(self):
        identity, now, _, evidence, *_ = fixture()
        bad = [replace(evidence, identity=replace(identity, account="other")),
               replace(evidence, schema_version=2),
               replace(evidence, complete_kinds=("positions",)),
               replace(evidence, consistent_snapshot=False), replace(evidence, source_utc=800),
               replace(evidence, position="1"),
               replace(evidence, metadata=replace(evidence.metadata, unit="unknown")),
               replace(evidence, metadata=replace(evidence.metadata, currency=""))]
        for item in bad:
            with self.subTest(item=item), self.assertRaises(PrimaryError):
                item.validate(identity, now, 100)

    def test_fill_dedup_and_conflict(self):
        identity, now, _, evidence, *_ = fixture()
        fill = Fill("exec-1", "broker-1", "BUY", "2")
        evidence = replace(evidence, history_ids=("broker-1",), fills=(fill, fill), position="2",
                           historical=(HistoricalOrder("broker-1", "BUY", "6", "PARTIALLY_FILLED", "2"),),
                           working=(WorkingOrder("broker-1", "BUY", "4", "99", False),))
        evidence.validate(identity, now, 100)
        with self.assertRaises(PrimaryError):
            replace(evidence, fills=(fill, replace(fill, quantity="3"))).validate(identity, now, 100)
        with self.assertRaises(PrimaryError):
            replace(evidence, history_ids=()).validate(identity, now, 100)

    def test_approval_and_config_fail_closed(self):
        _, now, book, evidence, policy, approval, _, proposal = fixture()
        self.assertEqual(policy_from_mapping(asdict(policy), evidence.identity, now), policy)
        for config in ({}, {**asdict(policy), "extra": 1}, {**asdict(policy), "version": True},
                       {**asdict(policy), "identity": {"account": "other"}},
                       {**asdict(policy), "production_enabled": True}):
            with self.assertRaises(PrimaryError):
                policy_from_mapping(config, evidence.identity, now)
        for item in ({}, {**approval, "extra": 1}, {**approval, "expires_utc": 900},
                     {**approval, "scope": "production"}, {**approval, "policy_hash": "changed"},
                     {**approval, "reviewer": approval["requester"]}):
            with self.subTest(approval=item), self.assertRaises(PrimaryError):
                validate_approval(item, policy, now)
        for item in (replace(policy, kill_switch=True), replace(policy, production_enabled=True),
                     replace(policy, expires_utc=900), replace(policy, max_quantity="NaN"),
                     replace(policy, max_quantity="5")):
            with self.subTest(policy=item), self.assertRaises(PrimaryError):
                check(proposal, book, evidence, item, approval, now, (), 0)

    def test_exact_ticks_lots_exposure_cash_margin_loss_rate_binding_expiry(self):
        _, now, book, evidence, policy, approval, _, proposal = fixture()
        for q, p in (("0", "99"), ("1.5", "99"), ("1", "99.5"), ("1", "101"), ("11", "99")):
            with self.subTest(q=q, p=p), self.assertRaises(PrimaryError):
                propose(book, evidence, policy, approval, now, "BUY", q, p, 2000)
        for item in (replace(evidence, cash="1"), replace(evidence, margin_available="1"),
                     replace(evidence, loss="100"), replace(evidence, drawdown="100")):
            bound = replace(proposal, account_hash=digest(asdict(item)))
            with self.assertRaises(PrimaryError):
                check(bound, book, item, policy, approval, now, (), 0)
        for reservations, attempts in ((("BUY", "5", "99"),), 0), ((), 5):
            with self.assertRaises(PrimaryError):
                check(proposal, book, evidence, policy, approval, now, reservations, attempts)
        with self.assertRaises(PrimaryError):
            check(replace(proposal, expires_utc=999), book, evidence, policy, approval, now, (), 0)
        with self.assertRaises(PrimaryError):
            check(proposal, replace(book, sequence=2), evidence, policy, approval, now, (), 0)


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "shared-account.sqlite"
        (self.identity, self.now, self.book, self.evidence, self.policy,
         self.approval, self.fresh, self.proposal) = fixture()
        self.coordinator = SimulationCoordinator(self.path, self.identity)
        self.token = self.coordinator.acquire("worker-1")
        self.coordinator.reconcile(self.token, self.evidence, self.now, 100)

    def tearDown(self):
        self.coordinator.close()
        self.temp.cleanup()

    def reserve(self, name="intent-1", coordinator=None, token=None):
        return (coordinator or self.coordinator).reserve(token or self.token, name, self.proposal,
                 self.fresh, self.evidence, self.policy, self.approval, self.now)

    def dispatch(self, name="intent-1", token=None):
        return self.coordinator.dispatch(token or self.token, name, self.proposal, self.fresh,
                                        self.evidence, self.policy, self.approval, self.now)

    def test_durable_before_dispatch_lost_ack_single_attempt_cancel_ack_retains(self):
        self.reserve()
        with self.assertRaises(PrimaryError):
            self.reserve()
        result = self.dispatch()
        self.assertEqual(result["attempts"], 1)
        self.assertFalse(result["network_used"])
        with self.assertRaises(PrimaryError):
            self.dispatch()
        e = event("broker-1", 1, "CANCELLED", "0", "0")
        self.coordinator.report(self.token, "intent-1", "broker-1", e, self.now)
        self.assertEqual(len(self.coordinator.reservations()), 1)
        with self.assertRaises(PrimaryError):
            self.reserve("intent-2")
        with self.assertRaises(PrimaryError):
            self.coordinator.resolve(self.token, "intent-1", self.evidence, [e], self.now, 100)

    def test_partial_fill_dedup_cancel_fill_race_and_complete_resolution(self):
        self.reserve()
        self.dispatch()
        first = event("broker-1", 1, "PARTIALLY_FILLED", "2", "4")
        cancelled = event("broker-1", 2, "CANCELLED", "2", "0")
        fill_race = event("broker-1", 3, "FILLED", "6", "0")
        for e in (first, first, cancelled, fill_race):
            state = self.coordinator.report(self.token, "intent-1", "broker-1", e, self.now)
        self.assertEqual(state["cum"], "6")
        self.assertEqual(len(state["events"]), 3)
        self.assertIn("TERMINAL_STATE_CONFLICT", state["blockers"])
        filled = replace(self.evidence, position="6", history_ids=("broker-1",),
                         fills=(Fill("exec-1", "broker-1", "BUY", "6"),),
                         historical=(HistoricalOrder("broker-1", "BUY", "6", "FILLED", "6"),))
        with self.assertRaises(PrimaryError):
            self.coordinator.resolve(self.token, "intent-1", filled, [first, cancelled, fill_race], self.now, 100)
        self.assertEqual(len(self.coordinator.reservations()), 1)

    def test_complete_history_resolution_and_reconciliation_required(self):
        self.reserve()
        self.dispatch()
        new = event("broker-1", 1, "NEW", "0", "6")
        partial = event("broker-1", 2, "PARTIALLY_FILLED", "2", "4")
        cancelled = event("broker-1", 3, "CANCELLED", "2", "0")
        for e in (new, partial, cancelled):
            self.coordinator.report(self.token, "intent-1", "broker-1", e, self.now)
        fill = Fill("exec-1", "broker-1", "BUY", "2")
        evidence = replace(self.evidence, position="2", history_ids=("broker-1",), fills=(fill, fill),
                           historical=(HistoricalOrder("broker-1", "BUY", "6", "CANCELLED", "2"),))
        with self.assertRaises(PrimaryError):
            self.coordinator.resolve(self.token, "intent-1", evidence, [partial, cancelled], self.now, 100)
        self.coordinator.resolve(self.token, "intent-1", evidence, [new, partial, cancelled], self.now, 100)
        self.assertEqual(self.coordinator.reservations(), [])
        with self.assertRaises(PrimaryError):
            self.reserve("intent-2")

    def test_takeover_isolation_stale_epoch_crash_uncertainty(self):
        self.reserve()
        with self.assertRaises(PrimaryError):
            self.coordinator.acquire("worker-2")
        token = self.coordinator.acquire("worker-2", old_dispatch_isolated=True)
        with self.assertRaises(PrimaryError):
            self.dispatch()
        with self.assertRaises(PrimaryError):
            self.dispatch(token=token)
        with self.assertRaises(PrimaryError):
            self.reserve("intent-2", token=token)
        self.coordinator.reconcile(token, self.evidence, self.now, 100)
        with self.assertRaises(PrimaryError):
            self.reserve("intent-2", token=token)  # retained unknown capacity
        self.assertEqual(self.coordinator.db.execute("SELECT state FROM intents").fetchone()[0], "UNKNOWN")

    def test_reopened_connection_cannot_dispatch_old_token_and_disconnect_blocks(self):
        self.reserve()
        reopened = SimulationCoordinator(self.path, self.identity)
        try:
            with self.assertRaises(PrimaryError):
                reopened.dispatch(self.token, "intent-1", self.proposal, self.fresh, self.evidence,
                                  self.policy, self.approval, self.now)
        finally:
            reopened.close()
        self.coordinator.disconnect(self.token, self.now)
        with self.assertRaises(PrimaryError):
            self.dispatch()
        self.assertEqual(self.coordinator.db.execute("SELECT state FROM intents").fetchone()[0], "UNKNOWN")

    def test_competing_connections_atomic_reservation_capacity(self):
        barrier = threading.Barrier(2)
        results = []
        def worker(name):
            c = SimulationCoordinator(self.path, self.identity)
            try:
                barrier.wait()
                self.reserve(name, coordinator=c)
                results.append("reserved")
            except PrimaryError:
                results.append("blocked")
            finally:
                c.close()
        threads = [threading.Thread(target=worker, args=("intent-" + str(i),)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertCountEqual(results, ["reserved", "blocked"])
        self.assertEqual(len(self.coordinator.reservations()), 1)

    def test_final_recheck_kill_switch_stale_book_and_account_binding(self):
        self.reserve()
        self.fresh.disconnect()
        with self.assertRaises(PrimaryError):
            self.dispatch()
        self.fresh.accept(replace(self.book, generation=2), self.now)
        with self.assertRaises(PrimaryError):
            self.dispatch()  # changed generation invalidates bound proposal
        self.assertIsNone(self.coordinator.db.execute("SELECT attempt_utc FROM intents").fetchone()[0])

    def test_unsupported_correction_sticky_quarantine(self):
        self.reserve()
        self.dispatch()
        correction = event("broker-1", 1, "REPLACED", "0", "6")
        with self.assertRaises(PrimaryError):
            self.coordinator.report(self.token, "intent-1", "broker-1", correction, self.now)
        self.assertEqual(self.coordinator.db.execute("SELECT state FROM intents").fetchone()[0], "QUARANTINED")
        self.coordinator.acquire("new-worker", old_dispatch_isolated=True)
        self.assertEqual(self.coordinator.db.execute("SELECT state FROM intents").fetchone()[0], "QUARANTINED")

    def test_actual_process_crash_retains_unknown_and_never_resends(self):
        crash_path = Path(self.temp.name) / "crashed.sqlite"
        program = """
import os, sys
sys.path.insert(0, sys.argv[2])
from test_offline_simulation import fixture
from market_making.execution.offline_coordinator import SimulationCoordinator
i,n,b,e,p,a,f,q = fixture()
c = SimulationCoordinator(sys.argv[1], i)
t = c.acquire('crashed-worker')
c.reconcile(t,e,n,100)
c.reserve(t,'crash-intent',q,f,e,p,a,n)
c.dispatch(t,'crash-intent',q,f,e,p,a,n)
os._exit(0)
"""
        result = subprocess.run([sys.executable, "-c", program, str(crash_path),
                                 str(Path(__file__).parent)], capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        c = SimulationCoordinator(crash_path, self.identity)
        try:
            token = c.acquire("recovery", old_dispatch_isolated=True)
            row = c.db.execute("SELECT state,attempt_utc FROM intents").fetchone()
            self.assertEqual(row["state"], "UNKNOWN")
            self.assertEqual(row["attempt_utc"], self.now.utc)
            c.reconcile(token, self.evidence, self.now, 100)
            with self.assertRaises(PrimaryError):
                c.dispatch(token, "crash-intent", self.proposal, self.fresh, self.evidence,
                           self.policy, self.approval, self.now)
        finally:
            c.close()

    def terminal_resolution(self, status="CANCELLED", cum="0"):
        self.reserve()
        self.dispatch()
        new = event("broker-1", 1, "NEW", "0", "6")
        terminal = event("broker-1", 2, status, cum, "0")
        self.coordinator.report(self.token, "intent-1", "broker-1", new, self.now)
        fills = () if cum == "0" else (Fill("durable-exec-1", "broker-1", "BUY", cum),)
        evidence = replace(self.evidence, position=cum, history_ids=("broker-1",), fills=fills,
                           historical=(HistoricalOrder("broker-1", "BUY", "6", status, cum),))
        self.coordinator.resolve(self.token, "intent-1", evidence, [new, terminal], self.now, 100)
        self.coordinator.reconcile(self.token, evidence, self.now, 100)
        return evidence, new, terminal

    def test_durable_fill_anchors_reject_old_newer_rebased_and_restart_snapshots(self):
        evidence, _, _ = self.terminal_resolution("FILLED", "6")
        omitted_newer = replace(self.evidence, snapshot_id="newer-but-omits-fill", source_utc=991)
        rebased = replace(evidence, baseline_position="-6", position="0", source_utc=991)
        contradictory = replace(evidence, position="5", source_utc=991,
                                fills=(Fill("durable-exec-1", "broker-1", "BUY", "5"),),
                                historical=(HistoricalOrder("broker-1", "BUY", "5", "FILLED", "5"),))
        for capture in (self.evidence, omitted_newer, rebased, contradictory):
            with self.subTest(capture=capture.snapshot_id), self.assertRaises(PrimaryError):
                self.coordinator.reconcile(self.token, capture, self.now, 100)
            self.assertIsNone(self.coordinator.db.execute("SELECT reconciled FROM authority").fetchone()[0])
            with self.assertRaises(PrimaryError):
                self.reserve("replayed-six-buy")
        with self.assertRaises(PrimaryError):
            self.coordinator.reconcile(self.token, evidence, self.now, 100)
        with self.assertRaises(PrimaryError):
            propose(self.book, evidence, self.policy, self.approval, self.now, "BUY", "6", "99", 2000)
        reopened = SimulationCoordinator(self.path, self.identity)
        try:
            token = reopened.acquire("restart", old_dispatch_isolated=True)
            with self.assertRaises(PrimaryError):
                reopened.reconcile(token, omitted_newer, self.now, 100)
            with self.assertRaises(PrimaryError):
                reopened.reconcile(token, evidence, self.now, 100)
            self.assertEqual(len(reopened.reservations()), 0)
        finally:
            reopened.close()

    def test_broker_identity_cannot_be_reused_after_resolution_or_restart(self):
        evidence, new, terminal = self.terminal_resolution()
        proposal = propose(self.book, evidence, self.policy, self.approval, self.now, "BUY", "6", "99", 2000)
        self.coordinator.reserve(self.token, "intent-2", proposal, self.fresh, evidence,
                                 self.policy, self.approval, self.now)
        self.coordinator.dispatch(self.token, "intent-2", proposal, self.fresh, evidence,
                                  self.policy, self.approval, self.now)
        with self.assertRaises(PrimaryError):
            self.coordinator.report(self.token, "intent-2", "broker-1", new, self.now)
        self.assertEqual(len(self.coordinator.reservations()), 2)
        with self.assertRaises(PrimaryError):
            self.coordinator.resolve(self.token, "intent-2", evidence, [new, terminal], self.now, 100)
        reopened = SimulationCoordinator(self.path, self.identity)
        try:
            token = reopened.acquire("restart", old_dispatch_isolated=True)
            with self.assertRaises(PrimaryError):
                reopened.reconcile(token, evidence, self.now, 100)
            self.assertEqual(len(reopened.reservations()), 2)
            self.assertEqual(reopened.db.execute("SELECT count(*) FROM intents WHERE broker_id='broker-1'").fetchone()[0], 1)
        finally:
            reopened.close()

    def test_concurrent_broker_binding_collision_quarantines_account_and_both_intents(self):
        proposal = propose(self.book, self.evidence, self.policy, self.approval, self.now, "BUY", "3", "99", 2000)
        for intent in ("small-1", "small-2"):
            self.coordinator.reserve(self.token, intent, proposal, self.fresh, self.evidence,
                                     self.policy, self.approval, self.now)
            self.coordinator.dispatch(self.token, intent, proposal, self.fresh, self.evidence,
                                      self.policy, self.approval, self.now)
        barrier = threading.Barrier(2)
        outcomes = []
        def worker(intent):
            c = SimulationCoordinator(self.path, self.identity)
            try:
                barrier.wait()
                c.report(self.token, intent, "collision", event("collision", 1, "NEW", "0", "3", "3"), self.now)
                outcomes.append("bound")
            except PrimaryError:
                outcomes.append("blocked")
            finally:
                c.close()
        threads = [threading.Thread(target=worker, args=(intent,)) for intent in ("small-1", "small-2")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertCountEqual(outcomes, ["bound", "blocked"])
        self.assertEqual([r[0] for r in self.coordinator.db.execute("SELECT state FROM intents")],
                         ["QUARANTINED", "QUARANTINED"])
        self.assertEqual(len(self.coordinator.reservations()), 2)
        with self.assertRaises(PrimaryError):
            self.coordinator.reconcile(self.token, self.evidence, self.now, 100)

    def test_late_exact_duplicates_idempotent_but_filled_restores_capacity_and_blocks_dispatch(self):
        evidence, new, terminal = self.terminal_resolution()
        for duplicate in (new, terminal, terminal):
            state = self.coordinator.report(self.token, "intent-1", "broker-1", duplicate, self.now)
            self.assertEqual(state["status"], "CANCELLED")
        self.assertEqual(len(self.coordinator.reservations()), 0)
        proposal = propose(self.book, evidence, self.policy, self.approval, self.now, "BUY", "6", "99", 2000)
        self.coordinator.reserve(self.token, "intent-2", proposal, self.fresh, evidence,
                                 self.policy, self.approval, self.now)
        with self.assertRaises(PrimaryError):
            self.coordinator.report(self.token, "intent-1", "broker-1",
                                    event("broker-1", 3, "FILLED", "6", "0"), self.now)
        self.assertEqual(len(self.coordinator.reservations()), 2)
        self.assertIsNone(self.coordinator.db.execute("SELECT reconciled FROM authority").fetchone()[0])
        with self.assertRaises(PrimaryError):
            self.coordinator.dispatch(self.token, "intent-2", proposal, self.fresh, evidence,
                                      self.policy, self.approval, self.now)
        token = self.coordinator.acquire("restart", old_dispatch_isolated=True)
        with self.assertRaises(PrimaryError):
            self.coordinator.reconcile(token, evidence, self.now, 100)

    def test_late_unsupported_correction_is_sticky_account_block(self):
        evidence, _, _ = self.terminal_resolution()
        with self.assertRaises(PrimaryError):
            self.coordinator.report(self.token, "intent-1", "broker-1",
                                    event("broker-1", 3, "REPLACED", "0", "6"), self.now)
        self.assertEqual(len(self.coordinator.reservations()), 1)
        self.assertEqual(self.coordinator.db.execute("SELECT state FROM intents").fetchone()[0], "QUARANTINED")
        with self.assertRaises(PrimaryError):
            self.coordinator.reconcile(self.token, evidence, self.now, 100)

    def test_cancel_terminal_obligation_cannot_be_omitted_and_legacy_migration_blocks(self):
        evidence, _, _ = self.terminal_resolution()
        with self.assertRaises(PrimaryError):
            self.coordinator.reconcile(self.token, self.evidence, self.now, 100)
        # Legacy journals lack verifiable execution-ID anchors: migration cannot
        # infer these from cumulative reports or silently permit new snapshots.
        self.coordinator.db.execute("DELETE FROM anchors")
        self.coordinator.db.execute("PRAGMA user_version=1")
        reopened = SimulationCoordinator(self.path, self.identity)
        try:
            token = reopened.acquire("legacy-recovery", old_dispatch_isolated=True)
            self.assertEqual(reopened.db.execute("PRAGMA user_version").fetchone()[0], 3)
            with self.assertRaises(PrimaryError):
                reopened.reconcile(token, evidence, self.now, 100)
        finally:
            reopened.close()

    def external_capture(self, status="UNKNOWN", total="6", cumulative="0", side="BUY",
                         working_quantity=None, price="99", uncertain=None, source=990,
                         fills=(), order_id="external-1"):
        if uncertain is None:
            uncertain = status == "UNKNOWN"
        if working_quantity is None:
            from decimal import Decimal
            working_quantity = str(Decimal(total) - Decimal(cumulative))
        working = () if status in {"FILLED", "CANCELLED", "REJECTED", "EXPIRED"} else (
            WorkingOrder(order_id, side, working_quantity, price, uncertain),)
        position = cumulative if side == "BUY" or cumulative == "0" else "-" + cumulative
        return replace(self.evidence, source_utc=source, snapshot_id="external-capture-" + str(source),
                       position=position, history_ids=(order_id,), fills=fills, working=working,
                       historical=(HistoricalOrder(order_id, side, total, status, cumulative),))

    def test_external_unknown_omissions_old_newer_restart_keep_obligations(self):
        capture = self.external_capture(source=991)
        self.coordinator.reconcile(self.token, capture, self.now, 100)
        for omission in (self.evidence, replace(self.evidence, source_utc=992, snapshot_id="newer-omission")):
            with self.assertRaises(PrimaryError):
                self.coordinator.reconcile(self.token, omission, self.now, 100)
            with self.assertRaises(PrimaryError):
                self.reserve("unsafe-six-buy")
            self.assertIsNone(self.coordinator.db.execute("SELECT reconciled FROM authority").fetchone()[0])
        reopened = SimulationCoordinator(self.path, self.identity)
        try:
            token = reopened.acquire("external-restart", old_dispatch_isolated=True)
            with self.assertRaises(PrimaryError):
                reopened.reconcile(token, replace(self.evidence, source_utc=992), self.now, 100)
            with self.assertRaises(PrimaryError):
                reopened.reconcile(token, capture, self.now, 100)
            with self.assertRaises(PrimaryError):
                propose(self.book, capture, self.policy, self.approval, self.now, "BUY", "6", "99", 2000)
        finally:
            reopened.close()

    def test_external_order_identity_total_price_and_status_changes_rejected(self):
        capture = self.external_capture(status="NEW")
        self.coordinator.reconcile(self.token, capture, self.now, 100)
        bad = (self.external_capture(status="NEW", side="SELL", source=991),
               self.external_capture(status="NEW", total="5", source=991),
               self.external_capture(status="NEW", total="7", source=991),
               self.external_capture(status="NEW", price="98", source=991),
               self.external_capture(status="UNKNOWN", source=991),
               self.external_capture(status="NEW", order_id="replacement-id", source=991))
        for index, item in enumerate(bad):
            item.validate(self.identity, self.now, 100)  # locally consistent, continuity is not
            c = SimulationCoordinator(self.path.with_name("mutation-" + str(index) + ".sqlite"), self.identity)
            try:
                token = c.acquire("mutation-test")
                c.reconcile(token, capture, self.now, 100)
                with self.subTest(item=item.historical), self.assertRaises(PrimaryError):
                    c.reconcile(token, item, self.now, 100)
                self.assertIsNone(c.db.execute("SELECT reconciled FROM authority").fetchone()[0])
                # Rejected evidence never changed anchors. Identity omission needs
                # corrected complete evidence; unsupported mutations need review.
                anchor = __import__("json").loads(c.db.execute("SELECT payload FROM anchors").fetchone()[0])
                self.assertEqual(anchor["orders"]["external-1"]["historical"]["quantity"], "6")
            finally:
                c.close()

    def test_external_unknown_capacity_reduction_without_fills_is_rejected(self):
        fill = Fill("external-fill-1", "external-1", "BUY", "2")
        capture = self.external_capture(cumulative="2", working_quantity="6", fills=(fill,))
        self.coordinator.reconcile(self.token, capture, self.now, 100)
        reduced = self.external_capture(cumulative="2", working_quantity="4", fills=(fill,), source=991)
        reduced.validate(self.identity, self.now, 100)
        with self.assertRaises(PrimaryError):
            self.coordinator.reconcile(self.token, reduced, self.now, 100)
        self.assertIsNone(self.coordinator.db.execute("SELECT reconciled FROM authority").fetchone()[0])

    def test_external_partial_pending_cancel_terminal_inventory_and_capacity(self):
        initial = self.external_capture()
        self.coordinator.reconcile(self.token, initial, self.now, 100)
        first_fill = Fill("external-fill-1", "external-1", "BUY", "2")
        partial = self.external_capture(status="PARTIALLY_FILLED", cumulative="2", source=991,
                                        fills=(first_fill, first_fill))
        self.coordinator.reconcile(self.token, partial, self.now, 100)
        with self.assertRaises(PrimaryError):
            propose(self.book, partial, self.policy, self.approval, self.now, "BUY", "6", "99", 2000)
        second_fill = Fill("external-fill-2", "external-1", "BUY", "1")
        pending = self.external_capture(status="PENDING_CANCEL", cumulative="3", source=992,
                                        fills=(first_fill, second_fill))
        self.coordinator.reconcile(self.token, pending, self.now, 100)
        cancelled = self.external_capture(status="CANCELLED", cumulative="3", source=993,
                                          fills=(first_fill, second_fill))
        self.coordinator.reconcile(self.token, cancelled, self.now, 100)
        proposal = propose(self.book, cancelled, self.policy, self.approval, self.now, "BUY", "6", "99", 2000)
        self.coordinator.reserve(self.token, "after-external-terminal", proposal, self.fresh, cancelled,
                                 self.policy, self.approval, self.now)
        result = self.coordinator.dispatch(self.token, "after-external-terminal", proposal, self.fresh,
                                           cancelled, self.policy, self.approval, self.now)
        self.assertEqual(result["state"], "UNKNOWN")
        self.assertFalse(result["network_used"])
        with self.assertRaises(PrimaryError):
            self.coordinator.reconcile(self.token, pending, self.now, 100)

    def test_external_filled_terminal_does_not_erase_inventory_or_allow_reopening(self):
        self.coordinator.reconcile(self.token, self.external_capture(status="NEW"), self.now, 100)
        fill = Fill("external-fill-1", "external-1", "BUY", "6")
        terminal = self.external_capture(status="FILLED", cumulative="6", fills=(fill,), source=991)
        self.coordinator.reconcile(self.token, terminal, self.now, 100)
        with self.assertRaises(PrimaryError):
            propose(self.book, terminal, self.policy, self.approval, self.now, "BUY", "6", "99", 2000)
        propose(self.book, terminal, self.policy, self.approval, self.now, "BUY", "4", "99", 2000)
        with self.assertRaises(PrimaryError):
            self.coordinator.reconcile(self.token, self.external_capture(status="NEW", source=992), self.now, 100)

    def test_external_obligation_v2_migration_without_local_intents_fails_closed(self):
        capture = self.external_capture()
        self.coordinator.reconcile(self.token, capture, self.now, 100)
        self.assertEqual(self.coordinator.db.execute("SELECT count(*) FROM intents").fetchone()[0], 0)
        self.coordinator.db.execute("PRAGMA user_version=2")
        reopened = SimulationCoordinator(self.path, self.identity)
        try:
            token = reopened.acquire("old-schema", old_dispatch_isolated=True)
            self.assertEqual(reopened.db.execute("PRAGMA user_version").fetchone()[0], 3)
            for evidence in (self.evidence, capture):
                with self.assertRaises(PrimaryError):
                    reopened.reconcile(token, evidence, self.now, 100)
        finally:
            reopened.close()

    def test_external_identity_cannot_be_adopted_by_later_local_dispatch(self):
        capture = self.external_capture(total="4", status="NEW")
        self.coordinator.reconcile(self.token, capture, self.now, 100)
        proposal = propose(self.book, capture, self.policy, self.approval, self.now, "BUY", "4", "99", 2000)
        self.coordinator.reserve(self.token, "different-local-intent", proposal, self.fresh, capture,
                                 self.policy, self.approval, self.now)
        self.coordinator.dispatch(self.token, "different-local-intent", proposal, self.fresh, capture,
                                  self.policy, self.approval, self.now)
        with self.assertRaises(PrimaryError):
            self.coordinator.report(self.token, "different-local-intent", "external-1",
                                    event("external-1", 1, "NEW", "0", "4", "4"), self.now)
        self.assertEqual(len(self.coordinator.reservations()), 1)
        self.assertIsNone(self.coordinator.db.execute("SELECT reconciled FROM authority").fetchone()[0])

    def test_uncertainty_downgrade_and_terminal_conflict_external_rejected(self):
        initial = self.external_capture(status="NEW", uncertain=True)
        self.coordinator.reconcile(self.token, initial, self.now, 100)
        certain = self.external_capture(status="NEW", uncertain=False, source=991)
        certain.validate(self.identity, self.now, 100)
        with self.assertRaises(PrimaryError):
            self.coordinator.reconcile(self.token, certain, self.now, 100)
        with self.assertRaises(PrimaryError):
            self.coordinator.reconcile(self.token, initial, self.now, 100)
        # Separate account history: a terminal contradiction is independently
        # tested without relying on the preceding uncertainty blocker.
        c = SimulationCoordinator(self.path.with_name("terminal-conflict.sqlite"), self.identity)
        token = c.acquire("terminal-conflict-test")
        cancelled = self.external_capture(status="CANCELLED", source=991)
        c.reconcile(token, cancelled, self.now, 100)
        filled = self.external_capture(status="FILLED", cumulative="6", source=992,
                                       fills=(Fill("late-fill", "external-1", "BUY", "6"),))
        filled.validate(self.identity, self.now, 100)
        try:
            with self.assertRaises(PrimaryError):
                c.reconcile(token, filled, self.now, 100)
            self.assertIsNone(c.db.execute("SELECT reconciled FROM authority").fetchone()[0])
            with self.assertRaises(PrimaryError):
                c.reconcile(token, cancelled, self.now, 100)
        finally:
            c.close()

    def test_external_pending_cancel_regression_requires_review_across_restart(self):
        fill = Fill("partial-fill", "external-1", "BUY", "2")
        pending = self.external_capture(status="PENDING_CANCEL", cumulative="2", fills=(fill,))
        self.coordinator.reconcile(self.token, pending, self.now, 100)
        regression = replace(pending, source_utc=991,
                             historical=(replace(pending.historical[0], status="PARTIALLY_FILLED"),))
        regression.validate(self.identity, self.now, 100)
        with self.assertRaises(PrimaryError):
            self.coordinator.reconcile(self.token, regression, self.now, 100)
        token = self.coordinator.acquire("restart-after-regression", old_dispatch_isolated=True)
        with self.assertRaises(PrimaryError):
            self.coordinator.reconcile(token, pending, self.now, 100)

    def test_external_valid_obligations_survive_restart_and_allow_terminal_resolution(self):
        capture = self.external_capture()
        self.coordinator.reconcile(self.token, capture, self.now, 100)
        reopened = SimulationCoordinator(self.path, self.identity)
        try:
            token = reopened.acquire("valid-external-restart", old_dispatch_isolated=True)
            reopened.reconcile(token, capture, self.now, 100)
            cancelled = self.external_capture(status="CANCELLED", source=991)
            reopened.reconcile(token, cancelled, self.now, 100)
            proposal = propose(self.book, cancelled, self.policy, self.approval, self.now, "BUY", "6", "99", 2000)
            reopened.reserve(token, "after-valid-restart-terminal", proposal, self.fresh, cancelled,
                             self.policy, self.approval, self.now)
            reopened.dispatch(token, "after-valid-restart-terminal", proposal, self.fresh, cancelled,
                              self.policy, self.approval, self.now)
        finally:
            reopened.close()

    def test_external_omission_blocks_already_reserved_dispatch_and_new_observation_replay(self):
        capture = self.external_capture()
        self.coordinator.reconcile(self.token, capture, self.now, 100)
        proposal = propose(self.book, capture, self.policy, self.approval, self.now, "BUY", "4", "99", 2000)
        self.coordinator.reserve(self.token, "already-reserved", proposal, self.fresh, capture,
                                 self.policy, self.approval, self.now)
        # A capture replaces the known external ID with a newly observed order.
        # Neither replaying a previous good capture nor dispatching a previously
        # reserved intent may hide the conflicting account observation.
        other = self.external_capture(order_id="new-external-id", source=991)
        with self.assertRaises(PrimaryError):
            self.coordinator.reconcile(self.token, other, self.now, 100)
        with self.assertRaises(PrimaryError):
            self.coordinator.reconcile(self.token, capture, self.now, 100)
        with self.assertRaises(PrimaryError):
            self.coordinator.dispatch(self.token, "already-reserved", proposal, self.fresh, capture,
                                      self.policy, self.approval, self.now)
        self.assertIsNone(self.coordinator.db.execute("SELECT attempt_utc FROM intents").fetchone()[0])


if __name__ == "__main__":
    unittest.main()

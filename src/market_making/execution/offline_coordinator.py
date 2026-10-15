"""Transactional account-scoped LOCAL simulation authority, never cross-host fencing.

All cooperating processes MUST use the same durable SQLite file on local disk.
No callable transport exists: dispatch records one simulated attempt only. A
committed attempt is UNKNOWN until complete normalized history reconciliation.
An ack/cancel report alone NEVER frees its reservation. Takeover requires explicit
old-dispatch isolation and fresh reconciliation; declarations are fixture-only.
"""
from contextlib import contextmanager
from dataclasses import asdict
import json
import sqlite3

from market_making.execution.offline_contracts import digest, require, decimal
from market_making.execution.offline_risk import check
from market_making.execution.execution_state import initial_state, reduce_event, validate_event, TERMINAL
from market_making.http_policy import PrimaryError


class SnapshotOrderConflict(PrimaryError):
    """Unsupported account-order mutation needs durable external review."""


class SimulationCoordinator:
    def __init__(self, path, identity):
        identity.validate()
        self.identity = identity
        # Dispatch capability is deliberately process/object-local and never
        # restored from the journal or a caller-supplied pre-restart token.
        self.dispatch_token = None
        self.account_key = digest({"environment": identity.environment, "account": identity.account})
        self.db = sqlite3.connect(str(path), timeout=5, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        require(version in (0, 1, 2, 3),
                "Unknown simulation journal schema version")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS authority (
              account TEXT PRIMARY KEY, scope TEXT NOT NULL, epoch INTEGER NOT NULL, owner TEXT NOT NULL,
              reconciled TEXT, last_utc INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS intents (
              account TEXT NOT NULL, id TEXT NOT NULL, epoch INTEGER NOT NULL,
              payload TEXT NOT NULL, state TEXT NOT NULL, attempt_utc INTEGER,
              broker_id TEXT, events TEXT NOT NULL DEFAULT '[]',
              PRIMARY KEY(account,id));
            CREATE TABLE IF NOT EXISTS audit (
              n INTEGER PRIMARY KEY, account TEXT, kind TEXT, payload TEXT);
            CREATE TABLE IF NOT EXISTS anchors (
              account TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS account_blocks (
              account TEXT NOT NULL, reason TEXT NOT NULL, PRIMARY KEY(account,reason));
            CREATE UNIQUE INDEX IF NOT EXISTS account_broker_identity
              ON intents(account,broker_id) WHERE broker_id IS NOT NULL;
        """)
        # Old populated journals cannot manufacture missing execution obligations.
        # Preserve their evidence, but require external manual review, not rebasing.
        if version < 3:
            with self.transaction():
                self.db.execute("INSERT OR IGNORE INTO account_blocks(account,reason) "
                                "SELECT account,'LEGACY_JOURNAL_CONTINUITY_UNPROVEN' FROM authority")
                self.db.execute("INSERT OR IGNORE INTO account_blocks(account,reason) "
                                "SELECT account,'LEGACY_JOURNAL_CONTINUITY_UNPROVEN' FROM intents")
                self.db.execute("INSERT OR IGNORE INTO account_blocks(account,reason) "
                                "SELECT account,'LEGACY_JOURNAL_CONTINUITY_UNPROVEN' FROM anchors")
                self.db.execute("PRAGMA user_version=3")

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def audit(self, kind, payload):
        self.db.execute("INSERT INTO audit(account,kind,payload) VALUES (?,?,?)",
                        (self.account_key, kind, json.dumps(payload, sort_keys=True)))

    def acquire(self, owner, old_dispatch_isolated=False):
        require(isinstance(owner, str) and 0 < len(owner) <= 128, "Owner identity required")
        with self.transaction():
            old = self.db.execute("SELECT * FROM authority WHERE account=?", (self.account_key,)).fetchone()
            require(old is None or old["scope"] == digest(asdict(self.identity)),
                    "Bounded simulation supports one instrument/specification per account")
            require(old is None or old_dispatch_isolated is True, "Old dispatcher isolation required")
            epoch = 1 if old is None else old["epoch"] + 1
            self.db.execute("INSERT INTO authority(account,scope,epoch,owner) VALUES (?,?,?,?) "
                            "ON CONFLICT(account) DO UPDATE SET epoch=excluded.epoch, "
                            "owner=excluded.owner,reconciled=NULL",
                            (self.account_key, digest(asdict(self.identity)), epoch, owner))
            # A crash between reservation and attempted dispatch cannot be proved
            # absent by restart; retain all capacity and require reconciliation.
            self.db.execute("UPDATE intents SET state='UNKNOWN' WHERE account=? AND state NOT IN ('RESOLVED','QUARANTINED')",
                            (self.account_key,))
            self.audit("ACQUIRE_RECONCILIATION_REQUIRED", {"owner": owner, "epoch": epoch})
        self.dispatch_token = (owner, epoch)
        return self.dispatch_token

    def disconnect(self, token, now):
        with self.transaction():
            self.authority(token, now)
            self.db.execute("UPDATE authority SET reconciled=NULL WHERE account=?", (self.account_key,))
            self.db.execute("UPDATE intents SET state='UNKNOWN' WHERE account=? AND state NOT IN ('RESOLVED','QUARANTINED')",
                            (self.account_key,))
            self.audit("DISCONNECT_RECONCILIATION_REQUIRED", {"epoch": token[1]})
        self.dispatch_token = None

    def authority(self, token, now):
        row = self.db.execute("SELECT * FROM authority WHERE account=?", (self.account_key,)).fetchone()
        require(row is not None and (row["owner"], row["epoch"]) == token
                and row["scope"] == digest(asdict(self.identity)), "Stale ownership epoch/scope")
        now.validate()
        require(now.utc >= row["last_utc"], "Account clock rollback")
        self.db.execute("UPDATE authority SET last_utc=? WHERE account=?", (now.utc, self.account_key))
        return row

    def reconcile(self, token, evidence, now, max_age):
        try:
            self._reconcile(token, evidence, now, max_age)
        except PrimaryError as error:
            with self.transaction():
                self.authority(token, now)
                self.db.execute("UPDATE authority SET reconciled=NULL WHERE account=?", (self.account_key,))
                if isinstance(error, SnapshotOrderConflict):
                    self.block("CONTRADICTORY_ACCOUNT_ORDER_EVIDENCE")
                self.audit("RECONCILIATION_REJECTED", {"snapshot": evidence.snapshot_id})
            raise

    def unblocked(self):
        require(self.db.execute("SELECT 1 FROM account_blocks WHERE account=? LIMIT 1",
                               (self.account_key,)).fetchone() is None,
                "Account quarantined; external manual review required")

    def block(self, reason):
        self.db.execute("INSERT OR IGNORE INTO account_blocks(account,reason) VALUES (?,?)",
                        (self.account_key, reason))
        self.db.execute("UPDATE authority SET reconciled=NULL WHERE account=?", (self.account_key,))

    def anchor(self, evidence):
        """Called under the write transaction after full evidence validation.

        A fixed inventory baseline and append-only execution/all-order obligations
        prevent older OR newer self-consistent captures from erasing known fills.
        There is deliberately no automatic baseline/history compaction API.
        """
        row = self.db.execute("SELECT payload FROM anchors WHERE account=?", (self.account_key,)).fetchone()
        current = {"baseline": evidence.baseline_position, "source_utc": evidence.source_utc,
                   "fills": {f.execution_id: asdict(f) for f in evidence.fills},
                   "orders": {o.order_id: {"historical": asdict(o), "working": None}
                              for o in evidence.historical}}
        for order in evidence.working:
            current["orders"][order.order_id]["working"] = asdict(order)
        if row:
            old = json.loads(row["payload"])
            require(decimal(current["baseline"], True) == decimal(old["baseline"], True)
                    and current["source_utc"] >= old["source_utc"], "Reconciliation baseline/time regression")
            if not all(current["fills"].get(key) == value for key, value in old["fills"].items()):
                raise SnapshotOrderConflict("Reconciliation omitted/contradicted durable execution obligations")
            require("orders" in old, "Legacy all-order obligations unproven")
            for key, obligation in old["orders"].items():
                if key not in current["orders"]:
                    raise SnapshotOrderConflict("Reconciliation omitted durable account order")
                self.order_transition(obligation, current["orders"][key])
        self.db.execute("INSERT INTO anchors(account,payload) VALUES (?,?) "
                        "ON CONFLICT(account) DO UPDATE SET payload=excluded.payload",
                        (self.account_key, json.dumps(current, sort_keys=True)))

    @staticmethod
    def order_transition(old, new):
        """No implicit replace/correction/uncertainty release in snapshot changes.

        Terminal records are immutable. For active records, immutable identity,
        total and price plus append-only fills preserve the exposure envelope.
        A working-capacity reduction must be covered by newly recorded fills;
        terminal completion requires complete validated history and inventory.
        """
        def guard(condition, message):
            if not condition:
                raise SnapshotOrderConflict(message)
        before, after = old["historical"], new["historical"]
        guard(before["order_id"] == after["order_id"] and before["side"] == after["side"]
                and decimal(before["quantity"]) == decimal(after["quantity"]),
                "Account order identity/quantity changed; replacement unsupported")
        transitions = {
            "UNKNOWN": {"UNKNOWN", "PENDING_NEW", "NEW", "PARTIALLY_FILLED", "PENDING_CANCEL"} | TERMINAL,
            "PENDING_NEW": {"PENDING_NEW", "NEW", "PARTIALLY_FILLED", "PENDING_CANCEL"} | TERMINAL,
            "NEW": {"NEW", "PARTIALLY_FILLED", "PENDING_CANCEL"} | TERMINAL,
            "PARTIALLY_FILLED": {"PARTIALLY_FILLED", "PENDING_CANCEL", "FILLED", "CANCELLED", "EXPIRED"},
            "PENDING_CANCEL": {"PENDING_CANCEL", "FILLED", "CANCELLED", "EXPIRED"},
        }
        if before["status"] in TERMINAL:
            guard(before == after and new["working"] is None, "Terminal account history changed")
            return
        guard(after["status"] in transitions[before["status"]]
                and decimal(after["cumulative"]) >= decimal(before["cumulative"]),
                "Account order status/cumulative regression")
        if after["status"] in TERMINAL:
            guard(new["working"] is None, "Terminal order still working")
            return
        prior, working = old["working"], new["working"]
        guard(prior is not None and working is not None
                and decimal(prior["price"]) == decimal(working["price"]),
                "Working order omitted/repriced; replacement unsupported")
        guard(not prior["uncertain"] or working["uncertain"]
                or before["status"] != after["status"], "Unsupported uncertainty downgrade")
        from decimal import Context, localcontext
        with localcontext(Context(prec=128)):
            capacity_reduction = decimal(prior["quantity"]) - decimal(working["quantity"])
            added_fills = decimal(after["cumulative"]) - decimal(before["cumulative"])
            guard(capacity_reduction <= added_fills,
                    "Working capacity reduced without corresponding new fills")

    def _reconcile(self, token, evidence, now, max_age):
        evidence.validate(self.identity, now, max_age)
        with self.transaction():
            self.authority(token, now)
            self.unblocked()
            self.anchor(evidence)
            # Unresolved intents remain reserved even if absent from active orders.
            self.db.execute("UPDATE authority SET reconciled=? WHERE account=?",
                            (digest(asdict(evidence)), self.account_key))
            self.audit("ACCOUNT_FIXTURE_RECONCILED", {"hash": digest(asdict(evidence))})

    def reservations(self, exclude=None):
        rows = self.db.execute("SELECT * FROM intents WHERE account=? AND state!='RESOLVED'",
                               (self.account_key,)).fetchall()
        result = []
        for row in rows:
            if row["id"] != exclude:
                proposal = json.loads(row["payload"])
                result.append((proposal["side"], proposal["quantity"], proposal["price"]))
        return result

    def gates(self, token, proposal, freshness, evidence, policy, approval, now, exclude=None):
        row = self.authority(token, now)
        self.unblocked()
        require(row["reconciled"] == digest(asdict(evidence)), "Reconciliation required")
        require(evidence.identity == self.identity, "Coordinator identity mismatch")
        attempts = self.db.execute("SELECT count(*) FROM intents WHERE account=? AND attempt_utc>=?",
                                   (self.account_key, now.utc - policy.rate_window_ns)).fetchone()[0]
        return check(proposal, freshness.current(now), evidence, policy, approval, now,
                     self.reservations(exclude), attempts)

    def reserve(self, token, intent_id, proposal, freshness, evidence, policy, approval, now):
        require(isinstance(intent_id, str) and 0 < len(intent_id) <= 128, "Intent identity required")
        with self.transaction():
            require(self.db.execute("SELECT 1 FROM intents WHERE account=? AND id=?",
                                   (self.account_key, intent_id)).fetchone() is None, "Intent already exists; never retry")
            limits = self.gates(token, proposal, freshness, evidence, policy, approval, now)
            self.db.execute("INSERT INTO intents(account,id,epoch,payload,state) VALUES (?,?,?,?, 'RESERVED')",
                            (self.account_key, intent_id, token[1], json.dumps(asdict(proposal), sort_keys=True)))
            self.audit("DURABLE_RESERVATION", {"id": intent_id, "limits": limits})
        return intent_id

    def dispatch(self, token, intent_id, proposal, freshness, evidence, policy, approval, now):
        """No broker send, no injected callback, no retry. Commit precedes return."""
        require(self.dispatch_token is not None and token == self.dispatch_token,
                "Local dispatch session requires acquire/reconciliation after restart or disconnect")
        with self.transaction():
            row = self.db.execute("SELECT * FROM intents WHERE account=? AND id=?",
                                  (self.account_key, intent_id)).fetchone()
            require(row is not None and row["state"] == "RESERVED" and row["attempt_utc"] is None
                    and row["epoch"] == token[1]
                    and json.loads(row["payload"]) == asdict(proposal), "No dispatch retry / altered intent")
            self.gates(token, proposal, freshness, evidence, policy, approval, now, intent_id)
            self.db.execute("UPDATE intents SET state='UNKNOWN',attempt_utc=? WHERE account=? AND id=?",
                            (now.utc, self.account_key, intent_id))
            self.audit("SIMULATED_ATTEMPT_UNKNOWN", {"id": intent_id})
        return {"scope": "offline_simulation", "state": "UNKNOWN", "attempts": 1, "network_used": False}

    def report(self, token, intent_id, broker_id, event, now):
        """Normalized synthetic reports only, unsupported corrections fail closed."""
        try:
            return self._report(token, intent_id, broker_id, event, now)
        except (PrimaryError, KeyError, TypeError, sqlite3.IntegrityError):
            # Persist quarantine even though malformed event processing rolled back.
            # It cannot be cleared by restart, a cancel ack or replacement history.
            with self.transaction():
                self.authority(token, now)
                self.db.execute("UPDATE intents SET state='QUARANTINED' WHERE account=? AND (id=? OR broker_id=?)",
                                (self.account_key, intent_id, broker_id if isinstance(broker_id, str) else None))
                self.block("INVALID_OR_CONFLICTING_EXECUTION_REPORT")
                self.audit("INVALID_REPORT_MANUAL_REVIEW_REQUIRED", {"id": intent_id})
            raise PrimaryError("Report invalid/conflicted; reservation quarantined") from None

    def _report(self, token, intent_id, broker_id, event, now):
        with self.transaction():
            self.authority(token, now)
            validate_event(event)
            row = self.db.execute("SELECT * FROM intents WHERE account=? AND id=?",
                                  (self.account_key, intent_id)).fetchone()
            require(row is not None and row["attempt_utc"] is not None
                    and row["state"] != "QUARANTINED",
                    "Report has no attempted intent")
            require(isinstance(broker_id, str) and broker_id and
                    row["broker_id"] in (None, broker_id) and event["order_key"] == broker_id,
                    "Authoritative identifier conflict")
            anchor_row = self.db.execute("SELECT payload FROM anchors WHERE account=?",
                                         (self.account_key,)).fetchone()
            anchored_orders = {} if anchor_row is None else json.loads(anchor_row["payload"]).get("orders", {})
            require(row["broker_id"] == broker_id or broker_id not in anchored_orders,
                    "Previously observed account order cannot be adopted as a new intent identity")
            events = json.loads(row["events"])
            require(self.db.execute("SELECT 1 FROM intents WHERE account=? AND broker_id=? AND id!=?",
                                    (self.account_key, broker_id, intent_id)).fetchone() is None,
                    "Broker identifier already bound to another durable intent")
            require(row["state"] != "RESOLVED" or event in events,
                    "Contradictory/unsupported report after resolution")
            state = initial_state(broker_id)
            for old in events:
                state = reduce_event(state, old)
            state = reduce_event(state, event)
            require(decimal(event["total"]) == decimal(json.loads(row["payload"])["quantity"]),
                    "Report quantity differs from reserved intent")
            self.db.execute("UPDATE intents SET broker_id=?,events=? WHERE account=? AND id=?",
                            (broker_id, json.dumps(state["events"]), self.account_key, intent_id))
            if state["blockers"] != ["BROKER_CONTINUITY_UNPROVEN"]:
                self.db.execute("UPDATE intents SET state='QUARANTINED' WHERE account=? AND id=?",
                                (self.account_key, intent_id))
                self.block("EXECUTION_HISTORY_CONFLICT")
            self.audit("NORMALIZED_REPORT_RESERVATION_RETAINED", {"id": intent_id})
            return state

    def resolve(self, token, intent_id, evidence, complete_history, now, max_age):
        """Only full sequential identifier-bound terminal history + inventory evidence.

        No 'not found' release, no timeout release, no cancel-ack-only release.
        History completeness is an offline declaration needing external verification.
        """
        try:
            self._resolve(token, intent_id, evidence, complete_history, now, max_age)
        except SnapshotOrderConflict:
            with self.transaction():
                self.authority(token, now)
                self.block("CONTRADICTORY_ACCOUNT_ORDER_EVIDENCE")
                self.audit("RESOLUTION_ACCOUNT_ORDER_CONFLICT", {"id": intent_id})
            raise

    def _resolve(self, token, intent_id, evidence, complete_history, now, max_age):
        evidence.validate(self.identity, now, max_age)
        with self.transaction():
            self.authority(token, now)
            self.unblocked()
            row = self.db.execute("SELECT * FROM intents WHERE account=? AND id=?",
                                  (self.account_key, intent_id)).fetchone()
            require(row is not None and row["broker_id"] in evidence.history_ids
                    and row["state"] not in {"RESOLVED", "QUARANTINED"} and isinstance(complete_history, list)
                    and complete_history, "Complete authoritative history required")
            state = initial_state(row["broker_id"])
            require([event.get("sequence") for event in complete_history] ==
                    list(range(1, len(complete_history) + 1)), "Complete ordered history baseline required")
            for event in complete_history:
                state = reduce_event(state, event)
            require(state["status"] in TERMINAL
                    and state["blockers"] == ["BROKER_CONTINUITY_UNPROVEN"], "History is nonterminal/conflicted")
            require(all(event in complete_history for event in json.loads(row["events"])),
                    "History omitted observed report")
            require(decimal(complete_history[0]["total"]) == decimal(json.loads(row["payload"])["quantity"]),
                    "History quantity mismatch")
            unique = {f.execution_id: f for f in evidence.fills if f.order_id == row["broker_id"]}
            from decimal import Context, localcontext
            with localcontext(Context(prec=128)):
                filled = sum((decimal(f.quantity) for f in unique.values()), decimal("0"))
            require(filled == decimal(state["cum"])
                    and not any(o.order_id == row["broker_id"] for o in evidence.working),
                    "Fills/working orders do not reconcile terminal history")
            historical = next(o for o in evidence.historical if o.order_id == row["broker_id"])
            require(historical.status == state["status"]
                    and decimal(historical.quantity) == decimal(complete_history[0]["total"]),
                    "Account history and execution history disagree")
            require(all(f.side == json.loads(row["payload"])["side"] for f in unique.values()),
                    "Fill side mismatch")
            self.anchor(evidence)
            # Preserve full resolution history for exact late duplicate recognition.
            self.db.execute("UPDATE intents SET state='RESOLVED',events=? WHERE account=? AND id=?",
                            (json.dumps(complete_history), self.account_key, intent_id))
            # Releasing reservation invalidates prior account bindings atomically.
            self.db.execute("UPDATE authority SET reconciled=NULL WHERE account=?", (self.account_key,))
            self.audit("RESOLVED_FROM_COMPLETE_FIXTURE_HISTORY", {"id": intent_id, "cum": state["cum"]})

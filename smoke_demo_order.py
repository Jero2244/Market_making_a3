"""Explicitly gated ONE demo BUY LIMIT DAY, followed immediately by cancel.

Default is OFFLINE/NOT_RUN. Independent safety review is required before --send.
"""
import argparse
from decimal import ROUND_FLOOR
import hashlib
import json
import os
from pathlib import Path
import requests
from check_connection import PrimaryError, authenticate, load_dotenv, safe_payload
from demo_execution import DemoClient, MonitoringBlocked, cleanup, durable, identifier, lifecycle
from instrument_rules import resolve, number
from order_book import fetch_book

LOCK = Path(__file__).resolve().parent / ".demo_order_lock.json"


def read_review_evidence(path):
    return path if isinstance(path, dict) else json.loads(Path(path).read_text(encoding="utf-8"))


def session_gate(path, now=None):
    """Independent review gate, NOT a production calendar gate (demo is 24/7)."""
    data = read_review_evidence(path)
    if data.get("independently_reviewed") is not True:
        raise PrimaryError("Independent demo safety review required.")


def choose_price(rules, book):
    book.gate()
    # At least ten ticks below the current best bid, not merely below ask.
    price = ((book.bid - 10*rules.tick) / rules.tick).to_integral_value(rounding=ROUND_FLOOR)*rules.tick
    return rules.validate_price(price)


def preflight(session, client, evidence):
    session_gate(evidence)
    timestamp_gate(evidence)
    rules = resolve(session)
    client.monitor_ready()
    if client.orders(active=True):
        raise PrimaryError("Existing active account orders: smoke blocked.")
    book = fetch_book(session)
    price = choose_price(rules, book)
    return rules, book, price


def timestamp_gate(path):
    # A boolean attestation cannot establish undocumented units/timezone/scope.
    raise PrimaryError("No supported authoritative whole-book timestamp mapping; send blocked.")


def final_send_gate(evidence, book):
    """Local gates only, after durable intent; never fetch a replacement book."""
    timestamp_gate(evidence)
    session_gate(evidence)
    book.gate()


def diagnostic_preflight(session, account, evidence):
    """Read-only observations independent of send gates; aggregate ALL blockers.

    No new/cancel endpoint is reachable here. Missing account/session/timestamp
    evidence must not prevent catalog/detail/snapshot diagnostics.
    """
    report = {"mode": "read_only_diagnostic", "orders_sent": 0, "stages": {}, "blockers": [],
              "environment": "REMARKETS 24/7 test; requests outside production hours permitted; acceptance/execution/freshness not guaranteed"}

    def record(stage, status, code=None, **facts):
        report["stages"][stage] = {"status": status, **facts}
        if code:
            report["stages"][stage]["code"] = code
            report["blockers"].append(code)

    record("authentication", "PASS" if session is not None else "BLOCKED",
           None if session is not None else "AUTHENTICATION_UNAVAILABLE")
    for stage, gate, code in (("independent_review", session_gate, "INDEPENDENT_SAFETY_REVIEW_UNVERIFIED"),
                              ("timestamp_semantics", timestamp_gate, "SNAPSHOT_TIMESTAMP_SEMANTICS_UNVERIFIED")):
        try:
            gate(evidence)
            record(stage, "PASS")
        except (Exception, KeyboardInterrupt):
            record(stage, "BLOCKED", code)
    rules = book = None
    if session is not None:
        try:
            rules = resolve(session)
            record("contract", "PASS", symbol=rules.symbol, expiry=rules.expiry,
                   price_tick=str(rules.tick), quantity=1, low=str(rules.low), high=str(rules.high))
        except (Exception, KeyboardInterrupt):
            record("contract", "BLOCKED", "EXACT_OCTOBER_RULES_UNVERIFIED")
        try:
            book = fetch_book(session)
            record("snapshot", "PASS", observation=book.summary())
        except (Exception, KeyboardInterrupt):
            record("snapshot", "BLOCKED", "REST_SNAPSHOT_UNAVAILABLE_OR_INVALID")
    else:
        record("contract", "BLOCKED", "EXACT_OCTOBER_RULES_UNVERIFIED")
        record("snapshot", "BLOCKED", "REST_SNAPSHOT_UNAVAILABLE_OR_INVALID")
    try:
        if book is None:
            raise PrimaryError("No book.")
        book.gate()
        record("book_freshness", "PASS")
    except (Exception, KeyboardInterrupt):
        code = ("EXCHANGE_SNAPSHOT_TIMESTAMP_MISSING_OR_INVALID" if book is not None and book.server_time is None
                else "EXCHANGE_SNAPSHOT_TIMESTAMP_PROVENANCE_UNVERIFIED" if book is not None and not book.timestamp_authoritative
                else "BOOK_NOT_FRESH_TWO_SIDED")
        record("book_freshness", "BLOCKED", code)
    if book is None or book.clock_uncertainty is None:
        record("clock", "BLOCKED", "CLOCK_UNCERTAINTY_UNVERIFIED")
    else:
        record("clock", "OBSERVED", uncertainty_seconds=book.clock_uncertainty)
    client = None
    try:
        identifier(account)
        record("account_configuration", "PASS")
        if session is not None:
            client = DemoClient(session, account)
    except (Exception, KeyboardInterrupt):
        record("account_configuration", "BLOCKED", "PRIMARY_ACCOUNT_MISSING_OR_INVALID")
    if client is not None:
        try:
            client.orders()
            record("account_access", "PASS")
        except (Exception, KeyboardInterrupt):
            record("account_access", "BLOCKED", "ACCOUNT_READ_ACCESS_UNVERIFIED")
        try:
            active = client.orders(active=True)
            record("active_orders", "PASS" if not active else "BLOCKED",
                   None if not active else "EXISTING_ACTIVE_ACCOUNT_ORDERS", count=len(active))
        except (Exception, KeyboardInterrupt):
            record("active_orders", "BLOCKED", "ACTIVE_ORDER_RECONCILIATION_UNVERIFIED")
        try:
            client.monitor_ready()
            record("monitoring", "PASS", method="correlated_historical_REST_id")
        except MonitoringBlocked:
            record("monitoring", "BLOCKED", "EMPTY_HISTORY_MONITORING_UNVERIFIED",
                   alternative="No verified execution-report subscription alternative implemented; no order may be created to seed history.")
        except (Exception, KeyboardInterrupt):
            record("monitoring", "BLOCKED", "ORDER_STATUS_MONITORING_UNVERIFIED")
    else:
        record("account_access", "BLOCKED", "ACCOUNT_READ_ACCESS_UNVERIFIED")
        record("active_orders", "BLOCKED", "ACTIVE_ORDER_RECONCILIATION_UNVERIFIED")
        record("monitoring", "BLOCKED", "ORDER_STATUS_MONITORING_UNVERIFIED")
    try:
        if rules is None or book is None:
            raise PrimaryError("Rules/book missing.")
        record("price", "PASS", price=str(choose_price(rules, book)))
    except (Exception, KeyboardInterrupt):
        record("price", "BLOCKED", "SAFE_SEND_PRICE_UNVERIFIED")
    report["status"] = "BLOCKED" if report["blockers"] else "READY_FOR_INDEPENDENT_REVIEW"
    return report


def run_diagnostic(folder, evidence, user, password, account):
    with requests.Session() as session:
        session.trust_env = False
        authenticated = False
        if user and password:
            try:
                authenticate(session, user, password)
                authenticated = True
            except (Exception, KeyboardInterrupt):
                pass
        report = diagnostic_preflight(session if authenticated else None, account, evidence)
        durable(folder / "preflight.json", report, exclusive=True)
    print("Read-only diagnostic recorded; orders sent: 0. " + report["status"])
    return 2 if report["blockers"] else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight", action="store_true", help="Read-only diagnostic; session/account evidence optional unless sending")
    parser.add_argument("--send", action="store_true", help="ONE demo order; requires independent review")
    parser.add_argument("--cancel-only", action="store_true", help="Resume cleanup; NEVER submit")
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--session-evidence", "--review-evidence", dest="session_evidence", type=Path,
                        help="Independent review evidence; not a production session calendar")
    args = parser.parse_args(argv)
    if not args.preflight and not args.send and not args.cancel_only:
        print("NOT_RUN: offline default; no network or order submitted.")
        return 2
    if (not args.evidence_dir or (args.send and (not args.preflight or args.cancel_only))
            or (args.send and not args.session_evidence)):
        parser.error("need evidence-dir; send requires preflight and independent review-evidence; cancel-only excludes send")
    folder = args.evidence_dir.resolve()
    lock_owned = False
    try:
        if not args.cancel_only:
            folder.mkdir(parents=True, exist_ok=False)
        load_dotenv(Path(".env"))
        user, password, account = (os.getenv(k, "") for k in
                                   ("PRIMARY_USER", "PRIMARY_PASSWORD", "PRIMARY_ACCOUNT"))
        if args.preflight and not args.send and not args.cancel_only:
            return run_diagnostic(folder, args.session_evidence, user, password, account)
        if not all((user, password, account)):
            raise PrimaryError("Missing local credentials/account.")
        account = identifier(account)
        account_tag = hashlib.sha256(account.encode()).hexdigest()
        with requests.Session() as session:
            session.trust_env = False  # no ambient proxy/netrc credential routing
            authenticate(session, user, password)
            client = DemoClient(session, account)
            journal = folder / "intent.json"
            if args.cancel_only:
                lock = json.loads(LOCK.read_text(encoding="utf-8"))
                if lock != {"evidence_dir": str(folder), "account_tag": account_tag}:
                    raise PrimaryError("Recovery lock/account mismatch.")
                state = json.loads(journal.read_text(encoding="utf-8"))
                if not safe_payload(state, session._primary_secrets):
                    raise PrimaryError("Unsafe recovery data.")
                if (state.get("symbol") != "RFX20/OCT26" or state.get("side") != "BUY"
                        or state.get("qty") != 1 or state.get("ordType") != "LIMIT"
                        or state.get("timeInForce") != "DAY" or number(state.get("price")) % 100):
                    raise PrimaryError("Recovery intent invalid.")
                ids = state.get("ids")
                if not isinstance(ids, dict) or set(ids) != {"clOrdId", "proprietary"}:
                    raise PrimaryError("Unknown submit: reconcile manually in demo UI; NEVER resubmit.")
                ids = {k: identifier(v) for k, v in ids.items()}
                success = cleanup(client, ids, number(state["price"]), journal, state)
                if success:
                    LOCK.unlink()
                print("Cleanup terminal proof recorded." if success else "BLOCKED/FAIL: smoke did not pass; inspect cleanup evidence; lock retained.")
                return 0 if success else 2
            rules, book, price = preflight(session, client, args.session_evidence)
            durable(folder / "preflight.json", {"status": "READY", "symbol": rules.symbol,
                    "expiry": rules.expiry, "tick": str(rules.tick), "price": str(price),
                    "book": book.summary(), "account_access": True, "monitor_access": True,
                    "orders_sent": 0}, exclusive=True)
            if not args.send:
                print("Preflight READY; no order sent. Independent safety review still required.")
                return 0
            durable(LOCK, {"evidence_dir": str(folder), "account_tag": account_tag}, exclusive=True)
            lock_owned = True

            send_book = None

            def recheck():
                nonlocal send_book
                fresh_rules, fresh_book, _ = preflight(session, client, args.session_evidence)
                fresh_rules.validate_price(price)
                if price > fresh_book.bid - 10*fresh_rules.tick or price >= fresh_book.ask:
                    raise PrimaryError("Market moved; abort, no replacement price/order.")
                send_book = fresh_book

            # Read evidence BEFORE lifecycle; final callback performs no filesystem IO.
            reviewed_evidence = read_review_evidence(args.session_evidence)
            success = lifecycle(client, price, journal, recheck,
                                lambda: final_send_gate(reviewed_evidence, send_book))
            final_state = json.loads(journal.read_text(encoding="utf-8"))
            if (final_state.get("phase") == "aborted_no_send"
                    and final_state.get("orders_sent") == 0):
                LOCK.unlink()  # durable proof the final gate prevented submission
                lock_owned = False
                print("BLOCKED: final review/freshness gate failed; orders sent: 0.")
                return 2
            if success:
                LOCK.unlink()
                lock_owned = False
            print("Terminal cancelled/zero/absent proof recorded." if success else
                  "BLOCKED/FAIL: lock retained; use cancel-only or manual demo reconciliation.")
            return 0 if success else 2
    except (Exception, KeyboardInterrupt):
        # Never emit exception text, request URLs, server text or account IDs.
        if lock_owned:
            try:
                state = json.loads((folder / "intent.json").read_text(encoding="utf-8"))
                if state.get("phase") == "prepared":
                    LOCK.unlink()  # no submission intent was durably entered
            except Exception:
                pass
        if folder.is_dir() and not args.cancel_only:
            try:
                durable(folder / "blocked.json", {"status": "BLOCKED", "orders_sent": "unknown" if lock_owned else 0,
                        "reason": "Gate/request/storage failure; no sensitive exception text retained."}, exclusive=True)
            except Exception:
                pass
        print("BLOCKED: gate/request/storage failure. Inspect local evidence; never blindly resubmit.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

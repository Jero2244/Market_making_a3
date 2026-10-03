"""Offline raw-observation audit. No network, credentials, book reconstruction or fills.

No enabling pathway is provided while exchange update/timestamp rules are unknown.
Local receipt differences below are wall-clock observations, not exchange latency.
"""

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path

from market_making.market_data.check_connection import instrument_id
from market_making.market_data.stream_market_data import (ENTRIES, auth_rejected, parse_market_frame,
                                server_timestamps, subscription_rejected)
from market_making.validation.validate_live import observation_acceptance, summarize, write_json
from market_making.paths import DEFAULT_REPLAY_POLICY


def receipt(value):
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp if stamp.tzinfo is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def entry_kind(data, key):
    if key not in data:
        return "absent"
    if data[key] is None:
        return "null"
    if data[key] == [] or data[key] == {}:
        return "empty"
    if number(data[key]) == 0:
        return "numeric_zero"
    return "present"


class UnverifiedBookGuard:
    """Fail-closed quarantine, NOT an implementation of exchange update rules.

    Never carries sides between messages; even two-sided Md or a claimed snapshot
    cannot establish a usable book. A reviewed rule implementation is still needed.
    """

    def __init__(self):
        self.reason = "Update rules and complete-refresh evidence unverified"
        self.generation = 0
        self.terminal = False

    @property
    def usable(self):
        return False

    def invalidate(self, reason, terminal=False):
        self.generation += 1
        self.reason = reason
        self.terminal |= terminal

    def observe(self, data):
        if self.terminal:
            self.reason = "Terminal error; no reuse"
        elif not all(isinstance(data.get(k), list) and data[k] for k in ("BI", "OF")):
            self.reason = "Missing/empty/partial sides; no carry-forward"
        else:
            self.reason = "Two-sided observation is not a verified snapshot/complete refresh"
        return self.usable


def level_issues(level, spec):
    if not isinstance(level, dict):
        return ["invalid_level"]
    price, size = number(level.get("price")), number(level.get("size"))
    issues = []
    for name, value, increment in (("price", price, spec.get("minPriceIncrement")),
                                    ("quantity", size, spec.get("tickSize"))):
        tick = number(increment)
        if value is None:
            issues.append("invalid_" + name)
        elif tick is None or tick <= 0:
            issues.append("unknown_" + name + "_increment")
        elif value % tick:
            issues.append(name + "_off_increment")
    if size is not None:
        if size <= 0:
            issues.append("nonpositive_quantity_deletion_semantics_unknown")
        for key, comparison in (("minTradeVol", lambda a, b: a < b),
                                 ("maxTradeVol", lambda a, b: a > b)):
            limit = number(spec.get(key))
            if limit is not None and comparison(size, limit):
                issues.append("quantity_outside_" + key)
        lot = number(spec.get("roundLot"))
        if lot is not None and lot > 0 and size % lot:
            issues.append("quantity_off_roundLot")
    if price is not None:
        for key, comparison in (("lowLimitPrice", lambda a, b: a < b),
                                 ("highLimitPrice", lambda a, b: a > b)):
            limit = number(spec.get(key))
            if limit is not None and comparison(price, limit):
                issues.append("price_outside_" + key)
    return issues


def quality(events, identity, spec, policy):
    availability = {k: {s: 0 for s in ("absent", "null", "empty", "numeric_zero", "present")}
                    for k in ENTRIES}
    rows, gaps, violations, order_errors, duplicates, repeated = [], [], [], [], [], []
    seen, seen_data = set(), set()
    previous = previous_line = None
    last_entries = {k: None for k in ENTRIES}
    missing_receipts, withheld, disconnects, terminal_lines = [], [], [], []
    guard = UnverifiedBookGuard()
    last_fault = None
    recovery = []
    stale_intervals = {k: [] for k in ENTRIES}
    threshold = policy["max_receipt_gap_seconds"]
    start = receipt(events[0].get("received_at_utc")) if events else None
    end = receipt(events[-1].get("received_at_utc")) if events else None
    for line, event in enumerate(events, 1):
        kind = event.get("event")
        now = receipt(event.get("received_at_utc"))
        if kind in ("connected", "disconnected", "gap", "authentication_rejected",
                    "subscription_rejected", "error"):
            guard.invalidate(kind, terminal=kind in ("authentication_rejected", "subscription_rejected", "error"))
            last_entries = {k: None for k in ENTRIES}
        if kind == "disconnected":
            disconnects.append(line)
            last_fault = (line, now)
        if kind in ("authentication_rejected", "subscription_rejected", "error"):
            terminal_lines.append(line)
        if kind != "message":
            continue
        message, retained = parse_market_frame(event.get("raw"))
        if auth_rejected(message) or subscription_rejected(message):
            terminal_lines.append(line)
            guard.invalidate("Raw authentication/subscription rejection", terminal=True)
        if not retained:
            withheld.append(line)
            guard.invalidate("Unretained/invalid frame")
            continue
        if instrument_id(message) != identity:
            continue
        data = message["marketData"]
        if now is None:
            missing_receipts.append(line)
            guard.invalidate("Unknown receipt time")
        elif previous is not None:
            gap = (now - previous).total_seconds()
            gaps.append({"from_line": previous_line, "to_line": line, "seconds": gap})
            if gap < 0:
                order_errors.append(line)
                guard.invalidate("Receipt clock regression")
            if gap > threshold:
                guard.invalidate("Receipt gap")
        if now is not None:
            previous, previous_line = now, line
        for key in ENTRIES:
            availability[key][entry_kind(data, key)] += 1
            if now is not None and key in data:
                before = last_entries[key]
                if before is not None and (now - before).total_seconds() > threshold:
                    stale_intervals[key].append({"start": (before + timedelta(seconds=threshold)).isoformat(),
                                                 "end": now.isoformat(), "to_line": line})
                last_entries[key] = now
        canonical = json.dumps(message, sort_keys=True, separators=(",", ":"))
        payload = json.dumps(data, sort_keys=True, separators=(",", ":"))
        if canonical in seen:
            duplicates.append(line)
        if payload in seen_data:
            repeated.append(line)
        seen.add(canonical)
        seen_data.add(payload)
        sides = {}
        for side in ("BI", "OF"):
            levels = data.get(side)
            sides[side] = []
            if levels not in (None, [], {}) and not isinstance(levels, list):
                violations.append({"line": line, "side": side, "issues": ["invalid_side_shape"]})
            if not isinstance(levels, list):
                continue
            for index, level in enumerate(levels):
                issues = level_issues(level, spec)
                if issues:
                    violations.append({"line": line, "side": side, "level": index, "issues": issues})
                if (isinstance(level, dict) and number(level.get("price")) is not None
                        and number(level.get("size")) is not None and number(level["size"]) > 0):
                    sides[side].append(level)
        # Only prices supplied together in THIS frame; never combine different Md.
        bid = max(sides["BI"], key=lambda x: x["price"]) if sides["BI"] else None
        offer = min(sides["OF"], key=lambda x: x["price"]) if sides["OF"] else None
        spread = float(number(offer["price"]) - number(bid["price"])) if bid and offer else None
        tick = number(spec.get("minPriceIncrement"))
        row = {"line": line, "received_at_utc": now.isoformat() if now else None,
               "bid": bid, "offer": offer, "spread": spread,
               "spread_ticks": float(Decimal(str(spread)) / tick) if spread is not None and tick and tick > 0 else None,
               "bid_quantity_sum": sum(x["size"] for x in sides["BI"]),
               "offer_quantity_sum": sum(x["size"] for x in sides["OF"]),
               "server_timestamps": server_timestamps(message),
               "volume": data.get("TV") if number(data.get("TV")) is not None else None,
               "book_usable": guard.observe(data)}
        rows.append(row)
        if last_fault is not None:
            recovery.append({"disconnect_line": last_fault[0], "new_data_line": line,
                             "receipt_wall_seconds_to_new_data": (now - last_fault[1]).total_seconds()
                             if now and last_fault[1] else None,
                             "seconds_to_valid_market_view": None,
                             "valid_market_view_status": "BLOCKED: no verified complete refresh"})
            last_fault = None
    for key, before in last_entries.items():
        if before and end and (end - before).total_seconds() > threshold:
            stale_intervals[key].append({"start": (before + timedelta(seconds=threshold)).isoformat(),
                                         "end": end.isoformat(), "to_line": len(events)})
    boundary_gaps = []
    if rows:
        first, last = rows[0], rows[-1]
        for a, b, from_line, to_line, label in (
                (start, receipt(first["received_at_utc"]), 1, first["line"], "start_to_first"),
                (receipt(last["received_at_utc"]), end, last["line"], len(events), "last_to_end")):
            if a and b:
                boundary_gaps.append({"kind": label, "from_line": from_line, "to_line": to_line,
                                      "start": a.isoformat(), "end": b.isoformat(),
                                      "seconds": (b - a).total_seconds()})
    gap_exclusions = [{**g, "reason": "Receipt gap exceeds prospective threshold; exchange gap unproven"}
                      for g in gaps + boundary_gaps if g["seconds"] > threshold]
    max_gap = max([g["seconds"] for g in gaps + boundary_gaps], default=None)
    receipt_coverage_complete = bool(
        rows and start and end and events[0].get("event") == "session"
        and events[-1].get("event") == "ended" and not missing_receipts
        and start <= end and all(start <= receipt(r["received_at_utc"]) <= end for r in rows)
        and not order_errors and len(boundary_gaps) == 2
        and all(g["seconds"] >= 0 for g in boundary_gaps))
    two_sided_fraction = sum(r["spread"] is not None for r in rows) / len(rows) if rows else 0
    crossed = [r["line"] for r in rows if r["spread"] is not None and r["spread"] < 0]
    comparisons = {
        "receipt_gap": {"measured": max_gap, "limit": threshold,
                        "evidence_complete": receipt_coverage_complete,
                        "within_limit": receipt_coverage_complete and max_gap is not None and max_gap <= threshold},
        "crossed_observations": {"measured": len(crossed), "limit": policy["max_crossed_observations"],
                                 "within_limit": len(crossed) <= policy["max_crossed_observations"]},
        "spec_violations": {"measured": len(violations), "limit": policy["max_spec_violations"],
                            "within_limit": len(violations) <= policy["max_spec_violations"]},
        "receipt_order_errors": {"measured": len(order_errors), "limit": policy["max_receipt_order_errors"],
                                 "within_limit": len(order_errors) <= policy["max_receipt_order_errors"]},
        "duplicate_frames": {"measured": len(duplicates), "limit": policy["max_duplicate_frames"],
                             "within_limit": len(duplicates) <= policy["max_duplicate_frames"]},
        "two_sided_fraction": {"measured": two_sided_fraction, "minimum": policy["min_two_sided_fraction"],
                               "within_limit": two_sided_fraction >= policy["min_two_sided_fraction"]}}
    return {"identity": {"marketId": identity[0], "symbol": identity[1]},
            "selected_message_count": len(rows), "availability": availability,
            "receipt_gaps": gaps, "boundary_receipt_gaps": boundary_gaps,
            "receipt_coverage_complete": receipt_coverage_complete,
            "max_receipt_gap_seconds": max_gap, "retrospective_threshold_comparisons": comparisons,
            "receipt_order_error_lines": order_errors, "missing_receipt_lines": missing_receipts,
            "exchange_order_errors": None, "exchange_order_status": "Unknown: sequencing rules unverified",
            "duplicate_frame_lines": duplicates, "repeated_market_payload_lines": repeated,
            "duplicate_note": "Repeated quotes with different frame timestamps are not duplicate frames",
            "crossed_observation_lines": crossed,
            "spec_violations": violations, "stale_receipt_intervals": stale_intervals,
            "staleness_scope": "Time since entry observation only, not quote age; null LA counts as observed null",
            "timestamp_message_count": sum(bool(r["server_timestamps"]) for r in rows),
            "withheld_frame_lines": withheld, "terminal_error_lines": terminal_lines,
            "disconnect_lines": disconnects, "recovery": recovery, "observations": rows,
            "excluded_intervals": [{"start": start.isoformat() if start else None,
                                    "end": end.isoformat() if end else None,
                                    "use": "All order-book/strategy replay",
                                    "reason": "Session, update/refresh rules and clocks unverified"}],
            "additional_gap_exclusions": gap_exclusions,
            "threshold_basis": "Retrospective comparison only; policy defined after this recording",
            "historical_clock_uncertainty_seconds": None}


def hashes(folder):
    return {p.name: sha256(p.read_bytes()).hexdigest() for p in sorted(folder.iterdir()) if p.is_file()}


def audit(original, output, policy_path):
    original, output = Path(original), Path(output)
    if output.resolve() == original.resolve() or original.resolve() in output.resolve().parents:
        raise ValueError("Output must be outside original evidence")
    provenance = hashes(original)
    report = json.loads((original / "report.json").read_text(encoding="utf-8"))
    events = [json.loads(s) for s in (original / "stream.jsonl").read_text(encoding="utf-8").splitlines()]
    spec = json.loads((original / "contract.json").read_text(encoding="utf-8"))["instrument"]
    policy = json.loads(Path(policy_path).read_text(encoding="utf-8"))
    identity = (report["selected"]["marketId"], report["selected"]["symbol"])
    if instrument_id(spec) != identity:
        raise ValueError("Contract specification identity mismatch")
    measured = quality(events, identity, spec, policy)
    summary = summarize(events, identity, expected_duration=report["duration_seconds"])
    matrix = [{"date_slot": date, "expiry_slot": expiry, "window": window,
               "window_buenos_aires": hours, "duration_seconds": policy["duration_seconds"],
               "status": "BLOCKED", "reason": "Confirmed date/available expiry and authorized capture not supplied"}
              for date in policy["capture_dates"] for expiry in policy["expiry_slots"]
              for window, hours in policy["windows_buenos_aires"].items()]
    decision = {"analysis_kind": "Offline acceptance audit, not a credentialed run",
                "original_file_sha256": provenance, "policy_sha256": sha256(Path(policy_path).read_bytes()).hexdigest(),
                "original_stages": report["stages"], "reviewer_approval": "PENDING independent review",
                "corrected_observational_acceptance": observation_acceptance(summary),
                "corrected_observational_summary": summary,
                "new_credentialed_runs": 0, "book_reconstructed": False,
                "capture_matrix": matrix,
                "existing_coverage": "One expiry, one unconfirmed date, middle-session observation only; does not complete prospective matrix",
                "uses": {
                    "raw_observation_exploration": {"status": "READY", "scope": "Offline raw-message inspection only", "fill_assumptions": "No fills, orders or P&L"},
                    "order_book_reconstruction": {"status": "BLOCKED", "reason": "Update, deletion, ordering and refresh rules unverified", "fill_assumptions": "None authorized"},
                    "quote_strategy_replay": {"status": "BLOCKED", "reason": "No valid book, verified session, clocks or adequate coverage", "fill_assumptions": "Displayed quotes do not prove executions; no queue/fill model approved"},
                    "time_dependent_replay": {"status": "BLOCKED", "reason": "Server timestamp semantics and historical clock uncertainty unknown", "fill_assumptions": "No latency or freshness-based fills"},
                    "trade_dependent_replay": {"status": "BLOCKED", "reason": "LA null and TV zero; no trade evidence", "fill_assumptions": "No inferred trades or fills from quote changes"}},
                "limitations": ["Demo liquidity is not production liquidity", "Local disconnect is not an exchange outage",
                                "Offline tests do not establish live trading safety", "Unknown historical secrets cannot be independently ruled out without issued-token registry"],
                "overall_replay_status": "BLOCKED"}
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "quality_report.json", measured)
    write_json(output / "readiness.json", decision)
    if provenance != hashes(original):
        raise ValueError("Original evidence changed during audit")
    return decision


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--policy", type=Path, default=DEFAULT_REPLAY_POLICY)
    args = parser.parse_args(argv)
    result = audit(args.original_dir, args.output_dir, args.policy)
    print("Replay: " + result["overall_replay_status"] + "; new credentialed runs: 0")


if __name__ == "__main__":
    main()

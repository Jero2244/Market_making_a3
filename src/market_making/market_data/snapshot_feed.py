"""Complete REST books for read-only/shadow consumers; no order API.

Receipt validity is bounded acquisition age, not exchange quote age. Each request
replaces the entire book. No streaming deltas or carried-forward sides are used.
"""
import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import sys
import time

import requests

from .check_connection import (PrimaryError, authenticate, get_json, instrument_id,
                               load_dotenv, require_field)
from .instrument_rules import number
from .stream_market_data import Recorder, validate_symbol
from market_making.ppi.deadline import DeadlineTransport


@dataclass(frozen=True)
class Contract:
    market_id: str
    symbol: str
    tick: Decimal
    lot: Decimal

    @classmethod
    def from_detail(cls, payload, market_id, symbol):
        detail = require_field(payload, "instrument", dict)
        if instrument_id(detail) != (market_id, symbol):
            raise PrimaryError("Instrument detail identity mismatch.")
        if not str(detail.get("cficode", "")).upper().startswith("F"):
            raise PrimaryError("Instrument is not a future.")
        tick, lot = number(detail.get("minPriceIncrement")), number(detail.get("roundLot"))
        if tick <= 0 or lot <= 0:
            raise PrimaryError("Missing/invalid contract tick or lot.")
        # Variable price grids need a separate adapter; never flatten them silently.
        if isinstance(detail.get("tickSize"), (list, dict)):
            raise PrimaryError("Variable tick schedule unsupported by snapshot adapter.")
        return cls(market_id, symbol, tick, lot)


def normalize_book(payload, contract):
    if not isinstance(payload, dict) or payload.get("status") != "OK":
        raise PrimaryError("Invalid snapshot status.")
    if "instrumentId" in payload:
        identity = payload["instrumentId"]
        if not isinstance(identity, dict) or instrument_id(identity) != (contract.market_id, contract.symbol):
            raise PrimaryError("Snapshot identity mismatch.")
    data = require_field(payload, "marketData", dict)
    sides = []
    for side in ("BI", "OF"):
        rows = require_field(data, side, list)
        levels = []
        for row in rows:
            if not isinstance(row, dict):
                raise PrimaryError("Malformed book level.")
            price, size = number(row.get("price")), number(row.get("size"))
            if price <= 0 or size <= 0 or price % contract.tick or size % contract.lot:
                raise PrimaryError("Invalid/off-grid price or size.")
            levels.append((price, size))
        if len({p for p, q in levels}) != len(levels):
            raise PrimaryError("Duplicate book prices.")
        sides.append(tuple(sorted(levels, reverse=side == "BI")))
    bids, asks = sides
    state = ("empty" if not bids and not asks else "one_sided" if not bids or not asks
             else "crossed_or_locked" if bids[0][0] >= asks[0][0] else "valid")
    return bids, asks, state


class SnapshotFeed:
    """Single-consumer feed with fail-closed refresh and connection generations.

    A validated observation is usable for shadow decisions only. Execution needs
    session, account, reference and provider freshness policy in the later runtime.
    """
    def __init__(self, contract, fetch, *, clock=time.monotonic, max_age=5, max_request=2):
        if any(not math.isfinite(v) or v <= 0 for v in (max_age, max_request)):
            raise ValueError("Age and acquisition limits must be finite and positive.")
        self.contract, self.fetch, self.clock = contract, fetch, clock
        self.max_age, self.max_request = max_age, max_request
        self.generation = self.sequence = 0
        self.connected = False
        self.invalidate("disconnected")

    def invalidate(self, state):
        self.state, self.bids, self.asks = state, (), ()
        self.request_started = self.received = self.received_at = None
        self.request_seconds = None

    def disconnect(self):
        self.connected = False
        self.invalidate("disconnected")

    def refresh(self):
        # Do not expose the old snapshot while a new request is in flight.
        self.invalidate("refreshing")
        started = self.clock()
        try:
            payload = self.fetch(self.contract, self.max_request)
            received = self.clock()
            received_at = datetime.now(timezone.utc).isoformat()
            bids, asks, state = normalize_book(payload, self.contract)
            elapsed = received - started
            if not math.isfinite(elapsed) or elapsed < 0 or elapsed > self.max_request:
                raise PrimaryError("Snapshot acquisition exceeded its validity budget.")
        except Exception:
            self.disconnect()
            self.state = "refresh_failed"
            raise
        if not self.connected:
            self.generation += 1
        self.connected = True
        self.sequence += 1
        self.bids, self.asks, self.state = bids, asks, state
        self.request_started, self.received = started, received
        self.received_at, self.request_seconds = received_at, elapsed
        return self.snapshot()

    def snapshot(self):
        now = self.clock()
        age = None if self.received is None else now - self.received
        acquisition_age = None if self.request_started is None else now - self.request_started
        state = self.state
        if acquisition_age is not None and (not math.isfinite(acquisition_age)
                                            or age < 0 or acquisition_age < 0
                                            or acquisition_age > self.max_age):
            state = "stale"
        return {"state": state, "usable_for_shadow": state == "valid" and self.connected,
                "provider": "remarkets", "marketId": self.contract.market_id,
                "symbol": self.contract.symbol, "generation": self.generation,
                "sequence": self.sequence, "connected": self.connected,
                "bids": [[str(p), str(q)] for p, q in self.bids],
                "asks": [[str(p), str(q)] for p, q in self.asks],
                "received_at_utc": self.received_at, "received_monotonic": self.received,
                "receipt_age_seconds": age, "acquisition_age_seconds": acquisition_age,
                "request_seconds": self.request_seconds, "exchange_quote_age_seconds": None,
                "identity_basis": "exact_catalog_detail_and_request",
                "validity_basis": "complete_REST_snapshot_bounded_acquisition_age"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Enable read-only network requests")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--market-id", default="ROFX")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--interval", type=float, default=5)
    parser.add_argument("--max-age", type=float, default=5)
    parser.add_argument("--max-request", type=float, default=2)
    parser.add_argument("--depth", type=int, choices=range(1, 6), default=2)
    parser.add_argument("--output", type=Path, help="New JSONL recording path")
    args = parser.parse_args(argv)
    if not args.live:
        parser.error("Use --live to enable read-only requests.")
    if args.iterations < 1 or any(not math.isfinite(v) or v <= 0
                                for v in (args.interval, args.max_age, args.max_request)):
        parser.error("Iterations and timing limits must be positive and finite.")
    load_dotenv(args.env_file)
    recorder = feed = transport = None
    try:
        user, password = os.getenv("PRIMARY_USER", ""), os.getenv("PRIMARY_PASSWORD", "")
        if not user or not password:
            raise PrimaryError("Primary credentials missing.")
        with requests.Session() as session:
            transport = DeadlineTransport(session)
            authenticate(session, user, password)
            catalog = get_json(session, "/rest/instruments/all")
            validate_symbol(require_field(catalog, "instruments", list), args.market_id, args.symbol)
            contract = Contract.from_detail(get_json(session, "/rest/instruments/detail",
                                           marketId=args.market_id, symbol=args.symbol),
                                           args.market_id, args.symbol)

            def fetch(target, timeout):
                with transport.bound(time.monotonic() + timeout, PrimaryError):
                    return get_json(session, "/rest/marketdata/get", marketId=target.market_id,
                                    symbol=target.symbol, entries="BI,OF", depth=args.depth,
                                    request_timeout=(min(5, timeout), timeout))

            feed = SnapshotFeed(contract, fetch, max_age=args.max_age, max_request=args.max_request)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                recorder = Recorder(args.output)
            for index in range(args.iterations):
                started = time.monotonic()
                record = {"event": "snapshot", **feed.refresh()}
                # This bounded shadow tool stops on any refresh or recording failure.
                if recorder:
                    recorder.write(record)
                print(json.dumps(record, allow_nan=False))
                if index + 1 < args.iterations:
                    time.sleep(max(0, args.interval - (time.monotonic() - started)))
        return 0
    except KeyboardInterrupt:
        return 130
    except (PrimaryError, OSError, ValueError):
        print("Snapshot receiver stopped; check credentials, connectivity and contract metadata.", file=sys.stderr)
        return 1
    finally:
        if feed:
            feed.disconnect()
        if recorder:
            recorder.close()
        if transport:
            transport.close()


if __name__ == "__main__":
    sys.exit(main())

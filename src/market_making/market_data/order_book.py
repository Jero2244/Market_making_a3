"""REST whole snapshots only. No WebSocket delta interpretation."""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import math
import time
from market_making.market_data.check_connection import PrimaryError, get_json, instrument_id
from market_making.market_data.instrument_rules import MARKET, SYMBOL, number


@dataclass(frozen=True)
class Book:
    bids: tuple
    asks: tuple
    received_at: datetime
    server_time: object
    state: str
    quote_age: object
    timestamp_age: object = None
    received_monotonic: float = 0.0
    clock_uncertainty: object = None
    timestamp_source: str = "absent"
    timestamp_authoritative: bool = False

    @property
    def bid(self):
        return self.bids[0][0] if self.bids else None

    @property
    def ask(self):
        return self.asks[0][0] if self.asks else None

    @property
    def spread(self):
        return self.ask - self.bid if self.bid is not None and self.ask is not None else None

    def summary(self):
        return {"state": self.state, "bids": [[str(p), str(q)] for p, q in self.bids],
                "asks": [[str(p), str(q)] for p, q in self.asks],
                "best_bid": str(self.bid) if self.bid is not None else None,
                "best_ask": str(self.ask) if self.ask is not None else None,
                "spread": str(self.spread) if self.spread is not None else None,
                "spread_ticks": str(self.spread / Decimal(100)) if self.spread is not None else None,
                "received_at_utc": self.received_at.isoformat(),
                "server_time": self.server_time, "quote_age_seconds": self.quote_age,
                "timestamp_source": self.timestamp_source,
                "timestamp_authoritative": self.timestamp_authoritative,
                "timestamp_derived_age_seconds": self.timestamp_age,
                "local_receipt_age_seconds": time.monotonic() - self.received_monotonic,
                "clock_uncertainty_seconds": self.clock_uncertainty,
                "exchange_freshness_at_receipt": "unknown" if self.quote_age is None else
                "fresh" if 0 <= self.quote_age <= 5 else "stale_or_future",
                "scope": "available REST depth; not a reconstructed/full exchange book"}

    def gate(self, now=None, max_age=5, monotonic_now=None):
        now = now or datetime.now(timezone.utc)
        if not self.timestamp_authoritative:
            raise PrimaryError("Exchange timestamp provenance unverified; send blocked.")
        elapsed = (time.monotonic() if monotonic_now is None else monotonic_now) - self.received_monotonic
        wall_elapsed = (now - self.received_at).total_seconds()
        if (self.clock_uncertainty is None or not math.isfinite(self.clock_uncertainty)
                or self.clock_uncertainty < 0):
            raise PrimaryError("Clock uncertainty unverified; send blocked.")
        if (self.state != "two_sided" or not math.isfinite(elapsed) or elapsed < 0
                or not math.isfinite(wall_elapsed) or wall_elapsed < 0
                or abs(wall_elapsed - elapsed) > self.clock_uncertainty
                or elapsed > min(max_age, 5)):
            raise PrimaryError("Missing/crossed/stale local snapshot.")
        if (self.quote_age is None or not math.isfinite(self.quote_age) or self.quote_age < 0
                or self.quote_age + elapsed + self.clock_uncertainty > min(max_age, 5)):
            raise PrimaryError("Exchange snapshot freshness unknown/stale; send blocked.")


def parse_snapshot(payload, received_at=None, requested_identity=None, received_monotonic=None):
    received_at = received_at or datetime.now(timezone.utc)
    receipt_mono = time.monotonic() if received_monotonic is None else received_monotonic
    if received_at.tzinfo is None:
        raise PrimaryError("Receipt clock must be timezone-aware.")
    identity = instrument_id(payload["instrumentId"]) if "instrumentId" in payload else requested_identity
    if payload.get("status") != "OK" or identity != (MARKET, SYMBOL):
        raise PrimaryError("Snapshot status/identity invalid.")
    data = payload.get("marketData")
    if not isinstance(data, dict):
        raise PrimaryError("Missing marketData object.")
    sides = []
    for side in ("BI", "OF"):
        levels = data.get(side, [])
        if not isinstance(levels, list):
            raise PrimaryError("Invalid book side.")
        parsed = []
        for level in levels:
            if not isinstance(level, dict):
                raise PrimaryError("Invalid book level.")
            p, q = number(level.get("price")), number(level.get("size"))
            if p <= 0 or q <= 0 or p % 100 or q % 1:
                raise PrimaryError("Invalid/off-grid book price or size.")
            parsed.append((p, q))
        sides.append(tuple(sorted(parsed, reverse=side == "BI")))
    bids, asks = sides
    state = "missing" if not bids or not asks else "crossed" if asks[0][0] <= bids[0][0] else "two_sided"
    # Diagnostic parsing only: no documented authoritative whole-book mapping.
    # Numeric units are unverified and deliberately NOT guessed.
    server = payload.get("timestamp")
    age = None
    if isinstance(server, str):
        try:
            stamp = datetime.fromisoformat(server.replace("Z", "+00:00"))
            if stamp.tzinfo is not None:
                age = (received_at - stamp).total_seconds()
        except ValueError:
            pass
    return Book(bids, asks, received_at, server, state, None, age, receipt_mono,
                None, "REST.top_level.timestamp (unverified)" if server is not None else "absent")


def fetch_book(session, depth=5):
    if depth not in range(1, 6):
        raise PrimaryError("Depth must be 1..5.")
    return parse_snapshot(get_json(session, "/rest/marketdata/get", marketId=MARKET,
                                  symbol=SYMBOL, entries="BI,OF", depth=depth),
                          requested_identity=(MARKET, SYMBOL))

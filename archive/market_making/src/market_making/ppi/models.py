"""Strict book normalization. Receipt time is NOT evidence of source freshness."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import math


def number(value, *, positive=True):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid_numeric_field")
    try:
        value = float(value)
    except OverflowError:
        raise ValueError("invalid_numeric_field") from None
    if not math.isfinite(value) or (value <= 0 if positive else value < 0):
        raise ValueError("invalid_numeric_field")
    return value


def timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


@dataclass(frozen=True)
class Level:
    price: float
    quantity: float


@dataclass
class Book:
    bid: Level | None
    ask: Level | None
    last_trade: float | None
    source_time: datetime | None
    last_trade_time: datetime | None
    receipt_time: datetime
    blockers: list[str] = field(default_factory=list)
    bids: tuple[Level, ...] = ()
    offers: tuple[Level, ...] = ()
    identity_basis: str | None = None
    requested_depth: int | None = None


def parse_book(data, current=None, *, receipt=None, source_semantics_verified=False, max_age=30):
    receipt = receipt or datetime.now(timezone.utc)
    blockers = []
    bid = ask = None
    normalized = {"bids": (), "offers": ()}
    if not isinstance(data, dict):
        data = {}
        blockers.append("malformed_book")
    for side, descending in (("bids", True), ("offers", False)):
        try:
            rows = data[side]
            if not isinstance(rows, list) or not rows:
                raise ValueError
            levels = [Level(number(x["price"]), number(x["quantity"])) for x in rows]
            # Duplicate prices have ambiguous depth; never sum silently.
            if len({x.price for x in levels}) != len(levels):
                raise ValueError
            normalized[side] = tuple(sorted(levels, key=lambda x: x.price, reverse=descending))
            best = normalized[side][0]
            if side == "bids":
                bid = best
            else:
                ask = best
        except (ValueError, KeyError, TypeError):
            blockers.append("invalid_or_missing_" + side)
    if bid and ask and bid.price >= ask.price:
        blockers.append("crossed_or_locked_book")
    source = timestamp(data.get("date"))
    if source is None:
        blockers.append("source_timestamp_missing_or_ambiguous")
    else:
        age = (receipt - source).total_seconds()
        if age < -2 or age > max_age:
            blockers.append("source_timestamp_stale_or_future")
    if not source_semantics_verified:
        blockers.append("source_timestamp_scope_clock_and_session_unverified")
    last = last_time = None
    if isinstance(current, dict):
        try:
            last = number(current.get("price"))
        except ValueError:
            blockers.append("invalid_last_trade")
        last_time = timestamp(current.get("date"))
    return Book(bid, ask, last, source, last_time, receipt, blockers,
                normalized["bids"], normalized["offers"])

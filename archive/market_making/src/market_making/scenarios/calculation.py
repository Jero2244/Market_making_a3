"""Signed futures P/L using an assumed TNA or the current implied rate."""
from dataclasses import dataclass
from datetime import date
import math

from .calendar import advance_sessions

CONTRACT_SHARES = 100
DEFAULT_EXPIRIES = {"2026-10": date(2026, 10, 30), "2026-12": date(2026, 12, 30)}
SYMBOLS = {"2026-10": "GGAL/OCT26", "2026-12": "GGAL/DIC26"}


def finite(value, label, *, positive=False):
    try:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError
        result = float(value)
        if not math.isfinite(result) or (positive and result <= 0):
            raise ValueError
        return result
    except (ValueError, OverflowError):
        raise ValueError(f"Enter a {'positive, ' if positive else ''}finite {label}.") from None


def validate_move(move):
    move = finite(move, "percentage move")
    if move <= -100:
        raise ValueError("The stock move must be greater than -100%.")
    return move


@dataclass(frozen=True)
class Position:
    expiry: str
    contracts: int  # Signed: positive = long, negative = short.
    entry_price: float | None = None
    current_price: float | None = None
    maturity: date | None = None


@dataclass(frozen=True)
class LegResult:
    expiry: str
    contracts: int
    implied_tna: float | None = None  # Percentage, not decimal.
    projected_future: float | None = None
    from_today: float | None = 0.0
    since_entry: float | None = 0.0
    reason: str | None = None
    projection_tna: float | None = None  # Actual rate used in the projection (%).


@dataclass(frozen=True)
class ScenarioResult:
    reference_date: date
    target_date: date
    projected_spot: float
    legs: tuple[LegResult, ...]
    total_from_today: float | None
    total_since_entry: float | None


def calculate(spot, move, positions, reference_date, *, horizon=2, assumed_tna=None):
    spot = finite(spot, "spot price", positive=True)
    move = validate_move(move)
    if assumed_tna is not None:
        assumed_tna = finite(assumed_tna, "assumed future TNA")
    target = advance_sessions(reference_date, horizon)
    projected = finite(spot * (1 + move / 100), "projected spot price", positive=True)
    legs = []
    seen = set()
    for position in positions:
        if position.expiry not in DEFAULT_EXPIRIES or position.expiry in seen:
            raise ValueError("Use distinct October and December positions.")
        seen.add(position.expiry)
        q = position.contracts
        if type(q) is not int:
            raise ValueError("Contracts must be whole numbers.")
        if q == 0:
            legs.append(LegResult(position.expiry, 0))
            continue
        entry = finite(position.entry_price, "entry price", positive=True)
        current = finite(position.current_price, "current futures price", positive=True)
        if type(position.maturity) is not date:
            raise ValueError("Enter the expiry as YYYY-MM-DD.")
        if position.maturity <= target:
            legs.append(LegResult(position.expiry, q, from_today=None, since_entry=None,
                                  reason="Scenario reaches or passes expiry"))
            continue
        days = (position.maturity - reference_date).days
        remaining = (position.maturity - target).days
        rate = finite((current / spot - 1) * 365 / days, "implied financing rate")
        projection_tna = finite(rate * 100, "implied TNA") if assumed_tna is None else assumed_tna
        future = finite(projected * (1 + (projection_tna / 100) * remaining / 365),
                        "projected future price", positive=True)
        try:
            change = finite(q * CONTRACT_SHARES * (future - current), "profit/loss")
            total = finite(q * CONTRACT_SHARES * (future - entry), "profit/loss")
        except OverflowError:
            raise ValueError("Position is too large to calculate a finite profit/loss.") from None
        legs.append(LegResult(position.expiry, q, finite(rate * 100, "implied TNA"), future, change, total,
                              projection_tna=projection_tna))
    unavailable = any(leg.reason for leg in legs)
    return ScenarioResult(reference_date, target, projected, tuple(legs),
                          None if unavailable else finite(sum(x.from_today for x in legs), "combined profit/loss"),
                          None if unavailable else finite(sum(x.since_entry for x in legs), "combined profit/loss"))


@dataclass(frozen=True)
class SimulatedPrice:
    price: float
    receipt_timestamp: str | None
    source: str


def simulated_midpoints(reports):
    """Only copy recognizable, non-crossed simulated GGAL futures books."""
    result = {}
    for report in reports:
        try:
            expiry = report["expiry"]
            book = report["books"]["future"]
            if (expiry not in SYMBOLS or book.get("symbol") != SYMBOLS[expiry]
                    or book.get("simulated") is not True
                    or book.get("provider") not in ("remarkets", "synthetic")):
                continue
            # Source timestamp caveats are advisory here; identity/book failures
            # must still prevent a numeric import.
            if any(not warning.startswith("source_timestamp_") for warning in book.get("diagnostics", [])):
                continue
            bid = finite(book["bids"][0]["price"], "bid", positive=True)
            ask = finite(book["offers"][0]["price"], "ask", positive=True)
            if bid >= ask:
                continue
            source = "Synthetic demo" if book["provider"] == "synthetic" else "REMARKETS simulated"
            result[expiry] = SimulatedPrice(bid / 2 + ask / 2, book.get("receipt_timestamp"), source)
        except (KeyError, TypeError, ValueError, IndexError):
            continue
    return result

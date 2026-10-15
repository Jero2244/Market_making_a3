"""Observed quotes and gross differences; observation age is not exchange age."""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import math

from market_making.ppi.models import Level, number, timestamp
from market_making.ppi.remarkets import parse_snapshot


@dataclass(frozen=True)
class Instrument:
    symbol: str
    a3_id: str
    tick: float = 1
    multiplier: float = 100
    currency: str = "ARS"
    unit: str = "share"
    maturity_date: str = "20261030"

    @property
    def display_currency(self):
        # A3 uses USG for its dollar-denominated WTI contract.
        return "USD" if self.currency == "USG" else self.currency

    @property
    def price_unit(self):
        return "points" if self.unit == "point" else f"{self.display_currency}/{self.unit}"

    @property
    def contract_size(self):
        if self.unit == "point":
            return f"{self.display_currency} {self.multiplier:g} per point per contract"
        return f"{self.multiplier:g} {self.unit}s per contract"


# Explicit mappings checked against A3's public reference data on 2026-10-15.
DEFAULT_INSTRUMENTS = {instrument.symbol: instrument for instrument in (
    Instrument("GGAL/OCT26", "rx_DUAL_GGAL_OCT26"),
    Instrument("GGAL/DIC26", "rx_DUAL_GGAL_DIC26", maturity_date="20261230"),
    Instrument("RFX20/OCT26", "rx_DUAL_RFX20_OCT26", tick=100, multiplier=1,
               unit="point"),
    Instrument("WTI/NOV26", "rx_DUAL_WTI_NOV26", tick=.01, multiplier=10,
               currency="USG", unit="barrel", maturity_date="20261117"),
)}
SYMBOLS = tuple(DEFAULT_INSTRUMENTS)
A3_IDS = {symbol: instrument.a3_id for symbol, instrument in DEFAULT_INSTRUMENTS.items()}


@dataclass(frozen=True)
class Quote:
    symbol: str
    source: str
    bid: Level | None
    ask: Level | None
    received_monotonic: float
    received_at: datetime
    last: float | None = None
    last_time: datetime | None = None
    problems: tuple[str, ...] = ()
    acquisition_seconds: float | None = None

    def age(self, now):
        return max(0, now - self.received_monotonic)

    def usable(self, now, max_age):
        if (self.problems or not math.isfinite(self.received_monotonic)
                or self.received_monotonic > now or self.age(now) > max_age):
            return False
        for level in (self.bid, self.ask):
            if level is not None:
                try:
                    number(level.price)
                    number(level.quantity)
                except ValueError:
                    return False
        return not (self.bid and self.ask and self.bid.price >= self.ask.price)


@dataclass(frozen=True)
class Difference:
    side: str
    price: float | None = None
    ticks: float | None = None
    percent: float | None = None
    per_contract: float | None = None
    remarkets_quantity: float | None = None
    reference_quantity: float | None = None
    qualifies: bool = False
    reason: str = "Waiting for both sources"


def compare(instrument, real, simulated, *, now, min_ticks=1, max_age=10, active=True):
    """Each direction needs its two relevant sides, not a last-price proxy."""
    if (not math.isfinite(min_ticks) or min_ticks < 0 or
            not math.isfinite(max_age) or max_age <= 0):
        raise ValueError("Use a non-negative minimum difference and a positive maximum age.")
    if (not math.isfinite(instrument.tick) or not math.isfinite(instrument.multiplier)
            or instrument.tick <= 0 or instrument.multiplier <= 0):
        raise ValueError("Invalid contract units.")
    reason = None
    if not active:
        reason = "Monitoring stopped"
    elif real is None or simulated is None:
        reason = "Waiting for both sources"
    elif (real.symbol != instrument.symbol or simulated.symbol != instrument.symbol
          or real.source != "A3" or simulated.source != "reMarkets"):
        reason = "Contract or source mismatch"
    elif real.problems or simulated.problems:
        reason = "Invalid quote / highlights paused"
    elif real.age(now) > max_age or simulated.age(now) > max_age:
        reason = "Old observation / highlights paused"
    elif not real.usable(now, max_age) or not simulated.usable(now, max_age):
        reason = "Invalid quote / highlights paused"
    if reason:
        return tuple(Difference(side, reason=reason) for side in ("BUY", "SELL"))
    results = []
    for side, reference, executable in (("BUY", real.bid, simulated.ask),
                                        ("SELL", real.ask, simulated.bid)):
        if reference is None or executable is None:
            results.append(Difference(side, reason="Required bid or ask is missing"))
            continue
        # Decimal arithmetic keeps a difference exactly at one tick eligible.
        ref, exe = Decimal(str(reference.price)), Decimal(str(executable.price))
        edge = ref - exe if side == "BUY" else exe - ref
        ticks = edge / Decimal(str(instrument.tick))
        qualifies = edge > 0 and ticks >= Decimal(str(min_ticks))
        results.append(Difference(side, float(edge), float(ticks), float(edge / ref * 100),
                                  float(edge * Decimal(str(instrument.multiplier))),
                                  executable.quantity, reference.quantity, qualifies,
                                  "Qualifying difference" if qualifies else "Below minimum difference"))
    return tuple(results)


def remarkets_quote(snapshot, symbol, *, now, receipt=None, acquisition_seconds=None):
    """Keep the existing identity/book checks, with timestamps advisory for this tab."""
    receipt = receipt or datetime.now(timezone.utc)
    instrument = {"provider": "remarkets", "market_id": "ROFX", "symbol": symbol}
    book = parse_snapshot(snapshot, instrument, receipt=receipt)
    advisory = {"source_timestamp_missing_or_ambiguous",
                "source_timestamp_scope_clock_and_session_unverified",
                "invalid_or_missing_BI", "invalid_or_missing_OF"}
    problems = [reason for reason in book.blockers if reason not in advisory]
    raw = snapshot.raw if hasattr(snapshot, "raw") else snapshot
    md = raw.get("marketData", {}) if isinstance(raw, dict) else {}
    if not isinstance(md, dict):
        md = {}
    # Explicit empty/missing sides are unavailable. Malformed sides invalidate
    # the contract, rather than quietly looking like an empty book.
    for side in ("BI", "OF"):
        if "invalid_or_missing_" + side in book.blockers and md.get(side) not in (None, []):
            problems.append("malformed_" + side)
    last = last_time = None
    if isinstance(md.get("LA"), dict):
        try:
            last = number(md["LA"].get("price"))
            last_time = timestamp(md["LA"].get("date"))
        except ValueError:
            pass
    return Quote(symbol, "reMarkets", book.bid, book.ask, now, receipt, last, last_time,
                 tuple(problems), acquisition_seconds)

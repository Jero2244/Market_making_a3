"""Fail-closed rules for the requested October 2026 ROFX future."""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from market_making.market_data.check_connection import PrimaryError, get_json, instrument_id, require_field

SYMBOL = "RFX20/OCT26"
MARKET = "ROFX"


def number(value):
    if isinstance(value, bool):
        raise PrimaryError("Boolean is not a number.")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise PrimaryError("Invalid numeric metadata.") from None
    if not result.is_finite():
        raise PrimaryError("Nonfinite number.")
    return result


@dataclass(frozen=True)
class Rules:
    symbol: str
    tick: Decimal
    low: Decimal
    high: Decimal
    expiry: str

    def validate_price(self, price):
        price = number(price)
        if not self.low <= price <= self.high or price % self.tick:
            raise PrimaryError("Price outside bands or off exact 100-point grid.")
        return price


def validate_detail(detail, now=None):
    now = now or datetime.now(timezone.utc)
    if instrument_id(detail) != (MARKET, SYMBOL):
        raise PrimaryError("Exact October identity mismatch.")
    if not str(detail.get("cficode", "")).startswith("F"):
        raise PrimaryError("Contract is not a confirmed future.")
    try:
        expiry = datetime.strptime(detail["maturityDate"], "%Y%m%d").date()
    except (KeyError, ValueError, TypeError):
        raise PrimaryError("Missing/invalid expiry.") from None
    if (expiry.year, expiry.month) != (2026, 10) or expiry <= now.date():
        raise PrimaryError("October contract is expired or expiry is inconsistent.")
    tick = number(detail.get("minPriceIncrement"))
    if tick != 100:
        raise PrimaryError("Price tick must be exactly 100.")
    ranges = detail.get("tickPriceRanges")
    if ranges is not None:
        if not isinstance(ranges, dict) or not ranges or any(
            not isinstance(r, dict) or number(r.get("tick")) != 100 for r in ranges.values()
        ):
            raise PrimaryError("Variable/conflicting tick ranges are unsupported.")
    minimum = number(detail.get("minTradeVol"))
    maximum = number(detail.get("maxTradeVol"))
    increment = number(detail.get("tickSize"))
    lot = number(detail.get("roundLot"))
    if not 0 < minimum <= 1 <= maximum or increment <= 0 or lot <= 0:
        raise PrimaryError("One contract is not permitted by size metadata.")
    if Decimal(1) % increment or Decimal(1) % lot or (Decimal(1)-minimum) % increment:
        raise PrimaryError("One contract is off the quantity grid.")
    if "LIMIT" not in detail.get("orderTypes", []) or "DAY" not in detail.get("timesInForce", []):
        raise PrimaryError("LIMIT/DAY unsupported.")
    low, high = number(detail.get("lowLimitPrice")), number(detail.get("highLimitPrice"))
    if not 0 < low < high:
        raise PrimaryError("Invalid price bands.")
    return Rules(SYMBOL, tick, low, high, expiry.isoformat())


def resolve(session, requested="RFX20/OCT", now=None):
    if requested not in ("RFX20/OCT", SYMBOL):
        raise PrimaryError("Only RFX20/OCT -> RFX20/OCT26 is supported; no fallback.")
    catalog = require_field(get_json(session, "/rest/instruments/all"), "instruments", list)
    matches = [item for item in catalog if isinstance(item, dict)
               and instrument_id(item) == (MARKET, SYMBOL)]
    if len(matches) != 1 or not str(matches[0].get("cficode", "")).startswith("F"):
        raise PrimaryError("Exact ROFX RFX20/OCT26 future unavailable/ambiguous; no fallback.")
    detail = require_field(get_json(session, "/rest/instruments/detail",
                                   marketId=MARKET, symbol=SYMBOL), "instrument", dict)
    return validate_detail(detail, now)

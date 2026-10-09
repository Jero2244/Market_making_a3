"""Explicit proxy assumptions, not a metadata verification/execution bypass."""
import json
import calendar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from .models import number, timestamp

TARGETS = ("2026-10", "2026-12")
SYMBOLS = {"2026-10": "GGAL/OCT26", "2026-12": "GGAL/DIC26"}

# Optional manual caucion TNA percentage. Leave None to display n/a, or set
# your rate here. --caucion-tna overrides this value for a single run.
CAUCION_TNA = None


def last_weekday_maturity(expiry):
    """User's calendar proxy: last Mon-Fri, end of day in Buenos Aires."""
    year, month = map(int, expiry.split('-'))
    maturity = datetime(year, month, calendar.monthrange(year, month)[1],
                        23, 59, 59, tzinfo=timezone(timedelta(hours=-3)))
    while maturity.weekday() >= 5:
        maturity -= timedelta(days=1)
    return maturity.isoformat()


def default_watch_config(caucion_tna=None):
    """Price/yield comparison using user assumptions, without trade inputs."""
    rate = CAUCION_TNA if caucion_tna is None else caucion_tna
    if rate is not None:
        rate = number(rate, positive=False)
    return {"quote_only": True, "spot": {"ticker": "GGAL", "type": "ACCIONES", "settlement": "INMEDIATA"},
            "futures": [{"expiry": expiry, "symbol": SYMBOLS[expiry], "market_id": "ROFX",
                         "provider": "remarkets", "maturity": last_weekday_maturity(expiry), "price_scale": 1}
                        for expiry in TARGETS],
            "rates": {"borrow_tna": rate, "lend_tna": None, "basis": 365}, "max_age_seconds": 30}


def _keys(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys.split()):
        raise ValueError("invalid_watch_config")


def _text(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 200
            or any(ord(c) < 32 for c in value)
            or any(x in value.upper() for x in ("REPLACE", "TODO", "PLACEHOLDER", "UNKNOWN", "<", ">"))):
        raise ValueError("watch_config_placeholder_or_invalid_text")
    return value


def validate_config(data):
    _keys(data, "spot futures rates threshold contracts max_age_seconds")
    _keys(data["rates"], "borrow_tna lend_tna basis provenance")
    rates = data["rates"]
    for key in ("borrow_tna", "lend_tna"):
        number(rates[key], positive=False)
    if type(rates["basis"]) is not int or rates["basis"] not in (360, 365):
        raise ValueError("invalid_rate_basis")
    _text(rates["provenance"])
    number(data["threshold"], positive=False)
    if type(data["contracts"]) is not int or data["contracts"] <= 0:
        raise ValueError("invalid_contract_size")
    if not 0 < number(data["max_age_seconds"]) <= 30:
        raise ValueError("invalid_max_age")
    spot = data["spot"]
    _keys(spot, "ticker type settlement underlying currency price_scale multiplier provenance")
    _instrument(spot)
    if spot["ticker"] != "GGAL" or spot["type"] != "ACCIONES" or spot["price_scale"] != 1 or spot["multiplier"] != 1:
        raise ValueError("ordinary_GGAL_ARS_share_required")
    futures = data["futures"]
    if not isinstance(futures, list) or len(futures) != 2:
        raise ValueError("exact_two_target_expiries_required")
    seen = set()
    ids = set()
    for future in futures:
        if isinstance(future, dict) and any(k in future for k in ("ticker", "type", "settlement")):
            raise ValueError("legacy_PPI_futures_not_supported_use_remarkets_symbol_and_market_id")
        _keys(future, "expiry maturity provider symbol market_id underlying currency price_scale multiplier provenance costs")
        if future["provider"] != "remarkets" or future["market_id"] != "ROFX":
            raise ValueError("remarkets_ROFX_futures_required")
        _text(future["symbol"])
        if future["symbol"] != SYMBOLS.get(future["expiry"]):
            raise ValueError("exact_GGAL_expiry_symbol_mapping_required")
        _text(future["provenance"])
        if future["underlying"] != "GGAL" or future["currency"] != "ARS":
            raise ValueError("matching_GGAL_ARS_required")
        number(future["price_scale"])
        number(future["multiplier"])
        maturity = timestamp(future["maturity"])
        if maturity is None or maturity.strftime("%Y-%m") != future["expiry"]:
            raise ValueError("explicit_aware_matching_maturity_required")
        seen.add(future["expiry"])
        ids.add((future["symbol"], future["market_id"]))
        _keys(future["costs"], "cash_carry reverse provenance")
        for direction in ("cash_carry", "reverse"):
            number(future["costs"][direction], positive=False)
        _text(future["costs"]["provenance"])
    if seen != set(TARGETS) or len(ids) != 2:
        raise ValueError("exact_distinct_target_expiries_required")
    return data


def _instrument(value):
    for key in ("ticker", "type", "settlement", "provenance"):
        _text(value[key])
    if value["underlying"] != "GGAL" or value["currency"] != "ARS" or value["settlement"] != "INMEDIATA":
        raise ValueError("matching_GGAL_ARS_immediate_settlement_required")
    number(value["price_scale"])
    number(value["multiplier"])


def load_watch_config(path):
    def reject_constant(value):
        raise ValueError("nonfinite_config")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_config_key")
            result[key] = value
        return result

    raw = Path(path).read_bytes()
    if len(raw) > 65536:
        raise ValueError("watch_config_too_large")
    return validate_config(json.loads(raw, parse_constant=reject_constant, object_pairs_hook=unique))

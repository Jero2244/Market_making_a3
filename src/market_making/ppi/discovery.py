"""Discover actual API candidates; a ticker/name is never contract metadata."""
from dataclasses import asdict
import re
from .client import PPIError, safe_error_code
from .models import parse_book


def label(value):
    """Bounded market identifiers, not arbitrary response strings."""
    if isinstance(value, str) and re.fullmatch(r"[\w ./+\-]{1,80}", value):
        return value
    return None


def discover(client, *, caucion_ticker=None):
    # REST marks Ticker required. Neither docs nor SDK establish empty-ticker
    # caucion enumeration or a one-day ticker. An explicit identifier is not
    # evidence of tenor, settlement compatibility or rate-side conventions.
    if caucion_ticker is not None and (not label(caucion_ticker) or not caucion_ticker.strip()):
        raise PPIError("invalid_caucion_ticker")
    report = {"provider": "PPI", "live_freshness_established": False, "executable": False,
              "settlements": [], "candidates": [], "blockers": [], "request_failures": []}

    def failure(category, stage, exc, **context):
        code = safe_error_code(exc)
        report["request_failures"].append({"category": category, "stage": stage, "code": code, **context})
        return code

    configuration = {}
    for name in ("InstrumentTypes", "Markets", "Settlements"):
        try:
            data = client.get("Configuration/" + name)
            if not isinstance(data, list) or not all(label(x) for x in data) or len(data) > 100:
                raise PPIError("malformed_configuration")
            configuration[name] = data
        except PPIError as exc:
            report["blockers"].append(name + ":" + failure("configuration", name, exc))
            configuration[name] = []
    report["settlements"] = configuration["Settlements"]
    for kind, ticker in (("ACCIONES", "GGAL"), ("FUTUROS", ""), ("CAUCIONES", caucion_ticker)):
        if kind not in configuration["InstrumentTypes"]:
            report["blockers"].append(kind + ":unsupported_or_unavailable")
            continue
        if kind == "CAUCIONES" and ticker is None:
            report["blockers"].append("CAUCIONES:broad_search_not_documented_explicit_ticker_required")
            continue
        try:
            rows = client.get("MarketData/SearchInstrument", {"Ticker": ticker, "Type": kind})
            if not isinstance(rows, list) or len(rows) > 10000:
                raise PPIError("malformed_instrument_search")
            candidates = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                fields = {k: label(row.get(k)) for k in ("ticker", "type", "market", "currency")}
                # PPI production names the local currency "Pesos", not "ARS".
                if fields["currency"] == "Pesos":
                    fields["currency"] = "ARS"
                if not all(fields.values()) or fields["type"] != kind:
                    continue
                if kind == "ACCIONES" and not (fields["ticker"] == "GGAL" and fields["market"] == "BYMA" and fields["currency"] == "ARS"):
                    continue
                if kind == "FUTUROS" and "GGAL" not in (str(row.get("ticker", "")) + " " + str(row.get("description", ""))).upper():
                    continue
                # Caucion currency is filtered but tenor is not guessed from ticker.
                if kind == "CAUCIONES" and (fields["currency"] != "ARS" or fields["ticker"] != ticker):
                    continue
                candidates.append(fields)
            if not candidates:
                report["blockers"].append(kind + ":no_matching_candidates_returned")
            if len(candidates) > 2:
                report["blockers"].append(kind + ":candidate_book_scan_truncated")
            for candidate_index, fields in enumerate(candidates[:2]):
                item = {**fields, "identity_verified": False, "books": []}
                report["candidates"].append(item)
                for settlement_index, settlement in enumerate(configuration["Settlements"][:3]):
                    params = {"Ticker": fields["ticker"], "Type": kind, "Settlement": settlement}
                    try:
                        book = client.get("MarketData/Book", params)
                        current = None
                        try:
                            current = client.get("MarketData/Current", params)
                        except PPIError as exc:
                            code = failure(kind, "Current", exc, candidate_index=candidate_index,
                                           settlement_index=settlement_index)
                            item.setdefault("blockers", []).append(settlement + ":Current:" + code)
                        item["books"].append({"settlement": settlement, **asdict(parse_book(book, current))})
                    except PPIError as exc:
                        code = failure(kind, "Book", exc, candidate_index=candidate_index,
                                       settlement_index=settlement_index)
                        item.setdefault("blockers", []).append(settlement + ":Book:" + code)
                if len(configuration["Settlements"]) > 3:
                    item.setdefault("blockers", []).append("settlement_book_scan_truncated")
        except PPIError as exc:
            report["blockers"].append(kind + ":SearchInstrument:" + failure(kind, "SearchInstrument", exc))
    report["blockers"].extend([
        "GGAL_ordinary_share_identity_requires_authoritative_metadata",
        "future_underlying_maturity_multiplier_price_units_and_delivery_rules_unavailable",
        "caucion_one_day_tenor_rate_units_day_basis_and_borrow_lend_side_mapping_unavailable",
        "supported_settlements_are_not_proof_of_instrument_settlement_compatibility",
        "source_timestamp_scope_clock_and_session_unverified",
        "fees_taxes_slippage_funding_stock_borrow_dividends_and_margin_unverified",
    ])
    return report

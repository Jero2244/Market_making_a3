"""Normalize simulated account snapshots; only outright GGAL futures enter P/L."""
from dataclasses import dataclass
from datetime import date, datetime, timezone
import re

from market_making.contracts import CONTRACT_SHARES, ggal_expiry
from .calculation import finite


class AccountDataError(ValueError):
    """A fixed, user-facing account import error, without remote error text."""


def _ok(payload):
    if not isinstance(payload, dict) or payload.get("status") != "OK":
        raise AccountDataError("reMarkets could not provide the account data. Try loading it again.")
    return payload


def _number(value, *, positive=False):
    try:
        return finite(value, "account value", positive=positive)
    except ValueError:
        return None


def calculation_time(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def select_account(payload, configured=""):
    rows = _ok(payload).get("accounts")
    if not isinstance(rows, list):
        raise AccountDataError("reMarkets returned an invalid account list.")
    names = {row["name"] for row in rows if isinstance(row, dict) and row.get("status") is not False
             and isinstance(row.get("name"), str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", row["name"])}
    if configured in names:
        return configured
    # A change of login must not keep using the old login's account name.
    if len(names) == 1:
        return names.pop()
    if not names:
        raise AccountDataError("This reMarkets login has no available trading account.")
    raise AccountDataError("This login has several accounts. Set PRIMARY_ACCOUNT in the selected credentials file.")


def position_rows(payload):
    rows = _ok(payload).get("positions")
    if not isinstance(rows, list):
        raise AccountDataError("reMarkets returned an invalid position list.")
    result, ignored = {}, 0
    for row in rows:
        if not isinstance(row, dict):
            raise AccountDataError("reMarkets returned an invalid position row.")
        symbol = row.get("tradingSymbol") or row.get("symbol")
        if ggal_expiry(symbol) is None:
            ignored += 1
            continue
        if row.get("symbol") not in (None, "", symbol) or symbol in result:
            raise AccountDataError("reMarkets returned conflicting GGAL position identifiers.")
        sizes = [_number(row.get(key)) for key in ("buySize", "sellSize")]
        if any(value is None or value < 0 or not value.is_integer() for value in sizes):
            raise AccountDataError("GGAL contract quantities must be non-negative whole numbers.")
        quantity = int(sizes[0]) - int(sizes[1])
        if quantity:
            result[symbol] = (quantity, row)
    if len(result) > 24:
        raise AccountDataError("This account has more than 24 GGAL expiries; the import limit is 24.")
    return result, ignored


def instrument_maturity(payload, symbol):
    detail = _ok(payload).get("instrument")
    if (not isinstance(detail, dict) or detail.get("instrumentId") != {"marketId": "ROFX", "symbol": symbol}
            or not str(detail.get("cficode", "")).startswith("F")
            or detail.get("currency") != "ARS"
            or _number(detail.get("contractMultiplier")) != CONTRACT_SHARES
            or _number(detail.get("priceConvertionFactor", 1)) != 1):
        raise AccountDataError("A GGAL future has incompatible currency, contract units or instrument metadata.")
    value = detail.get("maturityDate")
    try:
        maturity = datetime.strptime(value, "%Y%m%d").date()
        if maturity.strftime("%Y%m") != ggal_expiry(symbol).replace("-", ""):
            raise ValueError
        return maturity
    except (TypeError, ValueError, AttributeError):
        raise AccountDataError("A GGAL future has an invalid expiry date in reMarkets.") from None


@dataclass(frozen=True)
class AccountPosition:
    symbol: str
    contracts: int
    entry_price: float | None
    current_price: float | None
    maturity: date
    note: str = ""

    @property
    def expiry(self):
        return ggal_expiry(self.symbol)


@dataclass(frozen=True)
class AccountSnapshot:
    account: str
    positions: tuple[AccountPosition, ...]
    ignored_instruments: int
    current_cash: float | None
    available: float | None
    margin: float | None
    collateral: float | None
    calculated_at: datetime | None
    positions_calculated_at: datetime | None
    received_at: datetime


def normalize_account(account, positions_payload, details_payload, report_payload, instruments):
    summaries, ignored = position_rows(positions_payload)
    details = _ok(details_payload).get("detailedPosition")
    report = _ok(report_payload).get("accountData")
    if (not isinstance(details, dict) or details.get("account") != account
            or not isinstance(report, dict) or report.get("accountName") != account
            or report.get("hasError") is not False or not isinstance(details.get("report"), dict)):
        raise AccountDataError("The reMarkets reports do not confirm the selected account or contain a calculation error.")
    prices, detailed_quantities = {}, {}
    for kind, group in details["report"].items():
        if not isinstance(group, dict):
            raise AccountDataError("reMarkets returned an invalid detailed position report.")
        for symbol, instrument in group.items():
            if symbol not in summaries:
                continue
            if kind != "FUTURE" or not isinstance(instrument, dict):
                raise AccountDataError("A GGAL position was not classified as a future by reMarkets.")
            rows = instrument.get("detailedPositions")
            if not isinstance(rows, list):
                raise AccountDataError("A GGAL position has no usable detailed report.")
            values, quantities = set(), []
            for row in rows:
                if not isinstance(row, dict) or row.get("contractType") != "FUTURE":
                    raise AccountDataError("A GGAL position has incompatible contract details.")
                if row.get("currency", "ARS") != "ARS" or _number(row.get("priceConversionFactor", 1)) != 1:
                    raise AccountDataError("A GGAL account price uses incompatible currency or price units.")
                current = _number(row.get("marketPrice"), positive=True)
                if current is not None:
                    values.add(current)
                quantity = _number(row.get("totalCurrentSize"))
                if quantity is None or not quantity.is_integer():
                    raise AccountDataError("A GGAL position has an invalid detailed quantity.")
                quantities.append(int(quantity))
            if len(values) > 1:
                raise AccountDataError("A GGAL future has conflicting account market prices.")
            prices[symbol] = next(iter(values), None)
            detailed_quantities[symbol] = sum(quantities)
    positions = []
    for symbol, (quantity, row) in sorted(summaries.items(), key=lambda item: ggal_expiry(item[0])):
        if detailed_quantities.get(symbol) != quantity:
            raise AccountDataError("GGAL quantities changed between account reports. Refresh the account and try again.")
        side = "Buy" if quantity > 0 else "Sell"
        entry = _number(row.get(side.lower() + "Price"), positive=True)
        note = "API average entry; daily settlement may reset this basis."
        if entry is None:
            entry = _number(row.get("original" + side + "Price"), positive=True)
            note = "API original entry basis." if entry is not None else "Entry unavailable; use a manual simulation to fill it."
        if prices.get(symbol) is None:
            note += " Market price unavailable; use a manual simulation to fill it."
        positions.append(AccountPosition(symbol, quantity, entry, prices.get(symbol),
                                         instrument_maturity(instruments.get(symbol), symbol), note))
    reports = report.get("detailedAccountReports")
    reports = reports if isinstance(reports, dict) else {}
    # Settlement bucket 0 is the immediate available-to-operate report. Do not sum
    # balances across settlement dates, which would double-count account funds.
    immediate = reports.get("0", {})
    available = immediate.get("availableToOperate", {}) if isinstance(immediate, dict) else {}
    return AccountSnapshot(account, tuple(positions), ignored, _number(report.get("currentCash")),
                           _number(available.get("total")) if isinstance(available, dict) else None,
                           _number(report.get("margin")), _number(report.get("collateral")),
                           calculation_time(report.get("lastCalculation")),
                           calculation_time(details.get("lastCalculation")), datetime.now(timezone.utc))

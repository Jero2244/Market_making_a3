"""Shared GGAL contract assumptions for the checker and position scenarios.

These dates and units are user assumptions, not verified exchange metadata.
Edit this file when rolling contracts, then restart the app (and rebuild the exe).
Scenario expiry edits and reviewed full-assessment files remain explicit overrides.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re


BA = timezone(timedelta(hours=-3))
CONTRACT_SHARES = 100
DAY_BASIS = 365

GGAL_MONTHS = ("ENE", "FEB", "MAR", "ABR", "MAY", "JUN", "JUL", "AGO", "SEP", "OCT", "NOV", "DIC")


def ggal_expiry(symbol):
    """Recognize outright GGAL futures only, excluding stock and options."""
    if not isinstance(symbol, str):
        return None
    match = re.fullmatch(r"GGAL/([A-Z]{3})([0-9]{2})", symbol)
    if match is None or match[1] not in GGAL_MONTHS:
        return None
    return f"20{match[2]}-{GGAL_MONTHS.index(match[1]) + 1:02d}"


@dataclass(frozen=True)
class ContractSettings:
    expiry: str
    symbol: str
    label: str
    maturity: datetime
    market_id: str = "ROFX"
    provider: str = "remarkets"

    @property
    def month_label(self):
        return self.label.rsplit(" ", 1)[0]

    def watch_instrument(self):
        return {"expiry": self.expiry, "symbol": self.symbol,
                "market_id": self.market_id, "provider": self.provider,
                "maturity": self.maturity.isoformat(), "price_scale": 1,
                "multiplier": CONTRACT_SHARES}


CONTRACTS = {contract.expiry: contract for contract in (
    ContractSettings("2026-10", "GGAL/OCT26", "October 2026",
                     datetime(2026, 10, 30, 23, 59, 59, tzinfo=BA)),
    ContractSettings("2026-12", "GGAL/DIC26", "December 2026",
                     datetime(2026, 12, 30, 23, 59, 59, tzinfo=BA)),
)}
TARGETS = tuple(CONTRACTS)
SYMBOLS = {expiry: contract.symbol for expiry, contract in CONTRACTS.items()}
DEFAULT_EXPIRIES = {expiry: contract.maturity.date() for expiry, contract in CONTRACTS.items()}

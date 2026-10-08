"""Per-share ARS cash-and-carry research; never an execution recommendation."""
from dataclasses import dataclass
from datetime import datetime, timezone
import math
from .models import Book, number


MANUAL_CHECK_DIAGNOSTICS = frozenset({
    "source_timestamp_missing_or_ambiguous", "source_timestamp_stale_or_future",
    "source_timestamp_scope_clock_and_session_unverified", "invalid_last_trade",
})
MANUAL_FRESHNESS_CAVEAT = "manual_price_comparison_only_source_freshness_not_established"


def price_diagnostics(diagnostics, manual_check=False):
    """Partition diagnostics without modifying the source books."""
    return ([x for x in diagnostics if not (manual_check and x in MANUAL_CHECK_DIAGNOSTICS)],
            [x for x in diagnostics if manual_check and x in MANUAL_CHECK_DIAGNOSTICS])


def finite(value):
    """Validate signed computed values as well as arithmetic intermediates."""
    try:
        value = float(value)
        if not math.isfinite(value):
            raise ValueError
        return value
    except (ValueError, TypeError, OverflowError):
        raise ValueError("nonfinite_calculation") from None


def period_rate(tna_percent, days, basis):
    try:
        tna = number(tna_percent, positive=False)
        days = number(days)
        if basis not in (360, 365):
            raise ValueError("day_basis_must_be_explicit_360_or_365")
        return finite(finite(tna / 100 * days) / basis)
    except OverflowError:
        raise ValueError("nonfinite_calculation") from None


def fair_future(spot, tna_percent, days, basis):
    try:
        return finite(number(spot) * finite(1 + period_rate(tna_percent, days, basis)))
    except OverflowError:
        raise ValueError("nonfinite_calculation") from None


@dataclass(frozen=True)
class ContractMetadata:
    """Reviewed authoritative metadata, NOT SearchInstrument output.

    price_scale converts quoted futures price to ARS/share; multiplier is shares
    per contract. Source must be reviewed outside this adapter before setting
    verified. A symbol containing GGAL alone does not satisfy this contract.
    """
    underlying: str
    currency: str
    settlement: str
    maturity: datetime
    multiplier: float
    price_scale: float
    source: str
    verified: bool = False
    ordinary_share: bool = False
    delivery_compatible: bool = False


def assess(spot: Book, future: Book, metadata: ContractMetadata, *, spot_settlement,
           borrow_tna=None, lend_tna=None, basis=None, costs=None, contracts=1,
           funding_locked=False, stock_borrow_verified=False,
            dividends_verified=False, margin_verified=False, now=None, manual_check=False):
    """Edges use best executable sides, costs in ARS/share, depth in contracts.

    Unknown prerequisites block an executable claim even when a theoretical edge
    is positive. One-day funding rolled to maturity is hypothetical, not locked.
    Default assessment time is current aware UTC, never a saved receipt time.
    Explicit aware ``now`` is reserved for historical replay against that instant;
    replay output is not evidence of current freshness or an unexpired contract.
    """
    book_blockers, caveats = price_diagnostics(list(spot.blockers) + list(future.blockers), manual_check)
    blockers = list(book_blockers)
    if manual_check:
        caveats.append(MANUAL_FRESHNESS_CAVEAT)
    result = {"formula": "F=S(1+r)", "executable": False, "cash_carry_edge": None,
              "manual_check": manual_check, "live_freshness_established": False, "caveats": caveats,
              "reverse_edge": None, "cash_carry_capacity_contracts": None,
              "reverse_capacity_contracts": None, "blockers": blockers}
    if not metadata.verified or not metadata.source or not metadata.ordinary_share or not metadata.delivery_compatible or metadata.underlying != "GGAL" or metadata.currency != "ARS":
        blockers.append("authoritative_matching_GGAL_ARS_contract_metadata_required")
    if metadata.settlement != spot_settlement:
        blockers.append("settlement_mismatch")
    if spot_settlement != "INMEDIATA":
        blockers.append("dated_cash_settlement_funding_horizon_adjustment_required")
    if not funding_locked:
        blockers.append("overnight_rollover_is_hypothetical_not_locked_maturity_funding")
    if not stock_borrow_verified:
        blockers.append("stock_borrow_unverified")
    if not dividends_verified:
        blockers.append("dividends_unverified")
    if not margin_verified:
        blockers.append("margin_and_variation_margin_funding_unverified")
    if costs is None:
        blockers.append("all_in_costs_unknown")
    now = datetime.now(timezone.utc) if now is None else now
    freshness = []
    for book in (spot, future):
        if book.source_time is None or book.source_time.tzinfo is None or now.tzinfo is None:
            freshness.append("source_timestamp_missing_or_ambiguous")
        elif not -2 <= (now - book.source_time).total_seconds() <= 30:
            freshness.append("source_timestamp_stale_or_future")
    blocking, advisory = price_diagnostics(freshness, manual_check)
    blockers.extend(blocking)
    caveats.extend(advisory)
    try:
        if metadata.maturity.tzinfo is None or now.tzinfo is None:
            raise ValueError
        days = (metadata.maturity - now).total_seconds() / 86400
        number(days)
        multiplier = number(metadata.multiplier)
        scale = number(metadata.price_scale)
        requested = number(contracts)
        if requested != int(requested):
            raise ValueError
    except (ValueError, TypeError, AttributeError, OverflowError):
        blockers.append("invalid_or_expired_contract_horizon_units_or_size")
        return result
    result["days_to_maturity"] = days
    # Do not normalize prices with unverified identity, units or settlement.
    if any(x in blockers for x in ("authoritative_matching_GGAL_ARS_contract_metadata_required", "settlement_mismatch", "dated_cash_settlement_funding_horizon_adjustment_required", "source_timestamp_missing_or_ambiguous", "source_timestamp_stale_or_future")) or book_blockers:
        return result
    try:
        if not all((spot.bid, spot.ask, future.bid, future.ask)):
            raise ValueError
        for book in (spot, future):
            for level in (book.bid, book.ask):
                number(level.price)
                number(level.quantity)
            if book.bid.price >= book.ask.price:
                raise ValueError
        charge = number(costs, positive=False) if costs is not None else None
        result["cash_carry_capacity_contracts"] = math.floor(min(finite(spot.ask.quantity / multiplier), future.bid.quantity))
        result["reverse_capacity_contracts"] = math.floor(min(finite(spot.bid.quantity / multiplier), future.ask.quantity))
        computed = {}
        for direction, tna, capacity in (("cash_carry", borrow_tna, result["cash_carry_capacity_contracts"]), ("reverse", lend_tna, result["reverse_capacity_contracts"])):
            if capacity < requested:
                blockers.append(direction + "_insufficient_depth")
            if tna is None:
                blockers.append(direction + "_rate_unavailable")
                continue
            rate = period_rate(tna, days, basis)
            computed[direction + "_period_rate"] = rate
            if charge is not None:
                factor = finite(1 + rate)
                financed_spot = finite((spot.ask.price if direction == "cash_carry" else spot.bid.price) * factor)
                normalized_future = finite((future.bid.price if direction == "cash_carry" else future.ask.price) * scale)
                computed[direction + "_theoretical"] = financed_spot
                computed[direction + "_observed"] = normalized_future
                computed[direction + "_difference"] = finite(financed_spot - normalized_future)
                gross_edge = finite(normalized_future - financed_spot if direction == "cash_carry" else financed_spot - normalized_future)
                computed[direction + "_edge"] = finite(gross_edge - charge)
        result.update(computed)
    except (ValueError, TypeError, OverflowError):
        blockers.append("invalid_book_rate_basis_or_cost")
    # This adapter intentionally never claims executability: legal/account-specific
    # feasibility and simultaneous fills are outside read-only market data.
    blockers.append("read_only_research_cannot_establish_simultaneous_execution")
    return result

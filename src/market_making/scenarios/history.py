"""Normalize PPI daily price records without filling gaps or trimming extremes."""
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
import math

from .calendar import sessions_between, validate_horizon
from .calculation import finite, validate_move

MIN_WINDOWS = 30


def record_date(value):
    """Daily timestamps label the session date; midnight UTC is not shifted."""
    if not isinstance(value, str):
        raise ValueError("Invalid daily date.")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.date()


def lookback_start(end, years):
    if type(years) is not int or years not in (1, 3, 5):
        raise ValueError("Choose 1, 3, or 5 years of history.")
    try:
        return end.replace(year=end.year - years)
    except ValueError:
        return end.replace(year=end.year - years, day=28)


def annual_chunks(start, end):
    """Inclusive, disjoint annual query ranges, at most six for a five-year view."""
    chunks = []
    cursor = start
    while cursor <= end:
        finish = min(end, date(cursor.year, 12, 31))
        chunks.append((cursor, finish))
        cursor = finish + timedelta(days=1)
    return tuple(chunks)


@dataclass(frozen=True)
class HistorySample:
    closes: tuple[tuple[date, float], ...]
    returns: tuple[float, ...]  # Decimal returns over `horizon` trading sessions.
    expected_sessions: int
    invalid_records: int
    conflicting_dates: int
    excluded_windows: int
    requested_start: date
    requested_end: date
    horizon: int = 2


def normalize_history(records, start, end, *, completed_before, horizon=2):
    validate_horizon(horizon)
    if not isinstance(records, list):
        raise ValueError("Invalid historical data response.")
    values, rejected, conflicts = {}, set(), set()
    invalid = 0
    sessions = sessions_between(start, min(end, completed_before - timedelta(days=1)))
    allowed = set(sessions)
    for record in records:
        day = None
        try:
            day = record_date(record["date"])
            if day not in allowed:
                continue
            price = finite(record["price"], "historical daily price", positive=True)
            if day in values and values[day] != price:
                conflicts.add(day)
            else:
                values[day] = price
        except (KeyError, TypeError, ValueError, OverflowError):
            invalid += 1
            if day in allowed:
                rejected.add(day)
    for day in rejected | conflicts:
        values.pop(day, None)
    base = HistorySample(tuple(sorted(values.items())), (), len(sessions), invalid,
                         len(conflicts), 0, start, min(end, completed_before - timedelta(days=1)), horizon)
    return with_horizon(base, horizon)


def with_horizon(sample, horizon):
    """Recompute actual N-session returns from cached closes, without refetching."""
    validate_horizon(horizon)
    values = dict(sample.closes)
    sessions = sessions_between(sample.requested_start, sample.requested_end)
    returns = []
    excluded = 0
    # A missing day anywhere in a window makes its horizon ambiguous. A prefix
    # count checks this in O(1), including longer horizons.
    missing = [0]
    for day in sessions:
        missing.append(missing[-1] + (day not in values))
    for index in range(max(0, len(sessions) - horizon)):
        window = sessions[index:index + horizon + 1]
        if missing[index + horizon + 1] != missing[index]:
            excluded += 1
            continue
        value = values[window[-1]] / values[window[0]] - 1
        if value <= -1 or not math.isfinite(value) or not math.isfinite(value * 100):
            excluded += 1
            continue
        returns.append(value)
    return replace(sample, returns=tuple(returns), expected_sessions=len(sessions),
                   excluded_windows=excluded, horizon=horizon)


@dataclass(frozen=True)
class Frequency:
    matches: int
    observations: int
    percentage: float | None
    direction: str


def historical_frequency(sample, move):
    move = validate_move(move)
    target = move / 100
    # Inclusive thresholds, with a tight tolerance for compounding roundoff.
    def matches(value):
        equal = math.isclose(value, target, rel_tol=1e-12, abs_tol=1e-12)
        return equal or (value >= target if move >= 0 else value <= target)
    count = sum(matches(value) for value in sample.returns)
    n = len(sample.returns)
    return Frequency(count, n, count / n * 100 if n >= MIN_WINDOWS else None,
                     "up" if move > 0 else "down" if move < 0 else "nonnegative")


def histogram(returns, move, bins=30):
    """Percentage domain includes every observation and the selected target."""
    if not returns:
        return (), (), 0.0, 1.0
    values = [finite(value * 100, "historical return") for value in returns]
    low, high = min(min(values), move), max(max(values), move)
    if high == low:
        low, high = low - 0.5, high + 0.5
    width = (high - low) / bins
    counts = [0] * bins
    for value in values:
        index = min(bins - 1, max(0, int((value - low) / width)))
        counts[index] += 1
    edges = tuple(low + i * width for i in range(bins + 1))
    return edges, tuple(counts), low, high

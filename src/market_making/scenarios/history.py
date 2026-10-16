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


def horizon_windows(sample, horizon, excluded_days=()):
    """(start, end) session pairs of complete N-session windows, plus the
    number of excluded windows. A window is excluded when any of its sessions
    is missing or when any daily return inside it ends on an excluded day."""
    validate_horizon(horizon)
    values = dict(sample.closes)
    excluded_days = set(excluded_days)
    sessions = sessions_between(sample.requested_start, sample.requested_end)
    # A missing day anywhere in a window makes its horizon ambiguous. Prefix
    # counts check this in O(1), including longer horizons.
    missing, flagged = [0], [0]
    for day in sessions:
        missing.append(missing[-1] + (day not in values))
        flagged.append(flagged[-1] + (day in excluded_days))
    windows = []
    excluded = 0
    for index in range(max(0, len(sessions) - horizon)):
        if (missing[index + horizon + 1] != missing[index]
                or flagged[index + horizon + 1] != flagged[index + 1]):
            excluded += 1
            continue
        windows.append((sessions[index], sessions[index + horizon]))
    return windows, excluded, len(sessions)


def with_horizon(sample, horizon, excluded_days=()):
    """Recompute actual N-session returns from cached closes, without refetching."""
    values = dict(sample.closes)
    windows, excluded, expected = horizon_windows(sample, horizon, excluded_days)
    returns = []
    for first, last in windows:
        value = values[last] / values[first] - 1
        if value <= -1 or not math.isfinite(value) or not math.isfinite(value * 100):
            excluded += 1
            continue
        returns.append(value)
    return replace(sample, returns=tuple(returns), expected_sessions=expected,
                   excluded_windows=excluded, horizon=horizon)


def daily_log_returns(sample):
    """Chronological (session, log return) pairs between consecutive sessions
    that both have a close; a missing session breaks the chain, never bridges it."""
    values = dict(sample.closes)
    sessions = sessions_between(sample.requested_start, sample.requested_end)
    result = []
    for previous, day in zip(sessions, sessions[1:]):
        if previous in values and day in values:
            result.append((day, math.log(values[day] / values[previous])))
    return tuple(result)


@dataclass(frozen=True)
class Jump:
    day: date
    log_return: float
    robust_z: float  # Distance from the median in MAD-based standard deviations.


def flag_jumps(daily, threshold=6.0):
    """Daily moves far outside the robust spread, largest first.

    Catches unadjusted splits and large data errors. Ordinary dividends are a
    few percent and cannot be told apart from normal GGAL volatility this way.
    """
    if len(daily) < MIN_WINDOWS:
        return ()
    logs = sorted(value for _, value in daily)
    middle = len(logs) // 2
    median = logs[middle] if len(logs) % 2 else (logs[middle - 1] + logs[middle]) / 2
    deviations = sorted(abs(value - median) for value in logs)
    mad = deviations[middle] if len(deviations) % 2 else (deviations[middle - 1] + deviations[middle]) / 2
    scale = 1.4826 * mad
    if scale <= 0:
        return ()
    jumps = [Jump(day, value, (value - median) / scale) for day, value in daily
             if abs(value - median) / scale >= threshold]
    return tuple(sorted(jumps, key=lambda jump: -abs(jump.robust_z)))


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

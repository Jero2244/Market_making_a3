"""Optional drift and volatility adjustments of the historical sample.

Defaults reproduce the raw history exactly. Adjustments work in log growth so
every adjusted outcome keeps a positive price:

- Drift: "Historical" keeps the sample's own trend; "Zero" removes it;
  "Assumed TNA" centres the outcomes on the scenario's financing rate.
- Volatility: "Current (EWMA)" is filtered historical simulation. Each daily
  move is divided by the EWMA volatility of its own day and multiplied by
  today's, so calm and turbulent periods are both expressed at today's level.
"""
from dataclasses import dataclass, replace
import math

from .calendar import advance_sessions
from .calculation import finite
from .history import MIN_WINDOWS, daily_log_returns, flag_jumps, horizon_windows, with_horizon

DRIFT_MODES = ("Historical", "Zero", "Assumed TNA")
VOLATILITY_MODES = ("Equal weight", "Current (EWMA)")
EWMA_LAMBDA = .94  # RiskMetrics daily decay.


@dataclass(frozen=True)
class ModelSettings:
    drift: str = "Historical"
    volatility: str = "Equal weight"
    exclude_jumps: bool = False
    drift_tna: float | None = None  # Percentage; required for "Assumed TNA".

    @property
    def adjusted(self):
        return self.drift != "Historical" or self.volatility != "Equal weight" or self.exclude_jumps

    def label(self):
        parts = [{"Historical": "historical drift", "Zero": "zero drift",
                  "Assumed TNA": f"drift at {self.drift_tna:g}% TNA" if self.drift_tna is not None
                  else "TNA drift"}[self.drift],
                 "equal-weight volatility" if self.volatility == "Equal weight" else "EWMA current volatility"]
        if self.exclude_jumps:
            parts.append("flagged jumps excluded")
        return ", ".join(parts)


def validate_settings(settings):
    if settings.drift not in DRIFT_MODES:
        raise ValueError("Choose a supported drift model.")
    if settings.volatility not in VOLATILITY_MODES:
        raise ValueError("Choose a supported volatility model.")
    if settings.drift == "Assumed TNA":
        if settings.drift_tna is None:
            raise ValueError("Enter an assumed future TNA to use it as the drift.")
        finite(settings.drift_tna, "drift TNA")
    return settings


@dataclass(frozen=True)
class ModelSample:
    sample: object                       # HistorySample with adjusted N-day returns.
    daily: tuple[tuple, ...]             # (session, adjusted daily log return), chronological.
    jumps: tuple                         # Flagged raw daily moves, largest first.
    current_volatility: float | None     # Daily log-return sd used for scaling, if any.
    settings: ModelSettings


def ewma_scaled(daily, decay=EWMA_LAMBDA):
    """Rescale each daily move to today's EWMA volatility around the mean."""
    logs = [value for _, value in daily]
    if len(logs) < MIN_WINDOWS:
        return tuple(daily), None
    mean = math.fsum(logs) / len(logs)
    seed = logs[:MIN_WINDOWS]
    variance = math.fsum((value - mean) ** 2 for value in seed) / (len(seed) - 1)
    if variance <= 0:
        return tuple(daily), None
    scaled_sd = []
    for value in logs:
        scaled_sd.append(math.sqrt(variance))  # Forecast made before this day's move.
        variance = decay * variance + (1 - decay) * (value - mean) ** 2
    current = math.sqrt(variance)
    floor = 1e-6
    return tuple((day, mean + (value - mean) * current / max(sd, floor))
                 for (day, value), sd in zip(daily, scaled_sd)), current


def drift_per_day(settings, reference_date, horizon):
    """Target mean daily log growth, or None to keep the sample's own drift."""
    if settings.drift == "Historical":
        return None
    if settings.drift == "Zero":
        return 0.0
    calendar_days = (advance_sessions(reference_date, horizon) - reference_date).days
    growth = 1 + settings.drift_tna / 100 * calendar_days / 365
    if growth <= 0:
        raise ValueError("The drift TNA implies a non-positive price.")
    return math.log(growth) / horizon


def model_sample(history, horizon, settings, reference_date):
    """Apply the selected settings to cached history, without refetching."""
    validate_settings(settings)
    raw_daily = daily_log_returns(history)
    jumps = flag_jumps(raw_daily)
    excluded = {jump.day for jump in jumps} if settings.exclude_jumps else set()
    sample = with_horizon(history, horizon, excluded)
    daily = tuple((day, value) for day, value in raw_daily if day not in excluded)
    if not settings.adjusted:
        return ModelSample(sample, daily, jumps, None, settings)
    current = None
    if settings.volatility == "Current (EWMA)":
        daily, current = ewma_scaled(daily)
    target = drift_per_day(settings, reference_date, horizon)
    by_day = dict(daily)
    windows, _, _ = horizon_windows(history, horizon, excluded)
    logs = []
    if settings.volatility == "Current (EWMA)":
        # Rebuild each N-day window from its rescaled daily moves.
        sessions = sorted(by_day)
        position = {day: index for index, day in enumerate(sessions)}
        prefix = [0.0]
        for day in sessions:
            prefix.append(prefix[-1] + by_day[day])
        for _, last in windows:
            # Complete windows have every session, so their daily returns are
            # consecutive entries ending at `last`.
            end = position[last] + 1
            logs.append(prefix[end] - prefix[end - horizon])
    else:
        logs = [math.log1p(value) for value in sample.returns]
    if target is not None and logs:
        shift = target * horizon - math.fsum(logs) / len(logs)
        logs = [value + shift for value in logs]
        if daily:
            daily_shift = target - math.fsum(value for _, value in daily) / len(daily)
            daily = tuple((day, value + daily_shift) for day, value in daily)
    returns = []
    for value in logs:
        simple = math.expm1(value)
        if math.isfinite(simple) and simple > -1 and math.isfinite(simple * 100):
            returns.append(simple)
    return ModelSample(replace(sample, returns=tuple(returns)), daily, jumps, current, settings)

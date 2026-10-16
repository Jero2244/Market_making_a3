"""Monte Carlo daily paths of the futures book, for path-dependent risk.

The analytic P/L probabilities only need the end-of-horizon stock return. Paths
add what that cannot show:

- daily mark-to-market, so the worst intra-horizon loss and the chance of it
  exceeding a margin buffer can be estimated;
- legs that expire inside the horizon, which settle at that day's spot;
- an optional random financing rate (TNA), correlated with the stock.

Stock paths resample blocks of consecutive (model-adjusted) daily log returns,
keeping short-run volatility clustering. The TNA follows a random walk from the
current implied rate toward the projection rate, with the chosen volatility.
"""
from dataclasses import dataclass
from datetime import date, timedelta
import math

from market_making.contracts import CONTRACT_SHARES, DAY_BASIS
from .calculation import finite, validate_cost
from .calendar import is_session, validate_horizon
from .history import MIN_WINDOWS

MAX_PATHS = 50_000
DEFAULT_BLOCK = 5


@dataclass(frozen=True)
class SimulationSettings:
    paths: int = 5000
    rate_volatility: float = 0.0   # TNA sd at the horizon, percentage points.
    correlation: float = 0.0       # Between daily stock moves and TNA changes.
    margin_buffer: float | None = None  # ARS loss that would trigger a margin call.
    block: int = DEFAULT_BLOCK
    seed: int | None = None


@dataclass(frozen=True)
class SimulationLeg:
    expiry: str
    contracts: int
    current_price: float
    maturity: date


@dataclass(frozen=True)
class SimulationResult:
    paths: int
    horizon: int
    target_date: date
    mean: float
    percentiles: dict          # {5: ..., 25: ..., 50: ..., 75: ..., 95: ...} final P/L, ARS.
    tail: tuple                # ((level, VaR P/L, ES P/L), ...)
    profitable: float          # Percent of paths with final P/L > 0.
    at_least_target: float | None
    worst_mean: float          # Average of each path's lowest daily-close P/L (<= 0).
    worst_p05: float           # 5th percentile of that lowest P/L.
    margin_breach: float | None  # Percent of paths whose daily loss reaches the buffer.
    settled: tuple[str, ...]   # Expiries that settle inside the horizon.
    histogram: tuple           # (edges, counts) of final P/L.


def simulation_dates(reference_date, horizon):
    validate_horizon(horizon)
    dates, day = [], reference_date
    while len(dates) < horizon:
        day += timedelta(days=1)
        if is_session(day):
            dates.append(day)
    return tuple(dates)


def validate_settings(settings):
    if type(settings.paths) is not int or not 100 <= settings.paths <= MAX_PATHS:
        raise ValueError(f"Enter between 100 and {MAX_PATHS:,} simulated paths.")
    volatility = finite(settings.rate_volatility, "TNA volatility")
    if volatility < 0:
        raise ValueError("TNA volatility cannot be negative.")
    correlation = finite(settings.correlation, "correlation")
    if not -1 <= correlation <= 1:
        raise ValueError("Correlation must be between -1 and 1.")
    if settings.margin_buffer is not None and finite(settings.margin_buffer, "margin buffer") <= 0:
        raise ValueError("The margin buffer must be positive.")
    if type(settings.block) is not int or settings.block < 1:
        raise ValueError("Block length must be a positive whole number.")
    return settings


def simulate(daily_logs, legs, spot, reference_date, horizon, settings, *,
             assumed_tna=None, close_cost=0.0, target=None):
    """Simulate `settings.paths` daily paths. `daily_logs` are chronological
    daily log returns; `target` is the scenario P/L to compare against."""
    import numpy as np  # Lazy: numpy arrives with exchange-calendars.

    validate_settings(settings)
    spot = finite(spot, "spot price", positive=True)
    close_cost = validate_cost(close_cost)
    if assumed_tna is not None:
        assumed_tna = finite(assumed_tna, "assumed future TNA")
    pool = np.asarray([finite(value, "daily log return") for value in daily_logs], dtype=float)
    if len(pool) < MIN_WINDOWS:
        raise ValueError(f"At least {MIN_WINDOWS} daily returns are needed to simulate paths.")
    active = [leg for leg in legs if leg.contracts]
    if not active:
        raise ValueError("Enter at least one active position to simulate.")
    dates = simulation_dates(reference_date, horizon)
    rng = np.random.default_rng(settings.seed)
    paths, steps = settings.paths, horizon

    # Circular moving-block bootstrap of daily moves.
    block = min(settings.block, len(pool), steps)
    blocks = math.ceil(steps / block)
    starts = rng.integers(0, len(pool), size=(paths, blocks))
    index = (starts[:, :, None] + np.arange(block)) % len(pool)
    moves = pool[index.reshape(paths, blocks * block)[:, :steps]]
    stock = spot * np.exp(np.cumsum(moves, axis=1))

    # Correlated random walk for TNA shocks, sd = rate_volatility at the horizon.
    shocks = np.zeros((paths, steps))
    if settings.rate_volatility > 0:
        sd = pool.std()
        standard = (moves - pool.mean()) / sd if sd > 0 else np.zeros_like(moves)
        rho = settings.correlation
        noise = rng.standard_normal((paths, steps))
        increments = settings.rate_volatility / math.sqrt(steps) * (rho * standard + math.sqrt(1 - rho * rho) * noise)
        shocks = np.cumsum(increments, axis=1)

    progress = np.arange(1, steps + 1) / steps
    pnl = np.zeros((paths, steps))
    open_contracts = 0
    settled = []
    for leg in active:
        if type(leg.contracts) is not int:
            raise ValueError("Contracts must be whole numbers.")
        current = finite(leg.current_price, "current futures price", positive=True)
        days = (leg.maturity - reference_date).days
        if days <= 0:
            raise ValueError(f"{leg.expiry} has already expired on the reference date.")
        implied = (current / spot - 1) * DAY_BASIS / days * 100
        final_tna = implied if assumed_tna is None else assumed_tna
        tna = implied + (final_tna - implied) * progress + shocks
        remaining = np.array([(leg.maturity - day).days for day in dates], dtype=float)
        live = remaining >= 0
        future = stock * (1 + tna / 100 * np.clip(remaining, 0, None) / DAY_BASIS)
        if not live.all():
            # Futures converge to spot at expiry; after it, the leg is frozen at
            # its settlement value (spot on the last session before maturity).
            settled.append(leg.expiry)
            last = int(np.flatnonzero(live)[-1]) if live.any() else None
            value = future[:, last] if last is not None else np.full(paths, spot)
            future[:, ~live] = value[:, None]
        else:
            open_contracts += abs(leg.contracts)
        pnl += leg.contracts * CONTRACT_SHARES * (future - current)

    final = pnl[:, -1] - close_cost * open_contracts
    if not np.isfinite(final).all() or not np.isfinite(pnl).all():
        raise ValueError("The simulation produced non-finite P/L; check the inputs.")
    worst = np.minimum(pnl.min(axis=1), 0.0)
    tail = []
    for level in (.05, .01):
        cut = float(np.quantile(final, level))
        tail.append((level, cut, float(final[final <= cut].mean())))
    counts, edges = np.histogram(final, bins=40)
    buffer = settings.margin_buffer
    return SimulationResult(
        paths, horizon, dates[-1], float(final.mean()),
        {p: float(np.percentile(final, p)) for p in (5, 25, 50, 75, 95)},
        tuple(tail), 100 * float((final > 0).mean()),
        None if target is None else 100 * float((final >= target).mean()),
        float(worst.mean()), float(np.quantile(worst, .05)),
        None if buffer is None else 100 * float((worst <= -buffer).mean()),
        tuple(settled), (tuple(float(edge) for edge in edges), tuple(int(count) for count in counts)))

"""Empirical range probabilities and a Gaussian KDE of log growth factors.

Smoothing log(1 + return) keeps all projected prices positive. Probabilities
come from analytic Gaussian CDF differences, not chart height or pixel areas.
"""
from dataclasses import dataclass
import math
import random
import statistics

from market_making.contracts import CONTRACT_SHARES
from .calculation import finite
from .history import MIN_WINDOWS

RANGE_COMPARISONS = ("From X to Y", "More than X", "Less than X", ">= X", "<= X")


def quantile(values, probability):
    ordered = sorted(values)
    if not ordered:
        raise ValueError("No historical returns available.")
    index = (len(ordered) - 1) * probability
    low = int(index)
    fraction = index - low
    return ordered[low] * (1 - fraction) + ordered[min(low + 1, len(ordered) - 1)] * fraction


def range_returns(lower, upper=None, *, unit="Return (%)", spot=None, comparison="From X to Y"):
    if comparison not in RANGE_COMPARISONS:
        raise ValueError("Choose a supported range comparison.")
    if comparison != "From X to Y":
        upper = lower
    lower, upper = finite(lower, "lower bound"), finite(upper, "upper bound")
    if lower > upper:
        raise ValueError("The lower bound must be at or below the upper bound.")
    if unit == "Final price (ARS)":
        spot = finite(spot, "spot price", positive=True)
        if lower <= 0:
            raise ValueError("Both final-price bounds must be positive.")
        low, high = lower / spot - 1, upper / spot - 1
    elif unit == "Return (%)":
        low, high = lower / 100, upper / 100
    else:
        raise ValueError("Choose Return (%) or Final price (ARS).")
    if low <= -1 or not math.isfinite(low) or not math.isfinite(high):
        raise ValueError("Range bounds must imply finite returns greater than -100%.")
    if comparison in ("More than X", ">= X"):
        return low, math.inf
    if comparison in ("Less than X", "<= X"):
        return -math.inf, high
    return low, high


def _above_lower(value, lower, inclusive):
    # Treat rounding in price-to-return conversion as equality at the boundary.
    if math.isclose(value, lower, rel_tol=1e-12, abs_tol=1e-12):
        return inclusive
    return value > lower


def _contains(value, lower, upper, include_lower, include_upper):
    return (_above_lower(value, lower, include_lower)
            and _above_lower(-value, -upper, include_upper))


def default_block(observations, horizon):
    """Block length for resampling overlapping N-day windows: at least the
    overlap itself, and longer for big samples to keep volatility clusters."""
    return max(1, horizon, math.ceil(observations ** (1 / 3)))


def block_bootstrap_interval(values, block, *, draws=500, level=.95, seed=20261016):
    """Circular moving-block bootstrap interval for the mean of `values`
    (percent if the values are 0-100). Chronological order matters.

    Each draw concatenates random runs of `block` consecutive observations, so
    overlapping windows and volatility clusters are resampled together. The
    fixed seed keeps the displayed interval stable between refreshes.
    """
    n = len(values)
    if n < MIN_WINDOWS:
        return None
    block = max(1, min(int(block), n))
    prefix = [0.0]
    for value in tuple(values) + tuple(values[:block - 1]):
        prefix.append(prefix[-1] + value)
    sums = [prefix[start + block] - prefix[start] for start in range(n)]
    count = math.ceil(n / block)
    rng = random.Random(seed)
    means = sorted(math.fsum(sums[rng.randrange(n)] for _ in range(count)) / (count * block)
                   for _ in range(draws))
    tail = (1 - level) / 2
    return quantile(means, tail), quantile(means, 1 - tail)


def effective_observations(observations, horizon):
    """Roughly how many non-overlapping N-day periods the sample contains."""
    return observations / max(1, horizon)


@dataclass(frozen=True)
class RangeProbability:
    matches: int
    observations: int
    empirical: float | None  # Percentage.
    smoothed: float | None
    below: float | None     # Empirical percentages; below + inside + above = 100.
    above: float | None
    smoothed_interval: tuple[float, float] | None = None  # 95% block-bootstrap, percent.
    empirical_interval: tuple[float, float] | None = None


@dataclass(frozen=True)
class ReturnDensity:
    log_growth: tuple[float, ...]
    bandwidth: float | None  # None = a point mass; no invented spread.

    @classmethod
    def fit(cls, returns):
        if len(returns) < MIN_WINDOWS:
            return None
        logs = tuple(math.log1p(finite(value, "historical return")) for value in returns)
        std = statistics.stdev(logs)
        if std == 0:
            return cls(logs, None)
        iqr = quantile(logs, .75) - quantile(logs, .25)
        # Silverman's robust nrd0 rule; fall back to std when the IQR is zero.
        scale = min(std, iqr / 1.34) if iqr > 0 else std
        bandwidth = .9 * scale * len(logs) ** (-.2)
        if bandwidth <= 0 or not math.isfinite(bandwidth):
            return cls(logs, None)
        return cls(logs, bandwidth)

    def cdf(self, value):
        if value <= -1:
            return 0.0
        point = math.log1p(value)
        if self.bandwidth is None:
            return float(point >= self.log_growth[0])
        denominator = self.bandwidth * math.sqrt(2)
        return sum(.5 * math.erfc((mean - point) / denominator) for mean in self.log_growth) / len(self.log_growth)

    def interval_probability(self, lower, upper, *, include_lower=True, include_upper=True):
        if lower > upper:
            raise ValueError("Reversed return bounds.")
        if self.bandwidth is None:
            return float(_contains(math.expm1(self.log_growth[0]), lower, upper, include_lower, include_upper))
        return max(0.0, min(1.0, self.cdf(upper) - self.cdf(lower)))

    def components(self, lower, upper, *, include_lower=True, include_upper=True):
        """Each observation's kernel mass inside the interval, chronological.
        Their mean is `interval_probability`; resampling them gives its spread."""
        if lower > upper:
            raise ValueError("Reversed return bounds.")
        if self.bandwidth is None:
            inside = float(_contains(math.expm1(self.log_growth[0]), lower, upper, include_lower, include_upper))
            return (inside,) * len(self.log_growth)
        denominator = self.bandwidth * math.sqrt(2)

        def below(value, mean):
            if value <= -1:
                return 0.0
            if value == math.inf:
                return 1.0
            return .5 * math.erfc((mean - math.log1p(value)) / denominator)
        return tuple(max(0.0, below(upper, mean) - below(lower, mean)) for mean in self.log_growth)

    def log_quantile(self, probability):
        """Quantile of log growth under the smoothed mixture, by bisection."""
        if not 0 < probability < 1:
            raise ValueError("Quantile probability must be between 0 and 1.")
        if self.bandwidth is None:
            return self.log_growth[0]
        h = self.bandwidth
        low, high = min(self.log_growth) - 12 * h, max(self.log_growth) + 12 * h
        denominator = h * math.sqrt(2)
        for _ in range(80):
            middle = (low + high) / 2
            cdf = sum(.5 * math.erfc((mean - middle) / denominator) for mean in self.log_growth) / len(self.log_growth)
            if cdf < probability:
                low = middle
            else:
                high = middle
        return (low + high) / 2

    def partial_growth(self, log_point, *, upper_tail):
        """E[(1 + r) ; log(1 + r) below (or above) `log_point`] under the mixture."""
        if self.bandwidth is None:
            inside = self.log_growth[0] >= log_point if upper_tail else self.log_growth[0] <= log_point
            return math.exp(self.log_growth[0]) if inside else 0.0
        h = self.bandwidth
        total = 0.0
        for mean in self.log_growth:
            # Lognormal partial expectation of one Gaussian kernel.
            z = (log_point - mean - h * h) / (h * math.sqrt(2))
            weight = .5 * math.erfc(z) if upper_tail else .5 * math.erfc(-z)
            total += math.exp(mean + h * h / 2) * weight
        return total / len(self.log_growth)

    def pdf(self, percent):
        """Probability density per ONE percentage point of simple return."""
        if self.bandwidth is None or percent <= -100:
            return 0.0
        point = math.log1p(percent / 100)
        total = 0.0
        for mean in self.log_growth:
            z = (point - mean) / self.bandwidth
            if abs(z) < 40:
                total += math.exp(-.5 * z * z)
        # Change-of-variable derivative d(log(1 + x/100))/dx = 1/(100+x).
        return total / (len(self.log_growth) * self.bandwidth * math.sqrt(2 * math.pi) * (100 + percent))

    def plot_bounds(self):
        padding = 4 * self.bandwidth if self.bandwidth is not None else .005
        low = max(math.nextafter(-100.0, 0.0), 100 * math.expm1(max(-700, min(self.log_growth) - padding)))
        high = 100 * math.expm1(min(700, max(self.log_growth) + padding))
        return low, high


def range_probability(sample, lower, upper, density=None, *, include_lower=True, include_upper=True):
    if lower > upper:
        raise ValueError("Reversed return bounds.")
    matches = sum(_contains(value, lower, upper, include_lower, include_upper) for value in sample.returns)
    below = sum(not _above_lower(value, lower, include_lower) for value in sample.returns)
    n = len(sample.returns)
    if n < MIN_WINDOWS:
        return RangeProbability(matches, n, None, None, None, None)
    density = density or ReturnDensity.fit(sample.returns)
    block = default_block(n, sample.horizon)
    empirical_interval = block_bootstrap_interval(
        [100.0 * _contains(value, lower, upper, include_lower, include_upper) for value in sample.returns], block)
    smoothed_interval = block_bootstrap_interval(
        [100 * value for value in density.components(lower, upper, include_lower=include_lower,
                                                       include_upper=include_upper)], block)
    return RangeProbability(matches, n, 100 * matches / n,
                            100 * density.interval_probability(lower, upper, include_lower=include_lower,
                                                               include_upper=include_upper),
                            100 * below / n, 100 * (n - matches - below) / n,
                            smoothed_interval, empirical_interval)


@dataclass(frozen=True)
class PnlProbabilities:
    at_least_target: float | None  # Percentages, using the same smoothed return model.
    profitable: float | None
    at_least_target_interval: tuple[float, float] | None = None  # 95% block-bootstrap, percent.
    profitable_interval: tuple[float, float] | None = None


def _affine(scenario, spot):
    """(scenario return, exposure, scenario P/L): P/L(r) = target + exposure x (r - scenario return)."""
    spot = finite(spot, "spot price", positive=True)
    target = finite(scenario.total_from_today, "combined scenario P/L")
    scenario_return = scenario.projected_spot / spot - 1
    exposure = finite(spot * math.fsum(
        leg.contracts * CONTRACT_SHARES * (leg.projected_future / scenario.projected_spot)
        for leg in scenario.legs if leg.contracts
    ), "combined P/L exposure")
    return scenario_return, exposure, target


def pnl_probabilities(sample, scenario, spot, density=None, *, intervals=False):
    """Combined P/L from today under the scenario's fixed TNA assumptions.

    Every leg shares the same stock outcome. At fixed rates, combined P/L is
    affine in that return; invert it to integrate the existing return density.
    Negative exposure reverses the tail, and zero exposure is deterministic.
    """
    scenario_return, exposure, target = _affine(scenario, spot)
    if len(sample.returns) < MIN_WINDOWS:
        return PnlProbabilities(None, None)
    density = density or ReturnDensity.fit(sample.returns)

    def bounds(threshold, inclusive):
        boundary = scenario_return + (threshold - target) / exposure
        if exposure > 0:
            return dict(lower=boundary, upper=math.inf, include_lower=inclusive)
        return dict(lower=-math.inf, upper=boundary, include_upper=inclusive)

    def chance(threshold, inclusive):
        if exposure == 0:
            return 100.0 * _above_lower(target, threshold, inclusive)
        area = bounds(threshold, inclusive)
        return 100 * density.interval_probability(area.pop("lower"), area.pop("upper"), **area)

    def interval(threshold, inclusive):
        if not intervals:
            return None
        if exposure == 0:
            value = chance(threshold, inclusive)
            return value, value
        area = bounds(threshold, inclusive)
        components = density.components(area.pop("lower"), area.pop("upper"), **area)
        return block_bootstrap_interval([100 * value for value in components],
                                        default_block(len(components), sample.horizon))

    return PnlProbabilities(chance(target, True), chance(0, False),
                            interval(target, True), interval(0, False))


@dataclass(frozen=True)
class TailRisk:
    level: float           # Tail probability, e.g. .05.
    value_at_risk: float   # P/L at the tail quantile (negative = loss), ARS.
    expected_shortfall: float  # Average P/L inside that tail, ARS.


def pnl_tail_risk(sample, scenario, spot, density=None, levels=(.05, .01)):
    """Analytic VaR and expected shortfall of the fixed-rate combined P/L.

    P/L = a + exposure x (1 + r); with a log-normal kernel mixture for 1 + r,
    the tail quantile comes from bisection and the tail mean is closed form.
    """
    scenario_return, exposure, target = _affine(scenario, spot)
    if len(sample.returns) < MIN_WINDOWS:
        return ()
    density = density or ReturnDensity.fit(sample.returns)
    intercept = target - exposure * (1 + scenario_return)
    result = []
    for level in levels:
        if exposure == 0:
            result.append(TailRisk(level, target, target))
            continue
        # A long book loses when the stock falls, a short book when it rises.
        upper_tail = exposure < 0
        point = density.log_quantile(1 - level if upper_tail else level)
        growth = math.exp(point)
        tail_mean = density.partial_growth(point, upper_tail=upper_tail) / level
        if density.bandwidth is None:
            tail_mean = growth
        result.append(TailRisk(level, finite(intercept + exposure * growth, "value at risk"),
                               finite(intercept + exposure * tail_mean, "expected shortfall")))
    return tuple(result)

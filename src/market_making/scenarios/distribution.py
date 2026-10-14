"""Empirical range probabilities and a Gaussian KDE of log growth factors.

Smoothing log(1 + return) keeps all projected prices positive. Probabilities
come from analytic Gaussian CDF differences, not chart height or pixel areas.
"""
from dataclasses import dataclass
import math
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


@dataclass(frozen=True)
class RangeProbability:
    matches: int
    observations: int
    empirical: float | None  # Percentage.
    smoothed: float | None
    below: float | None     # Empirical percentages; below + inside + above = 100.
    above: float | None


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
    return RangeProbability(matches, n, 100 * matches / n,
                            100 * density.interval_probability(lower, upper, include_lower=include_lower,
                                                               include_upper=include_upper),
                            100 * below / n, 100 * (n - matches - below) / n)


@dataclass(frozen=True)
class PnlProbabilities:
    at_least_target: float | None  # Percentages, using the same smoothed return model.
    profitable: float | None


def pnl_probabilities(sample, scenario, spot, density=None):
    """Combined gross P/L from today under the scenario's fixed TNA assumptions.

    Every leg shares the same stock outcome. At fixed rates, combined P/L is
    affine in that return; invert it to integrate the existing return density.
    Negative exposure reverses the tail, and zero exposure is deterministic.
    """
    spot = finite(spot, "spot price", positive=True)
    target = finite(scenario.total_from_today, "combined scenario P/L")
    if len(sample.returns) < MIN_WINDOWS:
        return PnlProbabilities(None, None)
    density = density or ReturnDensity.fit(sample.returns)
    scenario_return = scenario.projected_spot / spot - 1
    exposure = finite(spot * math.fsum(
        leg.contracts * CONTRACT_SHARES * (leg.projected_future / scenario.projected_spot)
        for leg in scenario.legs if leg.contracts
    ), "combined P/L exposure")

    def chance(threshold, inclusive):
        if exposure == 0:
            return 100.0 * _above_lower(target, threshold, inclusive)
        boundary = scenario_return + (threshold - target) / exposure
        if exposure > 0:
            return 100 * density.interval_probability(boundary, math.inf, include_lower=inclusive)
        return 100 * density.interval_probability(-math.inf, boundary, include_upper=inclusive)

    return PnlProbabilities(chance(target, True), chance(0, False))

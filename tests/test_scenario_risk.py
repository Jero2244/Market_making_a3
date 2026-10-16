"""Sampling intervals, analytic tail risk, closing costs and TNA shifts."""
from datetime import date
import math
import random
import unittest

from market_making.scenarios.calculation import Position, calculate
from market_making.scenarios.distribution import (
    ReturnDensity, block_bootstrap_interval, pnl_probabilities, pnl_tail_risk, range_probability,
)
from market_making.scenarios.history import HistorySample


def sample(returns, horizon=2):
    return HistorySample((), tuple(returns), 0, 0, 0, 0, date(2025, 1, 1), date(2026, 10, 13), horizon)


def gaussian_returns(count=300, seed=3):
    rng = random.Random(seed)
    return [math.expm1(rng.gauss(.002, .05)) for _ in range(count)]


class IntervalTests(unittest.TestCase):
    def test_interval_brackets_mean_and_is_stable(self):
        values = [100.0 * (index % 3 == 0) for index in range(90)]
        low, high = block_bootstrap_interval(values, 5)
        self.assertLess(low, 100 / 3)
        self.assertGreater(high, 100 / 3)
        self.assertEqual((low, high), block_bootstrap_interval(values, 5))

    def test_constant_values_have_no_spread_and_small_samples_none(self):
        self.assertEqual(block_bootstrap_interval([42.0] * 40, 4), (42.0, 42.0))
        self.assertIsNone(block_bootstrap_interval([1.0] * 29, 4))

    def test_longer_blocks_widen_clustered_samples(self):
        values = [100.0 * ((index // 20) % 2) for index in range(400)]
        short = block_bootstrap_interval(values, 1)
        long = block_bootstrap_interval(values, 10)
        self.assertGreater(long[1] - long[0], short[1] - short[0])

    def test_range_probability_reports_intervals_around_estimates(self):
        returns = gaussian_returns()
        density = ReturnDensity.fit(returns)
        result = range_probability(sample(returns), -.05, .05, density)
        components = density.components(-.05, .05)
        self.assertAlmostEqual(sum(components) / len(components), result.smoothed / 100, places=12)
        for estimate, interval in ((result.smoothed, result.smoothed_interval),
                                   (result.empirical, result.empirical_interval)):
            self.assertLessEqual(interval[0], estimate)
            self.assertGreaterEqual(interval[1], estimate)

    def test_pnl_intervals_are_optional(self):
        returns = gaussian_returns()
        scenario = calculate(100, 5, [Position("2026-10", 1, 90, 100, date(2026, 10, 30))], date(2026, 10, 14),
                             assumed_tna=0)
        plain = pnl_probabilities(sample(returns), scenario, 100)
        detailed = pnl_probabilities(sample(returns), scenario, 100, intervals=True)
        self.assertIsNone(plain.profitable_interval)
        self.assertEqual(plain.profitable, detailed.profitable)
        self.assertLessEqual(detailed.profitable_interval[0], detailed.profitable)
        self.assertGreaterEqual(detailed.profitable_interval[1], detailed.profitable)


class TailRiskTests(unittest.TestCase):
    def brute_force(self, density, pnl, level, draws=400_000):
        rng = random.Random(11)
        outcomes = sorted(pnl(math.expm1(rng.choice(density.log_growth) + density.bandwidth * rng.gauss(0, 1)))
                          for _ in range(draws))
        cut = int(level * draws)
        return outcomes[cut], sum(outcomes[:cut]) / cut

    def test_long_and_short_tails_match_brute_force(self):
        returns = gaussian_returns()
        density = ReturnDensity.fit(returns)
        reference = date(2026, 10, 14)
        for contracts in (2, -2):
            with self.subTest(contracts=contracts):
                positions = [Position("2026-12", contracts, 95, 105, date(2026, 12, 30))]
                scenario = calculate(100, 0, positions, reference, assumed_tna=30)
                risks = pnl_tail_risk(sample(returns), scenario, 100, density)

                # P/L is linear in the stock return at a fixed rate.
                at_zero = scenario.total_from_today
                slope = calculate(100, 10, positions, reference, assumed_tna=30).total_from_today - at_zero

                def pnl(r):
                    return at_zero + slope * r * 10
                for risk in risks:
                    value, shortfall = self.brute_force(density, pnl, risk.level)
                    scale = abs(shortfall)
                    self.assertAlmostEqual(risk.value_at_risk, value, delta=.01 * scale)
                    self.assertAlmostEqual(risk.expected_shortfall, shortfall, delta=.01 * scale)
                    self.assertLessEqual(risk.expected_shortfall, risk.value_at_risk)

    def test_zero_exposure_is_deterministic(self):
        positions = [Position("2026-10", 1, 90, 100, date(2026, 10, 30)),
                     Position("2026-12", -1, 90, 100, date(2026, 10, 30).replace(month=12))]
        scenario = calculate(100, 0, positions, date(2026, 10, 14), assumed_tna=0)
        for risk in pnl_tail_risk(sample(gaussian_returns()), scenario, 100):
            self.assertEqual(risk.value_at_risk, scenario.total_from_today)


class CostAndShiftTests(unittest.TestCase):
    position = Position("2026-10", -3, 90, 100, date(2026, 10, 30))

    def test_closing_cost_is_charged_per_contract(self):
        gross = calculate(100, 5, [self.position], date(2026, 10, 14), assumed_tna=0)
        net = calculate(100, 5, [self.position], date(2026, 10, 14), assumed_tna=0, close_cost=12.5)
        self.assertAlmostEqual(gross.total_from_today - net.total_from_today, 37.5)
        self.assertAlmostEqual(gross.total_since_entry - net.total_since_entry, 37.5)
        with self.assertRaises(ValueError):
            calculate(100, 5, [self.position], date(2026, 10, 14), close_cost=-1)

    def test_tna_shift_moves_assumed_and_implied_rates(self):
        for assumed in (None, 30):
            with self.subTest(assumed=assumed):
                base = calculate(100, 0, [self.position], date(2026, 10, 14), assumed_tna=assumed)
                shifted = calculate(100, 0, [self.position], date(2026, 10, 14), assumed_tna=assumed, tna_shift=5)
                self.assertAlmostEqual(shifted.legs[0].projection_tna, base.legs[0].projection_tna + 5)
                # Short futures lose when the projected rate rises.
                self.assertLess(shifted.total_from_today, base.total_from_today)


if __name__ == "__main__":
    unittest.main()

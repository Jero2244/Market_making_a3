"""Horizon-specific distributions, analytic probabilities and density units."""
from datetime import date
import math
import unittest

from market_making.scenarios.calendar import sessions_between
from market_making.scenarios.calculation import Position, calculate
from market_making.scenarios.distribution import (
    RANGE_COMPARISONS, ReturnDensity, quantile, range_probability, range_returns,
)
from market_making.scenarios.history import HistorySample, normalize_history, with_horizon


class HorizonTests(unittest.TestCase):
    def history(self):
        days = sessions_between(date(2026, 1, 1), date(2026, 3, 31))
        rows = [{"date": day.isoformat(), "price": 100 * 1.01 ** index} for index, day in enumerate(days)]
        return normalize_history(rows, days[0], days[-1], completed_before=date(2026, 4, 1)), days, rows

    def test_actual_n_day_compounding_and_observation_counts(self):
        base, days, _ = self.history()
        for horizon in (1, 2, 5, 20):
            with self.subTest(horizon=horizon):
                sample = with_horizon(base, horizon)
                self.assertEqual(sample.horizon, horizon)
                self.assertEqual(len(sample.returns), len(days) - horizon)
                self.assertTrue(all(abs(value - (1.01 ** horizon - 1)) < 1e-10 for value in sample.returns))
                self.assertEqual(sample.closes, base.closes)

    def test_gaps_exclude_every_long_window_crossing_them(self):
        _, days, rows = self.history()
        rows.pop(10)
        sample = normalize_history(rows, days[0], days[-1], completed_before=date(2026, 4, 1), horizon=5)
        self.assertEqual(sample.excluded_windows, 6)
        self.assertEqual(len(sample.returns), len(days) - 5 - 6)

    def test_horizon_longer_than_sample_and_invalid_values(self):
        sample, _, _ = self.history()
        self.assertFalse(with_horizon(sample, 252).returns)
        for count in (0, -1, 1.5, True, 253, "5"):
            with self.subTest(count=count), self.assertRaises(ValueError):
                with_horizon(sample, count)

    def test_pnl_uses_same_variable_horizon_and_expiry_gate(self):
        row = Position("2026-10", 1, 100, 110, date(2026, 10, 30))
        result = calculate(100, 5, [row], date(2026, 10, 14), horizon=5)
        self.assertEqual(result.target_date, date(2026, 10, 21))
        self.assertAlmostEqual(result.legs[0].projected_future, 105 * (1 + .1 * 9 / 16))
        expired = calculate(100, 5, [row], date(2026, 10, 14), horizon=20)
        self.assertIsNone(expired.total_from_today)


class DistributionTests(unittest.TestCase):
    def sample(self, returns):
        return HistorySample((), tuple(returns), 0, 0, 0, 0, date(2026, 1, 1), date(2026, 10, 13))

    def test_returns_and_price_range_conversion_are_equivalent(self):
        expected = range_returns(-5, 10)
        actual = range_returns(9500, 11000, unit="Final price (ARS)", spot=10000)
        self.assertAlmostEqual(expected[0], actual[0])
        self.assertAlmostEqual(expected[1], actual[1])

    def test_range_validation(self):
        for low, high in ((5, -5), (-100, 5), (float("nan"), 5), (0, float("inf"))):
            with self.subTest(bounds=(low, high)), self.assertRaises(ValueError):
                range_returns(low, high)
        for spot in (0, -1, float("nan"), float("inf")):
            with self.subTest(spot=spot), self.assertRaises(ValueError):
                range_returns(95, 105, unit="Final price (ARS)", spot=spot)
        with self.assertRaises(ValueError):
            range_returns(0, 105, unit="Final price (ARS)", spot=100)

    def test_empirical_range_boundaries_and_partitions(self):
        sample = self.sample([-.10, -.05, 0, .05, .10] * 10)
        probability = range_probability(sample, -.05, .05)
        self.assertEqual(probability.matches, 30)
        self.assertEqual(probability.empirical, 60)
        self.assertEqual(probability.below, 20)
        self.assertEqual(probability.above, 20)
        self.assertEqual(probability.below + probability.empirical + probability.above, 100)
        self.assertGreater(probability.smoothed, 0)
        self.assertLess(probability.smoothed, 100)

    def test_threshold_comparisons_include_or_exclude_equal_returns(self):
        sample = self.sample([-.10, -.05, 0, .05, .10] * 10)
        expected = {"More than X": (10, 80, 0), ">= X": (20, 60, 0),
                    "Less than X": (30, 0, 40), "<= X": (40, 0, 20)}
        for comparison, (matches, below, above) in expected.items():
            with self.subTest(comparison=comparison):
                low, high = range_returns(5, comparison=comparison)
                result = range_probability(sample, low, high,
                                           include_lower=comparison != "More than X",
                                           include_upper=comparison != "Less than X")
                self.assertEqual(result.matches, matches)
                self.assertEqual(result.below, below)
                self.assertEqual(result.above, above)
                self.assertEqual(result.below + result.empirical + result.above, 100)

    def test_threshold_price_conversion_preserves_boundary_counts(self):
        sample = self.sample([-.05, 0, .05] * 10)
        for comparison in RANGE_COMPARISONS[1:]:
            with self.subTest(comparison=comparison):
                percent = range_returns(5, comparison=comparison)
                price = range_returns(10500, unit="Final price (ARS)", spot=10000, comparison=comparison)
                flags = {"include_lower": comparison != "More than X",
                         "include_upper": comparison != "Less than X"}
                expected = range_probability(sample, *percent, **flags)
                actual = range_probability(sample, *price, **flags)
                self.assertEqual(expected.matches, actual.matches)
                self.assertAlmostEqual(expected.smoothed, actual.smoothed)

    def test_smoothed_tails_are_complements_and_extend_to_infinity(self):
        sample = self.sample([-.10, -.05, 0, .05, .10] * 10)
        lower = range_probability(sample, *range_returns(5, comparison="<= X"))
        upper = range_probability(sample, *range_returns(5, comparison="More than X"), include_lower=False)
        self.assertAlmostEqual(lower.smoothed + upper.smoothed, 100)
        self.assertEqual(lower.matches + upper.matches, len(sample.returns))
        self.assertEqual(range_probability(sample, *range_returns(1000, comparison="Less than X")).smoothed, 100)
        self.assertEqual(range_probability(sample, *range_returns(1000, comparison="More than X")).smoothed, 0)

    def test_constant_sample_threshold_respects_strict_comparisons(self):
        sample = self.sample([.02] * 30)
        for comparison, expected in (("More than X", 0), ("Less than X", 0), (">= X", 100), ("<= X", 100)):
            with self.subTest(comparison=comparison):
                result = range_probability(sample, *range_returns(2, comparison=comparison),
                                           include_lower=comparison != "More than X",
                                           include_upper=comparison != "Less than X")
                self.assertEqual(result.empirical, expected)
                self.assertEqual(result.smoothed, expected)

    def test_threshold_validation_ignores_unused_upper_bound(self):
        self.assertEqual(range_returns(5, "unused", comparison="More than X"), (.05, math.inf))
        for comparison in RANGE_COMPARISONS[1:]:
            for value in (float("nan"), float("inf"), -100):
                with self.subTest(comparison=comparison, value=value), self.assertRaises(ValueError):
                    range_returns(value, comparison=comparison)
        with self.assertRaises(ValueError):
            range_returns(5, comparison="unknown")

    def test_single_point_interval_has_zero_continuous_probability(self):
        sample = self.sample([-.05, 0, .05] * 10)
        probability = range_probability(sample, 0, 0)
        self.assertEqual(probability.empirical, 100 / 3)
        self.assertEqual(probability.smoothed, 0)

    def test_small_sample_suppresses_density_and_probabilities(self):
        sample = self.sample([0] * 29)
        self.assertIsNone(ReturnDensity.fit(sample.returns))
        result = range_probability(sample, -.05, .05)
        self.assertIsNone(result.empirical)
        self.assertIsNone(result.smoothed)

    def test_constant_returns_are_point_mass_without_artificial_variance(self):
        sample = self.sample([.02] * 30)
        density = ReturnDensity.fit(sample.returns)
        self.assertIsNone(density.bandwidth)
        self.assertEqual(range_probability(sample, .01, .03, density).smoothed, 100)
        self.assertEqual(range_probability(sample, -.05, 0, density).smoothed, 0)
        self.assertEqual(range_probability(sample, .02, .02, density).smoothed, 100)

    def test_probability_density_integrates_to_one_in_percent_units(self):
        density = ReturnDensity.fit([-.1, -.05, 0, .03, .08] * 10)
        low, high = density.plot_bounds()
        steps = 4000
        width = (high - low) / steps
        values = [density.pdf(low + index * width) for index in range(steps + 1)]
        integral = width * (sum(values) - .5 * (values[0] + values[-1]))
        self.assertAlmostEqual(integral, 1, places=4)
        self.assertEqual(density.pdf(-100), 0)
        self.assertEqual(density.pdf(-101), 0)

    def test_cdf_is_monotone_and_interval_matches_independent_integration(self):
        density = ReturnDensity.fit([-.1, -.05, 0, .03, .08] * 10)
        points = [-.5, -.1, 0, .1, 1]
        cdfs = [density.cdf(value) for value in points]
        self.assertEqual(cdfs, sorted(cdfs))
        self.assertEqual(density.cdf(-1), 0)
        low, high = -5.0, 5.0
        steps = 1000
        width = (high - low) / steps
        values = [density.pdf(low + index * width) for index in range(steps + 1)]
        integral = width * (sum(values) - .5 * (values[0] + values[-1]))
        self.assertAlmostEqual(density.interval_probability(-.05, .05), integral, places=5)

    def test_pdf_price_axis_conversion_preserves_probability_area(self):
        density = ReturnDensity.fit([-.1, -.05, 0, .03, .08] * 10)
        spot = 10000
        low, high = 9500, 10500
        steps = 1000
        width = (high - low) / steps
        values = [density.pdf((low + index * width) / spot * 100 - 100) * 100 / spot
                  for index in range(steps + 1)]
        integral = width * (sum(values) - .5 * (values[0] + values[-1]))
        self.assertAlmostEqual(integral, density.interval_probability(-.05, .05), places=5)

    def test_quantile_and_outside_range(self):
        self.assertEqual(quantile([-.1, 0, .1], .5), 0)
        probability = range_probability(self.sample([-.1, 0, .1] * 10), 10, 11)
        self.assertEqual(probability.empirical, 0)
        self.assertLess(probability.smoothed, .0001)


if __name__ == "__main__":
    unittest.main()

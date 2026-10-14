"""P/L outcome probabilities under shared stock returns and fixed rates."""
from datetime import date
import unittest

from market_making.scenarios.calculation import Position, calculate
from market_making.scenarios.distribution import ReturnDensity, pnl_probabilities
from market_making.scenarios.history import HistorySample


class PnlProbabilityTests(unittest.TestCase):
    def sample(self, returns=None):
        return HistorySample((), tuple([-.1, -.05, 0, .05, .1] * 10 if returns is None else returns),
                             0, 0, 0, 0, date(2026, 1, 1), date(2026, 10, 13))

    def scenario(self, positions, move=5, rate=0):
        return calculate(100, move, positions, date(2026, 10, 14), assumed_tna=rate)

    def position(self, quantity=1, current=100):
        return Position("2026-10", quantity, 90, current, date(2026, 10, 30))

    def test_long_target_and_profit_use_upper_tails(self):
        sample = self.sample()
        density = ReturnDensity.fit(sample.returns)
        scenario = self.scenario([self.position()])
        result = pnl_probabilities(sample, scenario, 100, density)
        self.assertAlmostEqual(result.at_least_target, 100 * (1 - density.cdf(.05)))
        self.assertAlmostEqual(result.profitable, 100 * (1 - density.cdf(0)))

    def test_short_reverses_tail_even_for_a_negative_scenario_target(self):
        sample = self.sample()
        density = ReturnDensity.fit(sample.returns)
        scenario = self.scenario([self.position(-1)])
        self.assertLess(scenario.total_from_today, 0)
        result = pnl_probabilities(sample, scenario, 100, density)
        self.assertAlmostEqual(result.at_least_target, 100 * density.cdf(.05))
        self.assertAlmostEqual(result.profitable, 100 * density.cdf(0))

    def test_combined_positions_apply_one_shared_stock_outcome(self):
        positions = [self.position(), Position("2026-12", -3, 90, 115, date(2026, 12, 30))]
        scenario = self.scenario(positions, rate=35)
        # Hand-computed combined P/L(r) = exposure * (1+r) - current value.
        exposure = 100 * 100 * (1 + .35 * 14 / 365) - 3 * 100 * 100 * (1 + .35 * 75 / 365)
        current_value = 100 * 100 - 3 * 100 * 115
        sample = self.sample()
        density = ReturnDensity.fit(sample.returns)
        result = pnl_probabilities(sample, scenario, 100, density)
        self.assertAlmostEqual(result.at_least_target, 100 * density.cdf(.05))
        self.assertAlmostEqual(result.profitable, 100 * density.cdf(current_value / exposure - 1))

    def test_point_mass_probabilities_match_direct_pnl_evaluation(self):
        positions = [self.position(), Position("2026-12", -2, 90, 105, date(2026, 12, 30))]
        for rate in (None, 0, 35):
            scenario = self.scenario(positions, rate=rate)
            for move in (-20, 0, 5, 20):
                with self.subTest(rate=rate, move=move):
                    sample = self.sample([move / 100] * 30)
                    realized = self.scenario(positions, move=move, rate=rate).total_from_today
                    result = pnl_probabilities(sample, scenario, 100)
                    self.assertEqual(result.at_least_target, 100 * (realized >= scenario.total_from_today))
                    self.assertEqual(result.profitable, 100 * (realized > 0))

    def test_break_even_is_not_profitable_but_meets_zero_target(self):
        scenario = self.scenario([self.position()], move=0)
        result = pnl_probabilities(self.sample([0] * 30), scenario, 100)
        self.assertEqual(result.at_least_target, 100)
        self.assertEqual(result.profitable, 0)

    def test_exactly_offsetting_positions_have_deterministic_pnl(self):
        for current, profitable in ((100, 0), (105, 100), (95, 0)):
            with self.subTest(short_current=current):
                positions = [self.position(), Position("2026-12", -1, 90, current, date(2026, 12, 30))]
                scenario = self.scenario(positions)
                result = pnl_probabilities(self.sample(), scenario, 100)
                self.assertEqual(result.at_least_target, 100)
                self.assertEqual(result.profitable, profitable)

    def test_small_sample_and_unavailable_expiry_suppress_probabilities(self):
        scenario = self.scenario([self.position()])
        result = pnl_probabilities(self.sample([0] * 29), scenario, 100)
        self.assertIsNone(result.at_least_target)
        self.assertIsNone(result.profitable)
        expired = self.scenario([Position("2026-10", 1, 90, 100, date(2026, 10, 16))])
        with self.assertRaises(ValueError):
            pnl_probabilities(self.sample(), expired, 100)


if __name__ == "__main__":
    unittest.main()

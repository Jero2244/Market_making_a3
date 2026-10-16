"""Monte Carlo daily paths of the futures book."""
from datetime import date
import math
import unittest

from market_making.scenarios.calculation import Position, calculate
from market_making.scenarios.simulation import SimulationLeg, SimulationSettings, simulate, simulation_dates

REFERENCE = date(2026, 10, 16)
OCT = SimulationLeg("2026-10", 3, 1010, date(2026, 10, 30))
DEC = SimulationLeg("2026-12", -2, 1050, date(2026, 12, 30))


def as_position(leg):
    return Position(leg.expiry, leg.contracts, 990, leg.current_price, leg.maturity)


class SimulationTests(unittest.TestCase):
    def test_flat_paths_match_the_deterministic_scenario(self):
        for assumed in (None, 30):
            with self.subTest(assumed=assumed):
                result = simulate([0.0] * 40, [OCT, DEC], 1000, REFERENCE, 5, SimulationSettings(paths=100),
                                  assumed_tna=assumed, close_cost=50)
                expected = calculate(1000, 0, [as_position(OCT), as_position(DEC)], REFERENCE, horizon=5,
                                     assumed_tna=assumed, close_cost=50).total_from_today
                self.assertAlmostEqual(result.mean, expected, places=6)
                self.assertAlmostEqual(result.percentiles[5], expected, places=6)

    def test_leg_settles_at_spot_on_expiry(self):
        dates = simulation_dates(REFERENCE, 12)
        self.assertGreater(dates[-1], OCT.maturity)
        result = simulate([0.0] * 40, [OCT], 1000, REFERENCE, 12, SimulationSettings(paths=100))
        self.assertEqual(result.settled, ("2026-10",))
        self.assertAlmostEqual(result.mean, 3 * 100 * (1000 - 1010))

    def test_constant_decline_breaches_margin_on_every_path(self):
        daily = [math.log(.99)] * 40
        long = SimulationLeg("2026-12", 1, 1000, date(2026, 12, 30))
        result = simulate(daily, [long], 1000, REFERENCE, 10, SimulationSettings(paths=200, margin_buffer=5000),
                          assumed_tna=0)
        self.assertEqual(result.margin_breach, 100.0)
        self.assertEqual(result.profitable, 0.0)
        self.assertAlmostEqual(result.worst_mean, 100 * (1000 * .99 ** 10 - 1000), places=6)
        loose = simulate(daily, [long], 1000, REFERENCE, 10, SimulationSettings(paths=200, margin_buffer=1e9),
                         assumed_tna=0)
        self.assertEqual(loose.margin_breach, 0.0)

    def test_rate_volatility_spreads_a_flat_stock(self):
        flat = simulate([0.0] * 40, [DEC], 1000, REFERENCE, 20, SimulationSettings(paths=4000, seed=1), assumed_tna=30)
        noisy = simulate([0.0] * 40, [DEC], 1000, REFERENCE, 20,
                         SimulationSettings(paths=4000, rate_volatility=5, seed=1), assumed_tna=30)
        self.assertAlmostEqual(flat.percentiles[95] - flat.percentiles[5], 0, places=6)
        self.assertGreater(noisy.percentiles[95] - noisy.percentiles[5], 1000)
        self.assertAlmostEqual(noisy.mean, flat.mean, delta=.1 * abs(noisy.percentiles[95] - noisy.percentiles[5]))

    def test_seeded_runs_repeat_and_tails_are_ordered(self):
        daily = [math.log1p(((index * 37) % 11 - 5) / 200) for index in range(60)]
        settings = SimulationSettings(paths=2000, seed=4)
        first = simulate(daily, [DEC], 1000, REFERENCE, 5, settings)
        self.assertEqual(first, simulate(daily, [DEC], 1000, REFERENCE, 5, settings))
        (_, var95, es95), (_, var99, es99) = first.tail
        self.assertLessEqual(es95, var95 + 1e-6)
        self.assertLessEqual(var99, var95 + 1e-6)
        self.assertLessEqual(es99, var99 + 1e-6)
        self.assertEqual(sum(first.histogram[1]), 2000)

    def test_invalid_inputs_are_rejected(self):
        cases = (([0.0] * 29, [DEC], SimulationSettings()),
                 ([0.0] * 40, [], SimulationSettings()),
                 ([0.0] * 40, [DEC], SimulationSettings(paths=10)),
                 ([0.0] * 40, [DEC], SimulationSettings(correlation=2)),
                 ([0.0] * 40, [DEC], SimulationSettings(rate_volatility=-1)),
                 ([0.0] * 40, [DEC], SimulationSettings(margin_buffer=0)),
                 ([0.0] * 40, [SimulationLeg("2026-10", 1, 1000, REFERENCE)], SimulationSettings()))
        for daily, legs, settings in cases:
            with self.subTest(settings=settings, legs=legs):
                with self.assertRaises(ValueError):
                    simulate(daily, legs, 1000, REFERENCE, 5, settings)


if __name__ == "__main__":
    unittest.main()

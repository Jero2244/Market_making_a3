"""Drift, EWMA volatility and jump adjustments of the historical sample."""
from datetime import date
import math
import random
import statistics
import unittest

from market_making.scenarios.calendar import advance_sessions, sessions_between
from market_making.scenarios.history import daily_log_returns, flag_jumps, normalize_history, with_horizon
from market_making.scenarios.model import ModelSettings, model_sample


def history(jump_at=None, calm_after=None):
    rng = random.Random(7)
    days = sessions_between(date(2024, 1, 2), date(2026, 9, 30))
    price, records = 1000.0, []
    for index, day in enumerate(days):
        volatility = .01 if calm_after is not None and index >= calm_after else .03
        price *= math.exp(rng.gauss(.002, volatility))
        if index == jump_at:
            price *= .5
        records.append({"date": day.isoformat(), "price": price})
    return normalize_history(records, days[0], days[-1], completed_before=date(2026, 10, 1))


def mean_log(returns):
    return statistics.fmean(math.log1p(value) for value in returns)


class ModelSampleTests(unittest.TestCase):
    def test_defaults_reproduce_raw_history(self):
        sample = history()
        model = model_sample(sample, 5, ModelSettings(), None)
        self.assertEqual(model.sample.returns, with_horizon(sample, 5).returns)
        self.assertIsNone(model.current_volatility)
        self.assertFalse(ModelSettings().adjusted)

    def test_zero_drift_centres_log_growth(self):
        model = model_sample(history(), 10, ModelSettings(drift="Zero"), None)
        self.assertAlmostEqual(mean_log(model.sample.returns), 0, places=12)
        self.assertAlmostEqual(statistics.fmean(value for _, value in model.daily), 0, places=12)

    def test_assumed_tna_drift_matches_financing_growth(self):
        reference = date(2026, 10, 1)
        model = model_sample(history(), 10, ModelSettings(drift="Assumed TNA", drift_tna=36.5), reference)
        calendar_days = (advance_sessions(reference, 10) - reference).days
        self.assertAlmostEqual(mean_log(model.sample.returns), math.log(1 + .365 * calendar_days / 365), places=12)

    def test_assumed_tna_drift_requires_a_rate(self):
        with self.assertRaises(ValueError):
            model_sample(history(), 5, ModelSettings(drift="Assumed TNA"), date(2026, 10, 1))

    def test_ewma_rescales_turbulent_history_to_calm_present(self):
        sample = history(calm_after=500)
        raw = model_sample(sample, 1, ModelSettings(), None)
        scaled = model_sample(sample, 1, ModelSettings(volatility="Current (EWMA)"), None)
        self.assertLess(scaled.current_volatility, .015)
        raw_sd = statistics.stdev(math.log1p(value) for value in raw.sample.returns)
        scaled_sd = statistics.stdev(math.log1p(value) for value in scaled.sample.returns)
        self.assertLess(scaled_sd, raw_sd * .7)
        self.assertEqual(len(scaled.sample.returns), len(raw.sample.returns))

    def test_ewma_windows_sum_their_daily_moves(self):
        sample = history()
        one_day = model_sample(sample, 1, ModelSettings(volatility="Current (EWMA)"), None)
        three_day = model_sample(sample, 3, ModelSettings(volatility="Current (EWMA)"), None)
        daily = [value for _, value in one_day.daily]
        # Without gaps, every 3-day window is three consecutive scaled daily moves.
        self.assertAlmostEqual(math.log1p(three_day.sample.returns[0]), sum(daily[:3]), places=12)

    def test_jump_is_flagged_and_can_be_excluded(self):
        sample = history(jump_at=300)
        jumps = flag_jumps(daily_log_returns(sample))
        self.assertEqual(len(jumps), 1)
        self.assertAlmostEqual(jumps[0].log_return, math.log(.5), delta=.15)
        kept = model_sample(sample, 5, ModelSettings(), None)
        dropped = model_sample(sample, 5, ModelSettings(exclude_jumps=True), None)
        self.assertEqual(len(kept.sample.returns) - len(dropped.sample.returns), 5)
        self.assertEqual(dropped.sample.excluded_windows - kept.sample.excluded_windows, 5)
        self.assertGreater(min(dropped.sample.returns), min(kept.sample.returns))
        self.assertNotIn(jumps[0].day, dict(dropped.daily))

    def test_quiet_history_flags_nothing(self):
        self.assertEqual(flag_jumps(daily_log_returns(history())), ())


if __name__ == "__main__":
    unittest.main()

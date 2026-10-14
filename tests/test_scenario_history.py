"""Historical estimates use valid, complete consecutive-session windows."""
from datetime import date, timedelta
import unittest

from market_making.scenarios.calendar import sessions_between
from market_making.scenarios.history import (
    HistorySample, annual_chunks, historical_frequency, histogram, lookback_start, normalize_history,
)


class HistoryTests(unittest.TestCase):
    def normalize(self, rows, start=date(2026, 10, 13), end=date(2026, 10, 16)):
        return normalize_history(rows, start, end, completed_before=end + timedelta(days=1))

    def test_two_session_return_is_compounded(self):
        sample = self.normalize([{"date": "2026-10-13", "price": 100},
                                 {"date": "2026-10-14", "price": 110},
                                 {"date": "2026-10-15", "price": 121}])
        self.assertEqual(len(sample.returns), 1)
        self.assertAlmostEqual(sample.returns[0], .21)
        self.assertEqual(sample.excluded_windows, 1)

    def test_sort_duplicates_and_midnight_daily_label(self):
        rows = [{"date": "2026-10-15T00:00:00Z", "price": 104},
                {"date": "2026-10-13T00:00:00Z", "price": 100},
                {"date": "2026-10-14T00:00:00Z", "price": 102},
                {"date": "2026-10-14", "price": 102}]
        sample = self.normalize(rows)
        self.assertEqual(len(sample.closes), 3)
        self.assertEqual(sample.closes[0][0], date(2026, 10, 13))
        self.assertAlmostEqual(sample.returns[0], .04)

    def test_conflicting_or_invalid_date_cannot_be_used(self):
        rows = [{"date": "2026-10-13", "price": 100},
                {"date": "2026-10-14", "price": 102},
                {"date": "2026-10-14", "price": 103},
                {"date": "2026-10-15", "price": 104},
                {"date": "2026-10-15", "price": 0}, {"date": "bad", "price": 100}, None]
        sample = self.normalize(rows)
        self.assertEqual(sample.conflicting_dates, 1)
        self.assertEqual(sample.invalid_records, 3)
        self.assertEqual(len(sample.closes), 1)
        self.assertFalse(sample.returns)

    def test_missing_middle_session_is_not_a_two_session_window(self):
        sample = self.normalize([{"date": "2026-10-13", "price": 100},
                                 {"date": "2026-10-15", "price": 104},
                                 {"date": "2026-10-16", "price": 106}])
        self.assertFalse(sample.returns)
        self.assertEqual(sample.excluded_windows, 2)

    def test_weekend_and_holiday_are_not_missing_sessions(self):
        sample = self.normalize([{"date": "2026-10-09", "price": 100},
                                 {"date": "2026-10-13", "price": 102},
                                 {"date": "2026-10-14", "price": 104}],
                                date(2026, 10, 9), date(2026, 10, 14))
        self.assertEqual(sample.expected_sessions, 3)
        self.assertEqual(sample.excluded_windows, 0)
        self.assertAlmostEqual(sample.returns[0], .04)

    def test_unfinished_out_of_range_and_nontrading_records_excluded(self):
        sample = normalize_history([{"date": day, "price": 100} for day in
                                    ("2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16")],
                                   date(2026, 10, 12), date(2026, 10, 16), completed_before=date(2026, 10, 15))
        self.assertEqual([day for day, _ in sample.closes], [date(2026, 10, 13), date(2026, 10, 14)])
        self.assertFalse(sample.returns)

    def test_large_returns_preserved(self):
        sample = self.normalize([{"date": "2026-10-13", "price": 100},
                                 {"date": "2026-10-14", "price": 102},
                                 {"date": "2026-10-15", "price": 1000}])
        self.assertEqual(sample.returns, (9.0,))

    def test_nonfinite_percentage_overflow_excluded_before_display(self):
        sample = self.normalize([{"date": "2026-10-13", "price": 1e-100},
                                 {"date": "2026-10-14", "price": 1},
                                 {"date": "2026-10-15", "price": 1e207}])
        self.assertFalse(sample.returns)
        self.assertEqual(sample.excluded_windows, 2)

    def sample(self, returns):
        return HistorySample((), tuple(returns), 0, 0, 0, 0, date(2026, 1, 1), date(2026, 10, 13))

    def test_directional_inclusive_thresholds_and_zero(self):
        sample = self.sample([-.05, 0, .05, .10] * 10)
        self.assertEqual(historical_frequency(sample, 5).percentage, 50)
        self.assertEqual(historical_frequency(sample, -5).percentage, 25)
        self.assertEqual(historical_frequency(sample, 0).percentage, 75)
        self.assertEqual(historical_frequency(sample, 20).percentage, 0)
        self.assertEqual(historical_frequency(sample, -20).percentage, 0)

    def test_minimum_sample_and_floating_threshold(self):
        self.assertIsNone(historical_frequency(self.sample([.05] * 29), 5).percentage)
        self.assertEqual(historical_frequency(self.sample([1.05 - 1] * 30), 5).percentage, 100)
        self.assertEqual(historical_frequency(self.sample([.95 - 1] * 30), -5).percentage, 100)

    def test_histogram_preserves_all_observations_and_marks_outside_target(self):
        edges, counts, low, high = histogram([-.03, 0, .04, 9], -10)
        self.assertEqual(sum(counts), 4)
        self.assertLessEqual(low, -10)
        self.assertGreaterEqual(high, 900)
        self.assertEqual(len(edges), len(counts) + 1)
        self.assertEqual(sum(histogram([0] * 30, 0)[1]), 30)

    def test_disjoint_annual_chunks_and_leap_lookback(self):
        start, end = date(2021, 10, 14), date(2026, 10, 13)
        chunks = annual_chunks(start, end)
        self.assertEqual(len(chunks), 6)
        self.assertEqual(chunks[0][0], start)
        self.assertEqual(chunks[-1][1], end)
        self.assertTrue(all(a[1] + timedelta(days=1) == b[0] for a, b in zip(chunks, chunks[1:])))
        self.assertEqual(lookback_start(date(2024, 2, 29), 1), date(2023, 2, 28))
        with self.assertRaises(ValueError):
            lookback_start(end, 2)

    def test_partial_coverage_reports_actual_coverage(self):
        days = sessions_between(date(2026, 1, 1), date(2026, 3, 31))
        sample = self.normalize([{"date": day.isoformat(), "price": 100} for day in days[-35:]],
                                date(2026, 1, 1), date(2026, 3, 31))
        self.assertEqual(sample.closes[0][0], days[-35])
        self.assertEqual(len(sample.returns), 33)
        self.assertGreater(sample.expected_sessions, len(sample.closes))
        self.assertEqual(historical_frequency(sample, 1).percentage, 0)


if __name__ == "__main__":
    unittest.main()

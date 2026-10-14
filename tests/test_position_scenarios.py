"""Hand-calculated P/L and calendar regressions, without broker requests."""
from datetime import date
import unittest

from market_making.scenarios.calendar import (
    BYMA_2026_CLOSED, BYMA_2026_OPEN, advance_sessions, is_session, sessions_between,
)
from market_making.scenarios.calculation import Position, calculate, simulated_midpoints


class ScenarioCalculationTests(unittest.TestCase):
    def position(self, quantity=2, current=110, entry=100, maturity=date(2026, 10, 30)):
        return Position("2026-10", quantity, entry, current, maturity)

    def test_long_from_today_and_entry(self):
        result = calculate(100, 5, [self.position()], date(2026, 10, 14))
        self.assertEqual(result.target_date, date(2026, 10, 16))
        self.assertEqual(result.projected_spot, 105)
        self.assertAlmostEqual(result.legs[0].projected_future, 114.1875)
        self.assertAlmostEqual(result.total_from_today, 837.5)
        self.assertAlmostEqual(result.total_since_entry, 2837.5)

    def test_short_has_opposite_signed_pnl(self):
        result = calculate(100, 5, [self.position(-2)], date(2026, 10, 14))
        self.assertAlmostEqual(result.total_from_today, -837.5)
        self.assertAlmostEqual(result.total_since_entry, -2837.5)

    def test_opposing_expiries_sum_signed_results(self):
        positions = [self.position(1), Position("2026-12", -3, 112, 115, date(2026, 12, 30))]
        result = calculate(100, -5, positions, date(2026, 10, 14))
        self.assertLess(result.legs[0].from_today, 0)
        self.assertGreater(result.legs[1].from_today, 0)
        self.assertAlmostEqual(result.total_from_today, sum(leg.from_today for leg in result.legs))
        self.assertAlmostEqual(result.total_since_entry, sum(leg.since_entry for leg in result.legs))

    def test_zero_stock_move_still_has_carry_decay(self):
        result = calculate(100, 0, [self.position(1)], date(2026, 10, 14))
        self.assertAlmostEqual(result.legs[0].projected_future, 108.75)
        self.assertAlmostEqual(result.total_from_today, -125)

    def test_negative_rate_is_allowed(self):
        result = calculate(100, 0, [self.position(1, 90, 90)], date(2026, 10, 14))
        self.assertLess(result.legs[0].implied_tna, 0)
        self.assertAlmostEqual(result.legs[0].projected_future, 91.25)
        self.assertAlmostEqual(result.total_from_today, 125)

    def test_zero_contracts_ignore_missing_prices_and_expiry(self):
        result = calculate(100, 5, [Position("2026-10", 0), Position("2026-12", 0)], date(2026, 10, 14))
        self.assertEqual(result.total_from_today, 0)
        self.assertEqual(result.total_since_entry, 0)
        self.assertTrue(all(leg.projected_future is None for leg in result.legs))

    def test_expiry_at_or_before_target_suppresses_combined_total(self):
        for maturity in (date(2026, 10, 1), date(2026, 10, 15), date(2026, 10, 16)):
            with self.subTest(maturity=maturity):
                result = calculate(100, 5, [self.position(maturity=maturity),
                                           Position("2026-12", 1, 100, 100, date(2026, 12, 30))], date(2026, 10, 14))
                self.assertIsNone(result.legs[0].from_today)
                self.assertIsNone(result.total_from_today)
                self.assertIsNone(result.total_since_entry)
                self.assertAlmostEqual(result.legs[1].from_today, 500)

    def test_weekend_and_holiday_use_calendar_days_for_carry(self):
        result = calculate(100, 0, [self.position(1)], date(2026, 10, 9))
        self.assertEqual(result.target_date, date(2026, 10, 14))
        self.assertAlmostEqual(result.legs[0].projected_future, 100 * (1 + .1 * 16 / 21))

    def test_invalid_prices_moves_quantities_and_overflow(self):
        for spot in (0, -1, True, None, float("nan"), float("inf")):
            with self.subTest(spot=spot), self.assertRaises(ValueError):
                calculate(spot, 0, [self.position()], date(2026, 10, 14))
        for move in (-100, -101, float("nan"), float("inf"), True):
            with self.subTest(move=move), self.assertRaises(ValueError):
                calculate(100, move, [self.position()], date(2026, 10, 14))
        for quantity in (1.5, True, "2"):
            with self.subTest(quantity=quantity), self.assertRaises(ValueError):
                calculate(100, 0, [self.position(quantity)], date(2026, 10, 14))
        for current in (0, -2, None, float("nan")):
            with self.subTest(current=current), self.assertRaises(ValueError):
                calculate(100, 0, [self.position(current=current)], date(2026, 10, 14))
        with self.assertRaises(ValueError):
            calculate(1e308, 1e308, [self.position()], date(2026, 10, 14))

    def test_duplicate_expiry_rejected(self):
        with self.assertRaises(ValueError):
            calculate(100, 0, [self.position(), self.position()], date(2026, 10, 14))

    def test_assumed_tna_prices_at_financing_rate_and_keeps_current_implied(self):
        row = Position("2026-10", 1, 10000, 10500, date(2026, 10, 30))
        result = calculate(10000, 5, [row], date(2026, 9, 30), horizon=3, assumed_tna=35)
        leg = result.legs[0]
        self.assertEqual(result.target_date, date(2026, 10, 5))
        self.assertAlmostEqual(leg.implied_tna, 60.8333333333333)
        self.assertEqual(leg.projection_tna, 35)
        self.assertAlmostEqual(leg.projected_future, 10751.712328767123)
        self.assertAlmostEqual(leg.from_today, (10751.712328767123 - 10500) * 100)
        self.assertAlmostEqual(leg.since_entry, (10751.712328767123 - 10000) * 100)
        repriced_tna = (leg.projected_future / result.projected_spot - 1) * 365 / 25 * 100
        self.assertAlmostEqual(repriced_tna, 35)

    def test_zero_assumed_tna_is_an_explicit_override(self):
        result = calculate(100, 5, [self.position(1)], date(2026, 10, 14), assumed_tna=0)
        self.assertEqual(result.legs[0].projected_future, 105)
        self.assertEqual(result.legs[0].projection_tna, 0)
        self.assertAlmostEqual(result.total_from_today, -500)

    def test_common_assumed_rate_applies_to_both_expiries_with_signed_pnl(self):
        positions = [self.position(1), Position("2026-12", -3, 112, 115, date(2026, 12, 30))]
        result = calculate(100, 5, positions, date(2026, 10, 14), assumed_tna=35)
        self.assertNotEqual(result.legs[0].implied_tna, result.legs[1].implied_tna)
        for leg, remaining, current, q in zip(result.legs, (14, 75), (110, 115), (1, -3)):
            self.assertEqual(leg.projection_tna, 35)
            expected = 105 * (1 + .35 * remaining / 365)
            self.assertAlmostEqual(leg.projected_future, expected)
            self.assertAlmostEqual(leg.from_today, q * 100 * (expected - current))
        self.assertAlmostEqual(result.total_from_today, sum(leg.from_today for leg in result.legs))

    def test_negative_assumed_rate_allowed_if_projection_stays_positive(self):
        result = calculate(100, 0, [self.position(1)], date(2026, 10, 14), assumed_tna=-10)
        self.assertAlmostEqual(result.legs[0].projected_future, 100 * (1 - .1 * 14 / 365))
        with self.assertRaises(ValueError):
            calculate(100, 0, [self.position(1)], date(2026, 10, 14), assumed_tna=-10000)

    def test_invalid_assumed_tnas_are_rejected(self):
        for assumption in (True, "35", float("nan"), float("inf")):
            with self.subTest(assumption=assumption), self.assertRaises(ValueError):
                calculate(100, 0, [self.position(1)], date(2026, 10, 14), assumed_tna=assumption)

    def test_blank_assumption_preserves_each_current_implied_rate(self):
        result = calculate(100, 0, [self.position(1)], date(2026, 10, 14), assumed_tna=None)
        self.assertEqual(result.legs[0].projection_tna, result.legs[0].implied_tna)
        self.assertAlmostEqual(result.legs[0].projected_future, 108.75)

    def test_assumed_rate_retains_expiry_gate(self):
        result = calculate(100, 5, [self.position(maturity=date(2026, 10, 16))],
                           date(2026, 10, 14), assumed_tna=35)
        self.assertIsNone(result.total_from_today)
        self.assertIsNone(result.legs[0].projected_future)


class TradingCalendarTests(unittest.TestCase):
    def test_2026_closures_and_settlement_only_holidays(self):
        self.assertTrue(all(not is_session(day) for day in BYMA_2026_CLOSED))
        self.assertTrue(all(is_session(day) for day in BYMA_2026_OPEN))
        self.assertEqual(advance_sessions(date(2026, 11, 5)), date(2026, 11, 11))
        self.assertEqual(advance_sessions(date(2026, 12, 23)), date(2026, 12, 28))

    def test_weekends_new_year_and_nontrading_reference(self):
        self.assertEqual(advance_sessions(date(2026, 10, 10)), date(2026, 10, 14))
        self.assertEqual(advance_sessions(date(2026, 12, 30)), date(2027, 1, 5))
        self.assertEqual(sessions_between(date(2026, 12, 30), date(2026, 12, 31)), (date(2026, 12, 30),))

    def test_lookback_calendar_years_available(self):
        for year in range(2021, 2027):
            self.assertGreater(len(sessions_between(date(year, 1, 1), date(year, 12, 31))), 200)


class SimulatedImportTests(unittest.TestCase):
    def book(self, bid=100, ask=102):
        return {"expiry": "2026-10", "books": {"future": {
            "symbol": "GGAL/OCT26", "provider": "remarkets", "simulated": True,
            "bids": [{"price": bid}], "offers": [{"price": ask}],
            "receipt_timestamp": "2026-10-14T12:00:00Z", "diagnostics": [],
        }}}

    def test_midpoint_provenance_and_timestamp(self):
        item = simulated_midpoints([self.book()])["2026-10"]
        self.assertEqual(item.price, 101)
        self.assertEqual(item.source, "REMARKETS simulated")
        self.assertEqual(item.receipt_timestamp, "2026-10-14T12:00:00Z")

    def test_invalid_simulated_books_are_not_copied(self):
        reports = [self.book(102, 100), self.book(100, 100), self.book(float("nan"), 102),
                   {"expiry": "2026-10", "books": None}]
        for key, value in (("simulated", False), ("symbol", "WRONG"), ("provider", "production"),
                           ("diagnostics", ["symbol_mismatch"])):
            item = self.book()
            item["books"]["future"][key] = value
            reports.append(item)
        for item in reports:
            with self.subTest(report=item):
                self.assertEqual(simulated_midpoints([item]), {})


if __name__ == "__main__":
    unittest.main()

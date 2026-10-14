"""Return-distribution controls and tail shading, using a hidden Tk window."""
from datetime import date
import math
import tkinter as tk
import unittest

from market_making.desktop.scenario_tab import ScenarioTab
from market_making.scenarios.calendar import sessions_between
from market_making.scenarios.distribution import ReturnDensity
from market_making.scenarios.history import HistorySample, normalize_history


class DistributionControlsTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.tab = ScenarioTab(self.root, env_variable=tk.StringVar(self.root), get_reports=lambda: [])
        self.addCleanup(self.tab.close)
        self.tab.reference_date.set("2026-10-14")
        sample = HistorySample((), tuple([-.1, -.05, 0, .05, .1] * 10), 0, 0, 0, 0,
                               date(2026, 1, 1), date(2026, 10, 13))
        self.tab.history = self.tab.horizon_sample = sample
        self.tab.density = ReturnDensity.fit(sample.returns)
        self.tab._sample_key = (id(sample), 2)

    def test_comparison_updates_result_and_ignores_hidden_y(self):
        self.tab.range_upper.set("unused")
        self.tab.range_lower.set("5")
        for comparison, count, complement in (("More than X", 10, "<="), (">= X", 20, "<"),
                                               ("Less than X", 30, ">="), ("<= X", 40, ">")):
            with self.subTest(comparison=comparison):
                self.tab.range_comparison.set(comparison)
                self.assertIn(f"({count}/50)", self.tab.range_result.get())
                self.assertIn(comparison.replace("X", "+5%"), self.tab.range_result.get())
                self.assertIn(f"Historical {complement} +5%: {100 - 2 * count:.2f}%", self.tab.range_result.get())
                self.assertEqual(self.tab.range_upper_controls.winfo_manager(), "")
                self.assertEqual(self.tab.range_lower_label.cget("text"), "X")

    def test_switching_back_restores_interval_and_y(self):
        self.tab.range_comparison.set("More than X")
        self.tab.range_lower.set("-5")
        self.tab.range_upper.set("5")
        self.tab.range_comparison.set("From X to Y")
        self.assertEqual(self.tab.range_upper_controls.winfo_manager(), "pack")
        self.assertEqual(self.tab.range_lower_label.cget("text"), "From")
        self.assertIn("(30/50)", self.tab.range_result.get())
        self.assertEqual(self.tab.selected_range, (-.05, .05))
        self.assertEqual(len(self.tab.chart.find_withtag("range_bound")), 2)

    def test_price_conversion_preserves_threshold_with_invalid_hidden_y(self):
        self.tab.range_comparison.set(">= X")
        self.tab.range_lower.set("5")
        self.tab.range_upper.set("unused")
        self.tab.spot.set("10000")
        self.tab.range_unit.set("Final price (ARS)")
        self.assertEqual(self.tab.range_lower.get(), "10500")
        self.assertIn(">= 10,500.00 ARS", self.tab.range_result.get())
        self.assertIn("(20/50)", self.tab.range_result.get())
        self.tab.range_unit.set("Return (%)")
        self.assertEqual(self.tab.range_lower.get(), "5")
        self.assertIn("(20/50)", self.tab.range_result.get())

    def test_each_tail_has_one_finite_boundary_and_shades_correct_side(self):
        self.tab.range_lower.set("0")
        for comparison in ("More than X", "Less than X", ">= X", "<= X"):
            with self.subTest(comparison=comparison):
                self.tab.range_comparison.set(comparison)
                chart = self.tab.chart
                bounds = chart.find_withtag("range_bound")
                areas = chart.find_withtag("range_area")
                self.assertEqual(len(bounds), 1)
                self.assertEqual(len(areas), 1)
                boundary_x = chart.coords(bounds[0])[0]
                xs = chart.coords(areas[0])[::2]
                self.assertTrue(all(math.isfinite(value) for value in chart.coords(areas[0])))
                if comparison in ("More than X", ">= X"):
                    self.assertGreaterEqual(min(xs), boundary_x - 1e-9)
                    self.assertGreater(max(xs), boundary_x)
                else:
                    self.assertLessEqual(max(xs), boundary_x + 1e-9)
                    self.assertLess(min(xs), boundary_x)

    def calculate_position(self, direction="Long", current="100"):
        self.tab.spot.set("100")
        self.tab.move.set("5")
        self.tab.assumed_tna.set("0")
        row = self.tab.positions["2026-10"]
        row["contracts"].set("1")
        row["direction"].set(direction)
        row["current"].set(current)
        row["entry"].set("90")
        self.tab.recalculate()

    def test_pnl_summary_shows_combined_target_and_profit_estimates(self):
        self.calculate_position()
        self.assertEqual(self.tab.pnl_scenario.get(), "Scenario P/L: +500.00 ARS")
        self.assertIn("Chance of finishing with at least +500.00 ARS: ", self.tab.pnl_target_chance.get())
        self.assertTrue(self.tab.pnl_target_chance.get().endswith("%"))
        self.assertTrue(self.tab.pnl_profit_chance.get().endswith("%"))
        self.assertIn("Combined gross P/L from today", self.tab.pnl_probability_note.get())
        self.assertIn("2026-10-16", self.tab.pnl_probability_note.get())
        target_chance = self.tab.pnl_target_chance.get()
        profit_chance = self.tab.pnl_profit_chance.get()
        self.tab.range_lower.set("invalid range")
        self.assertEqual(self.tab.pnl_target_chance.get(), target_chance)
        self.assertEqual(self.tab.pnl_profit_chance.get(), profit_chance)

    def test_pnl_summary_clears_when_scenario_inputs_change(self):
        self.calculate_position()
        self.tab.positions["2026-10"]["direction"].set("Short")
        self.assertIsNone(self.tab.result)
        self.assertEqual(self.tab.pnl_scenario.get(), "Scenario P/L: -- ARS")
        self.assertTrue(self.tab.pnl_profit_chance.get().endswith("--"))
        self.tab.recalculate()
        self.assertEqual(self.tab.pnl_scenario.get(), "Scenario P/L: -500.00 ARS")
        self.assertTrue(self.tab.pnl_profit_chance.get().endswith("%"))

    def test_history_fetch_updates_existing_pnl_without_recalculation(self):
        days = sessions_between(date(2026, 1, 1), date(2026, 3, 31))
        sample = normalize_history([{"date": day.isoformat(), "price": 100 + index}
                                    for index, day in enumerate(days)], days[0], days[-1],
                                   completed_before=date(2026, 4, 1))
        self.tab.history = None
        self.calculate_position()
        self.assertEqual(self.tab.pnl_scenario.get(), "Scenario P/L: +500.00 ARS")
        self.assertTrue(self.tab.pnl_target_chance.get().endswith("--"))
        self.assertIn("Fetch PPI history", self.tab.pnl_probability_note.get())
        self.tab.controller.events.put((self.tab.controller.generation, "history", sample, None))
        self.tab.poll()
        self.assertTrue(self.tab.pnl_target_chance.get().endswith("%"))

    def test_insufficient_history_and_expired_positions_hide_probability(self):
        self.calculate_position()
        self.tab.horizon_sample = HistorySample((), (0,) * 29, 0, 0, 0, 0,
                                              date(2026, 1, 1), date(2026, 10, 13))
        self.tab.density = None
        self.tab.refresh_frequency()
        self.assertTrue(self.tab.pnl_target_chance.get().endswith("--"))
        self.assertIn("Insufficient history", self.tab.pnl_probability_note.get())
        self.tab.positions["2026-10"]["maturity"].set("2026-10-16")
        self.tab.recalculate()
        self.assertEqual(self.tab.pnl_scenario.get(), "Scenario P/L: unavailable")
        self.assertTrue(self.tab.pnl_profit_chance.get().endswith("--"))

    def test_no_positions_does_not_present_chance_of_profit(self):
        self.tab.spot.set("100")
        self.tab.recalculate()
        self.assertEqual(self.tab.pnl_scenario.get(), "Scenario P/L: +0.00 ARS")
        self.assertTrue(self.tab.pnl_profit_chance.get().endswith("--"))
        self.assertIn("active position", self.tab.pnl_probability_note.get())


if __name__ == "__main__":
    unittest.main()

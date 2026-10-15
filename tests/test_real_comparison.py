"""Gross directional differences and observation validity, without network calls."""
from dataclasses import replace
from datetime import datetime, timezone
import unittest

from market_making.comparison.models import (
    DEFAULT_INSTRUMENTS, Difference, Quote, compare, remarkets_quote,
)
from market_making.ppi.models import Level
from market_making.ppi.remarkets import RequestIdentity, Snapshot
from market_making.ppi import remarkets
from unittest.mock import Mock

SYMBOL = "GGAL/OCT26"
RECEIPT = datetime(2026, 10, 14, 20, 0, tzinfo=timezone.utc)


def quotes():
    return (Quote(SYMBOL, "A3", Level(5812, 6), Level(5820, 9), 100, RECEIPT, 5820, RECEIPT),
            Quote(SYMBOL, "reMarkets", Level(5809, 4), Level(5811, 3), 100, RECEIPT))


def raw_snapshot(symbol=SYMBOL, bid=5809, ask=5811):
    return Snapshot({"status": "OK", "marketData": {
        "BI": [{"price": bid, "size": 4}], "OF": [{"price": ask, "size": 3}],
        "LA": {"price": 5810, "date": "2026-10-14T20:00:00Z"},
    }}, RequestIdentity("ROFX", symbol))


class ComparisonTests(unittest.TestCase):
    def compare(self, real=None, sim=None, **kwargs):
        original = quotes()
        return compare(DEFAULT_INSTRUMENTS[SYMBOL], real or original[0], sim or original[1],
                       now=kwargs.pop("now", 100), **kwargs)

    def test_one_tick_boundary_and_all_units(self):
        buy, sell = self.compare()
        self.assertTrue(buy.qualifies)
        self.assertEqual((buy.price, buy.ticks, buy.per_contract), (1, 1, 100))
        self.assertAlmostEqual(buy.percent, 1 / 5812 * 100)
        self.assertEqual((buy.remarkets_quantity, buy.reference_quantity), (3, 6))
        self.assertEqual(sell.price, -11)
        self.assertFalse(sell.qualifies)

    def test_sell_and_quantity_are_from_simulated_bid(self):
        _, sell = self.compare(sim=replace(quotes()[1], bid=Level(5823, 7), ask=Level(5824, 2)))
        self.assertEqual((sell.price, sell.per_contract), (3, 300))
        self.assertEqual((sell.remarkets_quantity, sell.reference_quantity), (7, 9))
        self.assertTrue(sell.qualifies)

    def test_rfx20_and_wti_tick_boundaries_and_contract_differences(self):
        for symbol, real_bid, real_ask, sim_ask, sim_sell_bid, tick, contract_difference in (
                ("RFX20/OCT26", 620100, 620200, 620000, 620300, 100, 100),
                ("WTI/NOV26", 70.13, 70.14, 70.12, 70.15, .01, .1)):
            with self.subTest(symbol=symbol):
                real = replace(quotes()[0], symbol=symbol, bid=Level(real_bid, 6),
                               ask=Level(real_ask, 9))
                simulated = remarkets_quote(raw_snapshot(symbol, bid=sim_ask - tick, ask=sim_ask),
                                            symbol, now=100, receipt=RECEIPT)
                self.assertFalse(simulated.problems)
                buy, _ = compare(DEFAULT_INSTRUMENTS[symbol], real, simulated, now=100)
                self.assertTrue(buy.qualifies)
                self.assertEqual(buy.ticks, 1)
                self.assertEqual(buy.per_contract, contract_difference)
                self.assertFalse(compare(DEFAULT_INSTRUMENTS[symbol], real, simulated,
                                         now=100, min_ticks=1.01)[0].qualifies)
                simulated = replace(simulated, bid=Level(sim_sell_bid, 4),
                                    ask=Level(sim_sell_bid + tick, 3))
                _, sell = compare(DEFAULT_INSTRUMENTS[symbol], real, simulated, now=100)
                self.assertTrue(sell.qualifies)
                self.assertAlmostEqual(sell.ticks, 1)
                self.assertAlmostEqual(sell.per_contract, contract_difference)

    def test_decimal_tick_boundary_and_zero_minimum(self):
        spec = replace(DEFAULT_INSTRUMENTS[SYMBOL], tick=.1)
        real, sim = quotes()
        real = replace(real, bid=Level(10.2, 1), ask=Level(10.4, 1))
        sim = replace(sim, bid=Level(10, 1), ask=Level(10.1, 1))
        buy, _ = compare(spec, real, sim, now=100)
        self.assertTrue(buy.qualifies)
        self.assertEqual(buy.ticks, 1)
        real = replace(real, bid=sim.ask)
        self.assertFalse(compare(spec, real, sim, now=100, min_ticks=0)[0].qualifies)

    def test_last_prices_do_not_drive_either_direction(self):
        baseline = self.compare()
        modified = self.compare(replace(quotes()[0], last=99999), replace(quotes()[1], last=1))
        self.assertEqual(baseline, modified)

    def test_missing_unused_side_does_not_block_buy(self):
        buy, sell = self.compare(real=replace(quotes()[0], ask=None), sim=replace(quotes()[1], bid=None))
        self.assertTrue(buy.qualifies)
        self.assertIsNone(sell.price)

    def test_invalid_or_old_observations_disable_highlights(self):
        real, _ = quotes()
        for changed in (replace(real, symbol="GGAL/DIC26"), replace(real, source="Other"),
                        replace(real, received_monotonic=89.9), replace(real, received_monotonic=101),
                        replace(real, problems=("invalid_bid",)), replace(real, bid=Level(5821, 1)),
                        replace(real, bid=Level(float("nan"), 1)), replace(real, bid=Level(5812, 0))):
            with self.subTest(quote=changed):
                result = self.compare(real=changed)
                self.assertFalse(any(d.qualifies for d in result))
                self.assertTrue(all(d.price is None for d in result))
        self.assertTrue(self.compare(real=replace(real, received_monotonic=90))[0].qualifies)

    def test_stopped_and_missing_sources(self):
        self.assertFalse(any(d.qualifies for d in self.compare(active=False)))
        result = compare(DEFAULT_INSTRUMENTS[SYMBOL], None, quotes()[1], now=100)
        self.assertEqual(result[0], Difference("BUY"))

    def test_invalid_settings(self):
        for kwargs in ({"min_ticks": -1}, {"min_ticks": float("nan")}, {"max_age": 0},
                       {"max_age": float("inf")}):
            with self.assertRaises(ValueError):
                self.compare(**kwargs)

    def test_remarkets_keeps_identity_and_structural_checks(self):
        q = remarkets_quote(raw_snapshot(), SYMBOL, now=100, receipt=RECEIPT)
        self.assertEqual((q.bid.price, q.ask.price, q.last), (5809, 5811, 5810))
        self.assertEqual(q.last_time, RECEIPT)
        self.assertFalse(q.problems)  # Unknown source time is advisory, never fabricated.
        bad = raw_snapshot("GGAL/DIC26")
        self.assertTrue(remarkets_quote(bad, SYMBOL, now=100).problems)
        bad = raw_snapshot()
        bad.raw["marketData"]["BI"] = [{"price": "bad", "size": 1}]
        self.assertTrue(remarkets_quote(bad, SYMBOL, now=100).problems)

    def test_remarkets_empty_side_and_crossed_book(self):
        raw = raw_snapshot()
        raw.raw["marketData"]["BI"] = []
        q = remarkets_quote(raw, SYMBOL, now=100)
        self.assertIsNone(q.bid)
        self.assertIsNotNone(q.ask)
        self.assertFalse(q.problems)
        self.assertTrue(remarkets_quote(raw_snapshot(bid=5820, ask=5810), SYMBOL, now=100).problems)

    def test_last_trade_request_is_opt_in_for_existing_callers(self):
        client = remarkets.Client(remarkets.Credentials({}))
        client._request = Mock(return_value=raw_snapshot().raw)
        instrument = {"symbol": SYMBOL, "market_id": "ROFX", "provider": "remarkets"}
        try:
            client.snapshot(instrument)
            self.assertEqual(client._request.call_args.kwargs["params"]["entries"], "BI,OF")
            client.snapshot(instrument, include_last=True)
            self.assertEqual(client._request.call_args.kwargs["params"]["entries"], "BI,OF,LA")
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()

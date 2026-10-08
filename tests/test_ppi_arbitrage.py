from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest
from market_making.ppi.models import parse_book
from market_making.ppi.arbitrage import ContractMetadata, assess, fair_future, period_rate


NOW = datetime(2026, 10, 7, 15, tzinfo=timezone.utc)


def book(bid=99, ask=100, quantity=500, **kwargs):
    return parse_book({'date': NOW.isoformat(), 'bids': [{'price': bid, 'quantity': quantity}],
                       'offers': [{'price': ask, 'quantity': quantity}]},
                      {'price': 99.5, 'date': NOW.isoformat()}, receipt=NOW,
                      source_semantics_verified=True, **kwargs)


class BookTests(unittest.TestCase):
    def test_bid_ask_last_and_timestamps_distinct(self):
        b = book()
        self.assertEqual((b.bid.price, b.ask.price, b.last_trade), (99, 100, 99.5))
        self.assertEqual(b.receipt_time, NOW)
        self.assertEqual(b.source_time, NOW)
        self.assertEqual(b.blockers, [])

    def test_missing_crossed_invalid_duplicate_and_stale_books(self):
        valid = {'date': NOW.isoformat(), 'bids': [{'price': 99, 'quantity': 10}], 'offers': [{'price': 100, 'quantity': 10}]}
        for data in (None, {}, {**valid, 'date': '2026-10-07T15:00:00'},
                     {**valid, 'date': (NOW - timedelta(seconds=31)).isoformat()},
                     {**valid, 'date': (NOW + timedelta(seconds=3)).isoformat()},
                     {**valid, 'bids': [{'price': 101, 'quantity': 10}]},
                     {**valid, 'bids': valid['bids'] * 2},
                     {**valid, 'offers': [{'price': float('nan'), 'quantity': 10}]},
                     {**valid, 'offers': [{'price': 100, 'quantity': 0}]}):
            with self.subTest(data=data):
                self.assertTrue(parse_book(data, receipt=NOW, source_semantics_verified=True).blockers)

    def test_default_never_certifies_live_by_receipt(self):
        b = parse_book({'date': NOW.isoformat()}, receipt=NOW)
        self.assertIn('source_timestamp_scope_clock_and_session_unverified', b.blockers)

    def test_best_price_not_last_or_array_first(self):
        b = parse_book({'date': NOW.isoformat(), 'bids': [{'price': 90, 'quantity': 1}, {'price': 99, 'quantity': 2}],
                        'offers': [{'price': 110, 'quantity': 5}, {'price': 100, 'quantity': 7}]}, receipt=NOW)
        self.assertEqual((b.bid.price, b.bid.quantity, b.ask.price, b.ask.quantity), (99, 2, 100, 7))


class ArbitrageTests(unittest.TestCase):
    def test_manual_only_relaxes_exact_diagnostics(self):
        spot, future = book(), book(110, 111, 10)
        spot.source_time = None
        future.blockers = ['source_timestamp_scope_clock_and_session_unverified', 'invalid_last_trade']
        before = list(future.blockers)
        self.assertIsNone(assess(spot, future, self.meta, **self.kw)['cash_carry_edge'])
        result = assess(spot, future, self.meta, **self.kw, manual_check=True)
        self.assertAlmostEqual(result['cash_carry_edge'], 6)
        self.assertIn('source_timestamp_missing_or_ambiguous', result['caveats'])
        self.assertIn('invalid_last_trade', result['caveats'])
        self.assertEqual(future.blockers, before)
        self.assertFalse(result['executable'])
        self.assertFalse(result['live_freshness_established'])
        for meta in (replace(self.meta, verified=False), replace(self.meta, settlement='A-24HS'),
                     replace(self.meta, maturity=NOW), replace(self.meta, multiplier=0)):
            self.assertIsNone(assess(spot, future, meta, **self.kw, manual_check=True)['cash_carry_edge'])
        future.blockers.append('invalid_or_missing_bids')
        self.assertIsNone(assess(spot, future, self.meta, **self.kw, manual_check=True)['cash_carry_edge'])

    def setUp(self):
        self.meta = ContractMetadata('GGAL', 'ARS', 'INMEDIATA', NOW + timedelta(days=30),
                                     100, 1, 'reviewed synthetic fixture', True, True, True)
        self.kw = dict(spot_settlement='INMEDIATA', borrow_tna=36.5, lend_tna=18.25,
                       basis=365, costs=1, now=NOW)

    def test_simple_rate_and_user_formula(self):
        self.assertAlmostEqual(period_rate(36.5, 30, 365), .03)
        self.assertAlmostEqual(fair_future(100, 36.5, 30, 365), 103)
        self.assertAlmostEqual(period_rate(36, 1, 360), .001)
        for args in ((30, 0, 365), (30, -1, 365), (-30, 1, 365), (float('inf'), 1, 365), (30, 1, 366)):
            with self.assertRaises(ValueError):
                period_rate(*args)

    def test_executable_sides_costs_and_multiplier_depth(self):
        result = assess(book(), book(110, 111, 10), self.meta, **self.kw)
        self.assertAlmostEqual(result['cash_carry_edge'], 110 - 100 * 1.03 - 1)
        self.assertAlmostEqual(result['reverse_edge'], 99 * 1.015 - 111 - 1)
        self.assertEqual(result['cash_carry_capacity_contracts'], 5)
        self.assertFalse(result['executable'])
        self.assertIn('overnight_rollover_is_hypothetical_not_locked_maturity_funding', result['blockers'])

    def test_unknown_rates_costs_depth_and_feasibility_block(self):
        kw = {**self.kw, 'borrow_tna': None, 'costs': None, 'contracts': 6}
        result = assess(book(), book(110, 111, 10), self.meta, **kw)
        self.assertIsNone(result['cash_carry_edge'])
        self.assertIsNone(result['reverse_edge'])
        for blocker in ('all_in_costs_unknown', 'cash_carry_rate_unavailable', 'cash_carry_insufficient_depth',
                        'stock_borrow_unverified', 'dividends_unverified', 'margin_and_variation_margin_funding_unverified'):
            self.assertIn(blocker, result['blockers'])

    def test_metadata_currency_settlement_maturity_and_staleness_gate(self):
        for meta in (replace(self.meta, verified=False), replace(self.meta, currency='USD'),
                     replace(self.meta, underlying='GGAL ADR'), replace(self.meta, settlement='A-24HS'),
                     replace(self.meta, maturity=NOW), replace(self.meta, multiplier=0),
                     replace(self.meta, maturity=NOW.replace(tzinfo=None))):
            result = assess(book(), book(110, 111, 10), meta, **self.kw)
            self.assertIsNone(result['cash_carry_edge'])
            self.assertTrue(result['blockers'])
        result = assess(book(), book(110, 111, 10), self.meta, **{**self.kw, 'now': NOW + timedelta(seconds=31)})
        self.assertIsNone(result['cash_carry_edge'])

    def test_quote_scale_and_no_false_executable_claim(self):
        result = assess(book(), book(1100, 1110, 10), replace(self.meta, price_scale=.1),
                        **self.kw, funding_locked=True, stock_borrow_verified=True,
                        dividends_verified=True, margin_verified=True)
        self.assertAlmostEqual(result['cash_carry_edge'], 6)
        self.assertFalse(result['executable'])
        self.assertIn('read_only_research_cannot_establish_simultaneous_execution', result['blockers'])

    def test_default_current_utc_rejects_old_books_and_expired_contract(self):
        current = datetime.now(timezone.utc)
        old = current - timedelta(days=2)
        spot = book()
        future = book(110, 111, 10)
        for snapshot in (spot, future):
            snapshot.source_time = old
            snapshot.receipt_time = old
        kw = {k: v for k, v in self.kw.items() if k != 'now'}
        result = assess(spot, future, replace(self.meta, maturity=current + timedelta(days=30)), **kw)
        self.assertIn('source_timestamp_stale_or_future', result['blockers'])
        self.assertIsNone(result['cash_carry_edge'])
        self.assertIsNone(result['reverse_edge'])
        result = assess(spot, future, replace(self.meta, maturity=current - timedelta(days=1)), **kw)
        self.assertIn('invalid_or_expired_contract_horizon_units_or_size', result['blockers'])
        self.assertIsNone(result['cash_carry_edge'])
        self.assertIsNone(result['reverse_edge'])

    def test_explicit_historical_replay_uses_supplied_aware_instant(self):
        replay = datetime(2000, 1, 1, tzinfo=timezone.utc)
        spot = book()
        future = book(110, 111, 10)
        for snapshot in (spot, future):
            snapshot.source_time = replay
            snapshot.receipt_time = replay
        result = assess(spot, future, replace(self.meta, maturity=replay + timedelta(days=30)),
                        **{**self.kw, 'now': replay})
        self.assertAlmostEqual(result['cash_carry_edge'], 6)
        self.assertFalse(result['executable'])

    def test_finite_inputs_with_overflow_rejected(self):
        with self.assertRaisesRegex(ValueError, 'nonfinite_calculation'):
            period_rate(1e308, 1e308, 365)
        with self.assertRaisesRegex(ValueError, 'nonfinite_calculation'):
            fair_future(1e308, 100, 365, 365)

    def test_edge_intermediate_and_depth_overflow_leave_no_edges(self):
        meta = replace(self.meta, maturity=NOW + timedelta(days=365))
        cases = [
            # Cash-carry financed spot overflow.
            (book(9e307, 1e308), book(110, 111, 10), meta, {'borrow_tna': 100}),
            # Reverse overflow after valid cash-carry computation must not publish partial edges.
            (book(9e307, 1e308), book(110, 111, 10), meta, {'borrow_tna': 0, 'lend_tna': 200}),
            # Future quote normalization overflow.
            (book(), book(9e307, 1e308, 10), replace(meta, price_scale=100), {}),
            # Rate intermediate overflow.
            (book(), book(110, 111, 10), meta, {'borrow_tna': 1e308}),
            # Per-contract depth division overflow (even if min would hide it).
            (book(quantity=1e308), book(110, 111, 10), replace(meta, multiplier=1e-308), {}),
            # Cost subtraction overflows a negative finite gross edge.
            (book(9e307, 1e308), book(1, 2, 10), meta, {'borrow_tna': 0, 'costs': 1e308}),
        ]
        for spot, future, contract, overrides in cases:
            with self.subTest(overrides=overrides, scale=contract.price_scale):
                result = assess(spot, future, contract, **{**self.kw, **overrides})
                self.assertIn('invalid_book_rate_basis_or_cost', result['blockers'])
                self.assertIsNone(result['cash_carry_edge'])
                self.assertIsNone(result['reverse_edge'])
                import json
                json.dumps(result, allow_nan=False)

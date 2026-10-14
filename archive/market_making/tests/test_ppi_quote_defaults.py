"""Offline coverage of the no-config GGAL price/yield checker."""
import io
import json
import unittest
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from market_making.ppi import cli, monitor, monitor_config
from market_making.ppi.models import parse_book, timestamp

NOW = datetime(2026, 10, 9, 15, tzinfo=timezone.utc)


def book(bid, ask):
    return parse_book({'bids': [{'price': bid, 'quantity': 1000}],
                       'offers': [{'price': ask, 'quantity': 1000}]}, receipt=NOW)


class QuoteDefaultsTests(unittest.TestCase):
    def test_last_weekday_rule_and_local_end_of_day(self):
        for expiry, day in (('2026-10', 30), ('2026-12', 31), ('2026-02', 27),
                            ('2026-01', 30), ('2028-02', 29)):
            with self.subTest(expiry=expiry):
                value = timestamp(monitor_config.last_weekday_maturity(expiry))
                self.assertEqual(value.day, day)
                self.assertLess(value.weekday(), 5)
                self.assertEqual((value.hour, value.minute, value.second), (23, 59, 59))
                self.assertEqual(value.utcoffset().total_seconds(), -10800)

    def test_quotes_and_yield_need_no_caucion_costs_or_contract_size(self):
        cfg = monitor_config.default_watch_config()
        self.assertTrue(cfg['quote_only'])
        self.assertIsNone(cfg['rates']['borrow_tna'])
        for contract in cfg['futures']:
            result = monitor.evaluate(book(6100, 6105), book(6125, 6130), cfg, contract, now=NOW)
            days = (timestamp(contract['maturity']) - NOW).total_seconds() / 86400
            self.assertEqual(result['status'], 'PRICE CHECK')
            self.assertAlmostEqual(result['quote_summary']['implied_yield_tna'], (6125 / 6105 - 1) * 365 / days * 100)
            self.assertIsNone(result['quote_summary']['caucion_tna'])
            self.assertFalse(result['executable'])
            self.assertFalse(result['live_freshness_established'])
            self.assertEqual(result['assumptions']['price_unit'], 'ARS')
            self.assertEqual(result['timing']['policy'], 'advisory_price_comparison')
            json.dumps(result, allow_nan=False)

    def test_bad_books_do_not_become_price_checks(self):
        cfg = monitor_config.default_watch_config()
        for future in (parse_book({}, receipt=NOW), book(6130, 6125)):
            result = monitor.evaluate(book(6100, 6105), future, cfg, cfg['futures'][0], now=NOW)
            self.assertEqual(result['status'], monitor.UNAVAILABLE)
            self.assertIsNone(result['quote_summary']['implied_yield_tna'])

    def test_manual_rate_constant_and_override(self):
        with patch.object(monitor_config, 'CAUCION_TNA', 32):
            self.assertEqual(monitor_config.default_watch_config()['rates']['borrow_tna'], 32)
            self.assertEqual(monitor_config.default_watch_config(0)['rates']['borrow_tna'], 0)
        for rate in (-1, float('nan'), float('inf'), True):
            with self.assertRaises(ValueError):
                monitor_config.default_watch_config(rate)

    def test_live_cli_reads_env_and_books_without_json_config(self):
        for rate in (None, 30):
            spot = Mock()
            spot.get.return_value = {'bids': [{'price': 6100, 'quantity': 1000}],
                                    'offers': [{'price': 6105, 'quantity': 1000}]}
            primary = Mock()
            cfg = monitor_config.default_watch_config(rate)
            primary.snapshot.side_effect = [
                {'status': 'OK', 'instrumentId': {'marketId': 'ROFX', 'symbol': contract['symbol']},
                 'marketData': {'BI': [{'price': 6125, 'size': 10}], 'OF': [{'price': 6130, 'size': 10}]}}
                for contract in cfg['futures']]
            output = io.StringIO()
            with patch.object(monitor, 'load_watch_config', side_effect=AssertionError('config read')), \
                    patch.object(monitor, 'load_credentials', return_value=Mock(ready=True)) as ppi_env, \
                    patch.object(monitor, 'load_primary_credentials', return_value=Mock(ready=True)) as primary_env, \
                    patch.object(monitor, 'Client', return_value=spot), \
                    patch.object(monitor, 'RemarketsClient', return_value=primary), \
                    patch.object(monitor, 'datetime', wraps=datetime) as clock, redirect_stdout(output):
                clock.now.return_value = NOW
                args = ['watch', '--live', '--iterations', '1']
                if rate is not None:
                    args += ['--caucion-tna', str(rate)]
                self.assertEqual(cli.main(args), 0)
            ppi_env.assert_called_once_with('.env')
            primary_env.assert_called_once_with('.env')
            spot.get.assert_called_once_with('MarketData/Book', {'Ticker': 'GGAL', 'Type': 'ACCIONES', 'Settlement': 'INMEDIATA'})
            self.assertEqual(primary.snapshot.call_count, 2)
            self.assertEqual(len(output.getvalue().splitlines()), 2)
            self.assertIn('remarkets book: bid 6125.00 | ask 6130.00, ppi ggal price: bid 6100.00 | ask 6105.00', output.getvalue())
            self.assertIn('caucion ' + ('n/a' if rate is None else '30.00%'), output.getvalue())
            self.assertIn('PRICE CHECK', output.getvalue())
            spot.close.assert_called_once()
            primary.close.assert_called_once()

    def test_rate_argument_validation(self):
        for args in (['watch', '--live', '--caucion-tna', '-1'],
                     ['watch', '--live', '--caucion-tna', 'nan'],
                     ['watch', '--demo', '--caucion-tna', '30'],
                     ['watch', '--live', '--watch-config', 'example.json', '--caucion-tna', '30'],
                     ['assess', '--caucion-tna', '30']):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.main(args)


if __name__ == '__main__':
    unittest.main()

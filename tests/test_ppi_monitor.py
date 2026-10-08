"""Offline watch tests; transports are synthetic and credentials are never read."""
import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from market_making.ppi import cli, monitor
from market_making.ppi.client import PPIError
from market_making.ppi.models import parse_book
from market_making.ppi.monitor_config import load_watch_config, validate_config

NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)


def config():
    def instrument(ticker, kind):
        return dict(ticker=ticker, type=kind, settlement="INMEDIATA", underlying="GGAL",
                    currency="ARS", price_scale=1, multiplier=1,
                    provenance="Synthetic test assumptions, not market evidence")
    futures = []
    for expiry in ("2026-10", "2026-12"):
        future = instrument("SYNTHETIC-" + expiry, "FUTUROS")
        future.pop("ticker")
        future.update(provider="remarkets", symbol="GGAL/" + ("OCT26" if expiry == "2026-10" else "DIC26"), market_id="ROFX")
        future.pop("type")
        future.pop("settlement")
        future.update(expiry=expiry, maturity=expiry + "-30T12:00:00+00:00", multiplier=10,
                      costs=dict(cash_carry=0.5, reverse=0.5, provenance="Synthetic all-in costs"))
        futures.append(future)
    return dict(spot=instrument("GGAL", "ACCIONES"), futures=futures,
                rates=dict(borrow_tna=36, lend_tna=30, basis=365, provenance="Synthetic TNA assumptions"),
                threshold=0, contracts=1, max_age_seconds=30)


def raw_book(bid=100, ask=101, quantity=1000, date=None):
    return dict(bids=[dict(price=bid, quantity=quantity)], offers=[dict(price=ask, quantity=quantity)],
                date=(date or NOW).isoformat())


def primary_client():
    client = Mock()
    client.snapshot.side_effect = [dict(status="OK", instrumentId={"marketId": "ROFX", "symbol": f["symbol"]},
        marketData={"BI": [{"price": 110 + i * 10, "size": 1000}],
                    "OF": [{"price": 111 + i * 10, "size": 1000}]})
        for i, f in enumerate(config()["futures"])]
    return client


class CalculationTests(unittest.TestCase):
    def test_manual_timestamp_and_last_trade_caveats_both_legs(self):
        cfg = config()
        for leg in (0, 1):
            for date in (None, NOW.replace(tzinfo=None).isoformat(),
                         (NOW - timedelta(seconds=31)).isoformat(),
                         (NOW + timedelta(seconds=3)).isoformat()):
                with self.subTest(leg=leg, date=date):
                    books = [parse_book(raw_book(), receipt=NOW),
                             parse_book(raw_book(110, 111), receipt=NOW)]
                    raw = raw_book(100 if leg == 0 else 110, 101 if leg == 0 else 111)
                    raw['date'] = date
                    raw['volume'] = 0  # traded volume is not quoted depth
                    books[leg] = parse_book(raw, current={'price': 0, 'volume': 0}, receipt=NOW)
                    before = copy.deepcopy(books)
                    strict = monitor.evaluate(*books, cfg, cfg['futures'][0], now=NOW)
                    manual = monitor.evaluate(*books, cfg, cfg['futures'][0], now=NOW, manual_check=True)
                    self.assertEqual(strict['status'], monitor.UNAVAILABLE)
                    self.assertEqual(manual['status'], monitor.EDGE)
                    self.assertIn('invalid_last_trade', manual['caveats'])
                    self.assertTrue(any(x.startswith('source_timestamp_') for x in manual['caveats']))
                    self.assertFalse(manual['blockers'])
                    self.assertTrue(manual['manual_check'])
                    self.assertFalse(manual['live_freshness_established'])
                    self.assertFalse(manual['executable'])
                    self.assertEqual(books, before)

    def test_manual_retains_price_depth_and_input_gates(self):
        cfg = config()
        for changes in ({'offers': []}, {'bids': [{'price': float('inf'), 'quantity': 1}]},
                        {'bids': [{'price': 0, 'quantity': 1}]},
                        {'offers': [{'price': 111, 'quantity': 0}]},
                        {'bids': [{'price': 111, 'quantity': 1}]},
                        {'bids': [{'price': 110, 'quantity': 1}] * 2}):
            raw = {**raw_book(110, 111), **changes, 'date': None}
            result = monitor.evaluate(parse_book(raw_book(), receipt=NOW), parse_book(raw, receipt=NOW),
                                      cfg, cfg['futures'][0], now=NOW, manual_check=True)
            self.assertEqual(result['status'], monitor.UNAVAILABLE)
        for key, value in (('multiplier', 0), ('price_scale', 0), ('maturity', NOW.isoformat()),
                           ('maturity', NOW.replace(tzinfo=None).isoformat())):
            altered = copy.deepcopy(cfg)
            altered['futures'][0][key] = value
            result = monitor.evaluate(parse_book(raw_book(), receipt=NOW), parse_book(raw_book(110, 111), receipt=NOW),
                                      altered, altered['futures'][0], now=NOW, manual_check=True)
            self.assertEqual(result['status'], monitor.UNAVAILABLE)
        result = monitor.evaluate(parse_book(raw_book(), receipt=NOW), parse_book(raw_book(110, 111, quantity=.5), receipt=NOW),
                                  cfg, cfg['futures'][0], now=NOW, manual_check=True)
        self.assertEqual(result['status'], monitor.UNAVAILABLE)

    def test_zero_volume_and_invalid_last_trade_are_separate_from_depth(self):
        cfg = config()
        raw = {**raw_book(110, 111), 'volume': 0}
        spot = parse_book(raw_book(), receipt=NOW)
        future = parse_book(raw, receipt=NOW)
        self.assertEqual(monitor.evaluate(spot, future, cfg, cfg['futures'][0], now=NOW)['status'], monitor.EDGE)
        future = parse_book(raw, current={'price': 0, 'volume': 0}, receipt=NOW)
        self.assertEqual(monitor.evaluate(spot, future, cfg, cfg['futures'][0], now=NOW)['status'], monitor.UNAVAILABLE)
        manual = monitor.evaluate(spot, future, cfg, cfg['futures'][0], now=NOW, manual_check=True)
        self.assertEqual(manual['status'], monitor.EDGE)
        self.assertIn('invalid_last_trade', manual['caveats'])

    def evaluate(self, spot=None, future=None, cfg=None):
        cfg = cfg or config()
        return monitor.evaluate(spot or parse_book(raw_book(), receipt=NOW),
                                future or parse_book(raw_book(110, 111), receipt=NOW),
                                cfg, cfg["futures"][0], now=NOW)

    def test_signed_difference_and_scale(self):
        cfg = config()
        cfg["futures"][0]["price_scale"] = 2
        result = self.evaluate(cfg=cfg)
        cash = result["directions"]["cash_carry"]
        reverse = result["directions"]["reverse"]
        self.assertAlmostEqual(cash["theoretical"], 101 * (1 + .36 * 23 / 365))
        self.assertEqual(cash["observed"], 220)
        self.assertAlmostEqual(cash["difference"], cash["theoretical"] - 220)
        self.assertAlmostEqual(cash["net_edge"], -cash["difference"] - .5)
        self.assertAlmostEqual(reverse["theoretical"], 100 * (1 + .30 * 23 / 365))
        self.assertAlmostEqual(reverse["net_edge"], reverse["difference"] - .5)
        self.assertEqual(result["selected_direction"], "cash_carry")
        self.assertFalse(result["executable"])
        self.assertFalse(result["live_freshness_established"])

    def test_threshold_strictly_greater_and_unknown_costs(self):
        cfg = config()
        cfg["threshold"] = self.evaluate()["directions"]["cash_carry"]["net_edge"]
        self.assertEqual(self.evaluate(cfg=cfg)["status"], monitor.NO_EDGE)
        cfg["futures"][0]["costs"]["cash_carry"] = None
        result = self.evaluate(cfg=cfg)
        self.assertEqual(result["status"], monitor.UNAVAILABLE)
        self.assertIsNone(result["directions"]["cash_carry"]["net_edge"])

    def test_depth_missing_rate_and_horizon(self):
        result = self.evaluate(future=parse_book(raw_book(110, 111, quantity=.5), receipt=NOW))
        self.assertEqual(result["status"], monitor.UNAVAILABLE)
        cfg = config()
        cfg["rates"]["borrow_tna"] = None
        self.assertEqual(self.evaluate(cfg=cfg)["status"], monitor.UNAVAILABLE)
        cfg["futures"][0]["maturity"] = NOW.isoformat()
        self.assertEqual(self.evaluate(cfg=cfg)["status"], monitor.UNAVAILABLE)

    def test_bad_books_block_both_signals(self):
        for date in (NOW - timedelta(seconds=31), NOW + timedelta(seconds=3)):
            result = self.evaluate(future=parse_book(raw_book(110, 111, date=date), receipt=NOW))
            self.assertEqual(result["status"], monitor.UNAVAILABLE)
        for changes in ({"date": None}, {"date": "2026-10-07T12:00:00"},
                        {"offers": []}, {"bids": [{"price": float("nan"), "quantity": 1}]},
                        {"bids": [{"price": 111, "quantity": 1}]},
                        {"bids": [{"price": 110, "quantity": 1}] * 2}):
            raw = raw_book(110, 111)
            raw.update(changes)
            result = self.evaluate(future=parse_book(raw, receipt=NOW))
            self.assertEqual(result["status"], monitor.UNAVAILABLE)
            json.dumps(result, allow_nan=False)

    def test_overflow_is_not_a_signal(self):
        cfg = config()
        cfg["futures"][0]["price_scale"] = 1e308
        result = self.evaluate(cfg=cfg)
        self.assertEqual(result["status"], monitor.UNAVAILABLE)
        json.dumps(result, allow_nan=False)

    def test_highest_eligible_and_deterministic_tie(self):
        cfg = config()
        cfg["rates"].update(borrow_tna=0, lend_tna=1000)
        result = self.evaluate(cfg=cfg)
        self.assertTrue(all(item["eligible"] for item in result["directions"].values()))
        self.assertEqual(result["selected_direction"], "reverse")
        cfg["futures"][0]["costs"]["reverse"] += (
            result["directions"]["reverse"]["net_edge"] - result["directions"]["cash_carry"]["net_edge"])
        self.assertEqual(self.evaluate(cfg=cfg)["selected_direction"], "cash_carry")


class ConfigurationTests(unittest.TestCase):
    def test_valid_explicit_targets(self):
        self.assertEqual(validate_config(config()), config())

    def test_example_fails_closed(self):
        with self.assertRaises(ValueError):
            load_watch_config("config/ppi_ggal_watch.example.json")

    def test_invalid_configs(self):
        cases = []
        for path, value in ((["futures", 0, "expiry"], "2027-10"),
                            (["futures", 0, "maturity"], "2026-10-30T12:00:00"),
                            (["futures", 0, "symbol"], "REPLACE symbol"),
                            (["futures", 0, "multiplier"], 0),
                            (["futures", 0, "price_scale"], float("inf")),
                            (["futures", 0, "costs", "cash_carry"], None),
                            (["spot", "settlement"], "A-24HS"),
                            (["spot", "ticker"], "GGALD"),
                            (["rates", "basis"], True),
                            (["rates", "lend_tna"], -1),
                            (["max_age_seconds"], 31), (["threshold"], -1),
                            (["contracts"], 1.5)):
            cfg = config()
            node = cfg
            for key in path[:-1]:
                node = node[key]
            node[path[-1]] = value
            cases.append(cfg)
        cfg = config()
        cfg["futures"][0]["verified"] = True
        cases.append(cfg)
        cfg = config()
        cfg["futures"][1] = copy.deepcopy(cfg["futures"][0])
        cases.append(cfg)
        for cfg in cases:
            with self.subTest(cfg=cfg), self.assertRaises(ValueError):
                validate_config(cfg)

    def test_strict_json_load(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "watch.json"
            for raw in ('{"threshold":NaN}', '{"threshold":0,"threshold":1}'):
                path.write_text(raw)
                with self.assertRaises(ValueError):
                    load_watch_config(path)


class RunnerTests(unittest.TestCase):
    def test_watch_rejects_each_assessment_option_before_any_work(self):
        with patch.object(cli, 'load_credentials', side_effect=AssertionError('credentials')), \
                patch.object(monitor, 'load_credentials', side_effect=AssertionError('credentials')), \
                patch.object(monitor, 'Client', side_effect=AssertionError('network')), \
                patch.object(monitor, 'watch', side_effect=AssertionError('watch')) as watch:
            for mode in ('--demo', '--live'):
                for manual in ([], ['--manual-check']):
                    for option, value in (('--spot', '100'), ('--tna', '30'),
                                          ('--days', '1'), ('--basis', '365')):
                        with self.subTest(mode=mode, manual=manual, option=option), \
                                redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                            cli.main(['watch', mode, option, value] + manual)
                        self.assertEqual(raised.exception.code, 2)
            watch.assert_not_called()

    def test_manual_does_not_bypass_auth_or_guess_http400(self):
        for failure, reason in (('authentication_missing_or_expired', 'authentication_failure_stop'),
                                ('http_failure_400', 'safe_market_data_failure_stop')):
            output = io.StringIO()
            with patch.object(monitor, 'load_watch_config', return_value=config()), \
                    patch.object(monitor, 'load_credentials', return_value=Mock(ready=True)), \
                    patch.object(monitor, 'load_primary_credentials', return_value=Mock(ready=True)), \
                    patch.object(monitor, 'live_cycle', side_effect=PPIError(failure)) as run, \
                    patch.object(monitor.time, 'sleep') as sleep, redirect_stdout(output):
                code = cli.main(['watch', '--live', '--manual-check', '--watch-config', 'synthetic.json',
                                 '--iterations', '2', '--json'])
            self.assertEqual(code, 2)
            run.assert_called_once()
            self.assertTrue(run.call_args.kwargs['manual_check'])
            sleep.assert_not_called()
            records = [json.loads(x) for x in output.getvalue().splitlines()]
            self.assertTrue(all(r['manual_check'] and reason in r['blockers'] for r in records))

    def test_manual_cli_markers_and_configuration_failure_no_network(self):
        with patch.object(monitor, 'Client', side_effect=AssertionError('network')), \
                patch.object(monitor, 'load_credentials', side_effect=AssertionError('credentials')):
            for extra in ([], ['--json']):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(cli.main(['watch', '--demo', '--manual-check', '--iterations', '1'] + extra), 0)
                    self.assertEqual(cli.main(['watch', '--live', '--manual-check', '--watch-config',
                                               'config/ppi_ggal_watch.example.json', '--iterations', '1'] + extra), 2)
                if extra:
                    records = [json.loads(x) for x in output.getvalue().splitlines()]
                    self.assertTrue(all(r['manual_check'] and not r['executable'] and not r['live_freshness_established'] for r in records))
                else:
                    self.assertEqual(output.getvalue().count('MANUAL-CHECK'), 4)
                    self.assertIn('Fteo=', output.getvalue())
                    self.assertIn('Fobs=', output.getvalue())
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.main(['assess', '--manual-check'])

    def test_manual_batch_age_rechecked_but_advisory(self):
        client = Mock()
        client.get.side_effect = [raw_book(), raw_book(110, 111), raw_book(120, 121)]
        with patch.object(monitor, 'Client', return_value=client), \
                patch.object(monitor, 'RemarketsClient', return_value=primary_client()), \
                patch.object(monitor, 'datetime', wraps=datetime) as clock:
            clock.now.side_effect = [NOW, NOW, NOW, NOW + timedelta(seconds=31)]
            reports = monitor.live_cycle(config(), Mock(ready=True), 1, primary_credentials=Mock(ready=True), manual_check=True)
        self.assertTrue(all(r['status'] == monitor.EDGE for r in reports))
        self.assertTrue(all('source_timestamp_stale_or_future' in r['caveats'] for r in reports))
        client.close.assert_called_once()

    def test_demo_repeated_statuses_both_expiries_zero_network(self):
        output = io.StringIO()
        with patch.object(monitor, "Client", side_effect=AssertionError("network")), \
                patch.object(monitor, "load_credentials", side_effect=AssertionError("credentials")), \
                patch.object(cli, "load_credentials", side_effect=AssertionError("credentials")), redirect_stdout(output):
            code = cli.main(["watch", "--demo", "--interval", "0", "--iterations", "8", "--json"])
        self.assertEqual(code, 0)
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(records), 16)
        for cycle in range(1, 9):
            pair = records[(cycle - 1) * 2:cycle * 2]
            self.assertEqual([r["expiry"] for r in pair], ["2026-10", "2026-12"])
            expected = [monitor.NO_EDGE, monitor.EDGE, monitor.EDGE, monitor.UNAVAILABLE][(cycle - 1) % 4]
            self.assertTrue(all(r["status"] == expected for r in pair))
            self.assertTrue(all(set(r["directions"]) == {"cash_carry", "reverse"} for r in pair))
            if cycle % 4 == 2:
                self.assertTrue(all(r["directions"]["cash_carry"]["difference"] < 0 for r in pair))
            if cycle % 4 == 3:
                self.assertTrue(all(r["directions"]["reverse"]["difference"] > 0 for r in pair))

    def test_cli_opt_in_and_invalid_flags(self):
        for argv in (["watch"], ["watch", "--demo", "--live"],
                     ["watch", "--demo", "--iterations", "0"],
                     ["watch", "--demo", "--interval", "nan"]):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.main(argv)
        with patch.object(monitor, "Client", side_effect=AssertionError), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["watch", "--live", "--watch-config",
                                       "config/ppi_ggal_watch.example.json", "--iterations", "1"]), 2)
            self.assertEqual(cli.main(["watch", "--live", "--interval", "1"]), 2)

    def test_bounded_batch_and_cleanup(self):
        client = Mock()
        client.get.return_value = raw_book()
        primary = primary_client()
        with patch.object(monitor, "Client", return_value=client) as factory, \
                patch.object(monitor, "RemarketsClient", return_value=primary):
            reports = monitor.live_cycle(config(), Mock(ready=True), 1, primary_credentials=Mock(ready=True))
        factory.assert_called_once()
        self.assertEqual(factory.call_args.kwargs, dict(live=True, max_requests=2))
        self.assertEqual(client.get.call_count, 1)
        self.assertEqual(primary.snapshot.call_count, 2)
        primary.close.assert_called_once()
        for call in client.get.call_args_list:
            self.assertEqual(call.args[0], "MarketData/Book")
            self.assertEqual(set(call.args[1]), {"Ticker", "Type", "Settlement"})
        client.close.assert_called_once()
        self.assertTrue(all(r["status"] == monitor.UNAVAILABLE for r in reports))  # old fixture books
        for error in (PPIError("http_failure_401"), KeyboardInterrupt()):
            client.reset_mock()
            client.login.side_effect = error
            with patch.object(monitor, "Client", return_value=client), \
                    patch.object(monitor, "RemarketsClient", return_value=primary_client()), self.assertRaises(type(error)):
                monitor.live_cycle(config(), Mock(ready=True), 1, primary_credentials=Mock(ready=True))
            client.close.assert_called_once()

    def run_live_mock(self, failures, iterations=2):
        output = io.StringIO()
        credentials = Mock(ready=True)
        with patch.object(monitor, "load_watch_config", return_value=config()), \
                patch.object(monitor, "load_credentials", return_value=credentials), \
                patch.object(monitor, "load_primary_credentials", return_value=Mock(ready=True)), \
                patch.object(monitor, "live_cycle", side_effect=failures) as run, \
                patch.object(monitor.time, "sleep") as sleep, redirect_stdout(output):
            code = cli.main(["watch", "--live", "--watch-config", "synthetic.json",
                             "--iterations", str(iterations), "--json"])
        return code, output.getvalue(), run, sleep

    def test_fresh_live_input_batch_is_only_a_proxy(self):
        client = Mock()
        client.get.side_effect = [raw_book(), raw_book(110, 111), raw_book(120, 121)]
        with patch.object(monitor, "Client", return_value=client), \
                patch.object(monitor, "RemarketsClient", return_value=primary_client()), \
                patch.object(monitor, "datetime", wraps=datetime) as clock:
            clock.now.return_value = NOW
            reports = monitor.live_cycle(config(), Mock(ready=True), 1, primary_credentials=Mock(ready=True))
        self.assertEqual([r["expiry"] for r in reports], ["2026-10", "2026-12"])
        self.assertTrue(all(r["status"] == monitor.UNAVAILABLE for r in reports))
        self.assertTrue(all(r["mode"] == "LIVE-PROXY" and not r["executable"]
                            and not r["live_freshness_established"] for r in reports))
        self.assertTrue(all(monitor.UNVERIFIED in r["caveats"] for r in reports))
        client.close.assert_called_once()

    def test_first_book_rechecked_at_batch_completion(self):
        client = Mock()
        client.get.side_effect = [raw_book(), raw_book(110, 111), raw_book(120, 121)]
        with patch.object(monitor, "Client", return_value=client), \
                patch.object(monitor, "RemarketsClient", return_value=primary_client()), \
                patch.object(monitor, "datetime", wraps=datetime) as clock:
            clock.now.side_effect = [NOW, NOW, NOW, NOW + timedelta(seconds=31)]
            reports = monitor.live_cycle(config(), Mock(ready=True), 1, primary_credentials=Mock(ready=True))
        self.assertTrue(all(r["status"] == monitor.UNAVAILABLE for r in reports))
        client.close.assert_called_once()

    def test_auth_stops_and_sanitizes(self):
        code, output, run, sleep = self.run_live_mock([PPIError("http_failure_401")])
        self.assertEqual(code, 2)
        self.assertEqual(run.call_count, 1)
        sleep.assert_not_called()
        self.assertIn("authentication_failure_stop", output)
        code, output, _, _ = self.run_live_mock([PPIError("secret traceback server body")])
        self.assertEqual(code, 2)
        self.assertNotIn("secret", output)
        code, output, _, _ = self.run_live_mock([RuntimeError("secret exception")])
        self.assertEqual(code, 2)
        self.assertNotIn("secret", output)

    def test_transient_waits_regular_interval_and_interrupt(self):
        code, output, run, sleep = self.run_live_mock([PPIError("http_failure_429"), PPIError("http_failure_503")])
        self.assertEqual(code, 0)
        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once_with(30)
        self.assertEqual(len(output.splitlines()), 4)
        self.assertTrue(all(json.loads(line)["status"] == monitor.UNAVAILABLE for line in output.splitlines()))
        code, _, _, _ = self.run_live_mock([KeyboardInterrupt()])
        self.assertEqual(code, 130)


if __name__ == "__main__":
    unittest.main()

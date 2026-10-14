from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
import json
import unittest
from unittest.mock import Mock, patch
from market_making.ppi.cli import main
from market_making.ppi.discovery import discover
from market_making.ppi.client import PPIError


class DiscoveryTests(unittest.TestCase):
    def test_actual_returned_candidates_no_invented_metadata(self):
        client = Mock()
        def get(endpoint, params=None):
            if endpoint.endswith('InstrumentTypes'):
                return ['ACCIONES', 'FUTUROS', 'CAUCIONES']
            if endpoint.endswith('Markets'):
                return ['BYMA', 'ROFEX']
            if endpoint.endswith('Settlements'):
                return ['INMEDIATA', 'A-24HS']
            if endpoint.endswith('SearchInstrument'):
                kind = params['Type']
                if kind == 'ACCIONES':
                    return [{'ticker': 'GGAL', 'type': kind, 'market': 'BYMA', 'currency': 'ARS'},
                            {'ticker': 'GGAL', 'type': kind, 'market': 'NYSE', 'currency': 'USD'}]
                if kind == 'FUTUROS':
                    return []
                return [{'ticker': 'RETURNED-UNKNOWN-TENOR', 'type': kind, 'market': 'BYMA', 'currency': 'ARS'}]
            return {}
        client.get.side_effect = get
        report = discover(client, caucion_ticker='RETURNED-UNKNOWN-TENOR')
        self.assertEqual([x['ticker'] for x in report['candidates']], ['GGAL', 'RETURNED-UNKNOWN-TENOR'])
        self.assertFalse(report['executable'])
        self.assertFalse(report['live_freshness_established'])
        self.assertTrue(all(not x['identity_verified'] for x in report['candidates']))
        self.assertIn('FUTUROS:no_matching_candidates_returned', report['blockers'])

    def test_unavailable_configuration_partial_report(self):
        client = Mock()
        client.get.side_effect = PPIError('http_failure_503')
        report = discover(client)
        self.assertEqual(report['candidates'], [])
        self.assertIn('Settlements:http_failure_503', report['blockers'])

    @staticmethod
    def discovery_client(*, fail_stage=None, rows=None, settlements=None):
        client = Mock()
        def get(endpoint, params=None):
            stage = endpoint.split('/')[-1]
            if stage == 'InstrumentTypes':
                return ['ACCIONES', 'FUTUROS', 'CAUCIONES']
            if stage == 'Markets':
                return ['BYMA']
            if stage == 'Settlements':
                return ['INMEDIATA'] if settlements is None else settlements
            if params['Type'] == 'CAUCIONES' and stage == fail_stage:
                raise PPIError('http_failure_400')
            if stage == 'SearchInstrument':
                kind = params['Type']
                if kind == 'CAUCIONES' and rows is not None:
                    return rows
                return [{'ticker': {'ACCIONES': 'GGAL', 'FUTUROS': 'GGAL-RETURNED',
                                    'CAUCIONES': 'EXPLICIT-CAUCION'}[kind],
                         'type': kind, 'market': 'BYMA', 'currency': 'Pesos'}]
            return {}
        client.get.side_effect = get
        return client

    def test_default_caucion_does_not_send_undocumented_empty_search(self):
        client = self.discovery_client()
        report = discover(client)
        self.assertEqual([row['type'] for row in report['candidates']], ['ACCIONES', 'FUTUROS'])
        self.assertIn('CAUCIONES:broad_search_not_documented_explicit_ticker_required', report['blockers'])
        self.assertFalse(any(call.args[1]['Type'] == 'CAUCIONES' for call in client.get.call_args_list
                             if len(call.args) > 1))

    def test_exact_request_and_failure_stages_preserve_partial_results_no_retry(self):
        for stage in ('SearchInstrument', 'Book', 'Current'):
            with self.subTest(stage=stage):
                client = self.discovery_client(fail_stage=stage)
                report = discover(client, caucion_ticker='EXPLICIT-CAUCION')
                failure, = report['request_failures']
                self.assertEqual(failure['stage'], stage)
                self.assertEqual(failure['category'], 'CAUCIONES')
                self.assertEqual(failure['code'], 'http_failure_400')
                self.assertEqual([row['type'] for row in report['candidates'][:2]], ['ACCIONES', 'FUTUROS'])
                calls = [call for call in client.get.call_args_list
                         if len(call.args) > 1 and call.args[1]['Type'] == 'CAUCIONES']
                self.assertEqual(calls[0].args, ('MarketData/SearchInstrument',
                                                {'Ticker': 'EXPLICIT-CAUCION', 'Type': 'CAUCIONES'}))
                failed = [call for call in calls if call.args[0] == 'MarketData/' + stage]
                self.assertEqual(len(failed), 1)
                if stage != 'SearchInstrument':
                    self.assertEqual(failed[0].args[1], {'Ticker': 'EXPLICIT-CAUCION', 'Type': 'CAUCIONES',
                                                       'Settlement': 'INMEDIATA'})
                    self.assertEqual(failure['settlement_index'], 0)
                    self.assertEqual(failure['candidate_index'], 0)
                    item = report['candidates'][-1]
                    self.assertEqual(len(item['books']), int(stage == 'Current'))
                self.assertFalse(report['executable'])
                self.assertFalse(report['live_freshness_established'])
                self.assertNotIn('funding_rate', report)

    def test_empty_malformed_and_mismatched_caucion_results(self):
        for rows in ([], {}, [None, 'bad', {'ticker': 'OTHER', 'type': 'CAUCIONES',
                                         'currency': 'ARS', 'market': 'BYMA'}]):
            with self.subTest(rows=rows):
                client = self.discovery_client(rows=rows)
                report = discover(client, caucion_ticker='EXPLICIT-CAUCION')
                self.assertEqual(len(report['candidates']), 2)
                self.assertTrue(any('CAUCIONES:' in code for code in report['blockers']))
                self.assertFalse(any(call.args[0] in ('MarketData/Book', 'MarketData/Current') and
                                     call.args[1]['Type'] == 'CAUCIONES' for call in client.get.call_args_list
                                     if len(call.args) > 1))

    def test_caucion_scans_only_configured_bounded_settlements_and_exact_ticker(self):
        client = self.discovery_client(settlements=['INMEDIATA', 'A-24HS', 'A-48HS', 'A-72HS'])
        report = discover(client, caucion_ticker='EXPLICIT-CAUCION')
        item = report['candidates'][-1]
        self.assertEqual([book['settlement'] for book in item['books']], ['INMEDIATA', 'A-24HS', 'A-48HS'])
        self.assertIn('settlement_book_scan_truncated', item['blockers'])
        self.assertFalse(item['identity_verified'])

    def test_unknown_exception_text_withheld_and_invalid_identifier_no_calls(self):
        client = Mock()
        client.get.side_effect = PPIError('secret-server-body')
        report = discover(client)
        self.assertNotIn('secret', json.dumps(report))
        self.assertEqual(report['request_failures'][0]['code'], 'ppi_failure_details_withheld')
        for ticker in ('', ' ', 'x\n', 'x' * 81):
            client = Mock()
            with self.assertRaisesRegex(PPIError, '^invalid_caucion_ticker$'):
                discover(client, caucion_ticker=ticker)
            client.get.assert_not_called()

    def test_cli_explicit_caucion_opt_in_closure_and_watch_rejection(self):
        for stage in ('SearchInstrument', 'Book', 'Current'):
            client = self.discovery_client(fail_stage=stage)
            with patch('market_making.ppi.cli.load_credentials') as load, \
                    patch('market_making.ppi.cli.Client', return_value=client), redirect_stdout(StringIO()) as out:
                load.return_value.ready = True
                self.assertEqual(main(['discover', '--live', '--caucion-ticker', 'EXPLICIT-CAUCION']), 0)
            self.assertEqual(json.loads(out.getvalue())['request_failures'][0]['stage'], stage)
            client.login.assert_called_once()
            client.close.assert_called_once()
        with patch('market_making.ppi.cli.Client') as factory, redirect_stdout(StringIO()):
            self.assertEqual(main(['discover', '--caucion-ticker', 'EXPLICIT-CAUCION']), 0)
            factory.assert_not_called()
        with patch('market_making.ppi.cli.load_credentials') as load, patch('market_making.ppi.monitor.watch') as watch, \
                redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit):
                main(['watch', '--demo', '--caucion-ticker', 'EXPLICIT-CAUCION'])
            load.assert_not_called()
            watch.assert_not_called()

    def test_production_pesos_currency_normalized_without_accepting_other_currencies(self):
        client = Mock()
        def get(endpoint, params=None):
            if endpoint.endswith('InstrumentTypes'):
                return ['ACCIONES']
            if endpoint.endswith('Markets'):
                return ['BYMA']
            if endpoint.endswith('Settlements'):
                return []
            return [{'ticker': 'GGAL', 'type': 'ACCIONES', 'market': 'BYMA', 'currency': currency}
                    for currency in ('Pesos', 'USD', 'UNKNOWN')]
        client.get.side_effect = get
        report = discover(client)
        self.assertEqual(len(report['candidates']), 1)
        self.assertEqual(report['candidates'][0]['currency'], 'ARS')
        self.assertFalse(report['candidates'][0]['identity_verified'])

    def test_no_network_without_live_even_credentials_present(self):
        with patch('market_making.ppi.cli.Client') as client, redirect_stdout(StringIO()) as out:
            self.assertEqual(main(['discover']), 0)
            client.assert_not_called()
        self.assertFalse(json.loads(out.getvalue())['executable'])

    def test_explicit_scenario_and_invalid_partial_inputs(self):
        with redirect_stdout(StringIO()) as out:
            self.assertEqual(main(['assess', '--spot', '100', '--tna', '36.5', '--days', '30', '--basis', '365']), 0)
        scenario = json.loads(out.getvalue())['hypothetical_scenario']
        self.assertAlmostEqual(scenario['F'], 103)
        self.assertTrue(scenario['not_live_or_executable'])
        for arguments in (['--spot', '100'], ['--spot', 'nan', '--tna', '30', '--days', '1', '--basis', '365']):
            with redirect_stdout(StringIO()) as out:
                self.assertEqual(main(['assess'] + arguments), 2)
            self.assertNotIn('hypothetical_scenario', json.loads(out.getvalue()))

    def test_credentials_status_only_named_states(self):
        with patch('market_making.ppi.cli.load_credentials') as load, redirect_stdout(StringIO()) as out:
            load.return_value.statuses.return_value = {'PPI_API_KEY': 'missing'}
            self.assertEqual(main(['credentials-status', '--live']), 0)
        self.assertEqual(json.loads(out.getvalue()), {'PPI_API_KEY': 'missing'})

    def test_finite_scenario_overflows_are_sanitized_nonzero_strict_json(self):
        for spot, tna, days in (('1e308', '100', '365'), ('100', '1e308', '1e308')):
            with self.subTest(spot=spot, tna=tna), redirect_stdout(StringIO()) as out:
                code = main(['assess', '--spot', spot, '--tna', tna, '--days', days, '--basis', '365'])
            self.assertEqual(code, 2)
            report = json.loads(out.getvalue(), parse_constant=lambda value: self.fail('nonstandard JSON: ' + value))
            self.assertIn('invalid_hypothetical_scenario', report['blockers'])
            self.assertNotIn('hypothetical_scenario', report)
            self.assertNotIn('Infinity', out.getvalue())

    def test_json_serialization_defense_withholds_unexpected_nonfinite_output(self):
        with patch('market_making.ppi.cli.fair_future', return_value=float('inf')), redirect_stdout(StringIO()) as out:
            code = main(['assess', '--spot', '100', '--tna', '30', '--days', '30', '--basis', '365'])
        self.assertEqual(code, 2)
        report = json.loads(out.getvalue())
        self.assertEqual(report['blockers'], ['local_configuration_or_processing_failure'])
        self.assertNotIn('Infinity', out.getvalue())

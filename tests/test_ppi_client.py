from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import Mock
from market_making.ppi.config import Credentials, KEYS
from market_making.ppi.client import ALLOWLIST, BASE_URL, Client, PPIError


def response(data=None, status=200, raw=None):
    item = Mock()
    item.status_code = status
    item.iter_content.return_value = [raw if raw is not None else json.dumps(data).encode()]
    return item


def token(expired=False):
    date = datetime.now(timezone.utc) + timedelta(seconds=-60 if expired else 600)
    return {'accessToken': 'secret-token-sentinel', 'expirationDate': date.isoformat()}


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.client = Client(Credentials(dict.fromkeys(KEYS, 'private-sentinel')), live=True, session=self.session)

    def test_forbidden_requests_never_reach_transport(self):
        for endpoint, method in [('Order/Confirm', 'POST'), ('Order/ActiveOrders', 'GET'),
                                 ('Account/Accounts', 'GET'), ('Configuration/Markets', 'POST'),
                                 ('https://evil.test', 'GET'), ('../Order/Confirm', 'GET'),
                                 ('MarketData/Book?secret=yes', 'GET'), ('Account/RefreshToken', 'GET')]:
            with self.subTest(endpoint=endpoint):
                with self.assertRaisesRegex(PPIError, 'forbidden_endpoint'):
                    self.client.request(endpoint, method=method)
        self.session.request.assert_not_called()

    def test_offline_missing_and_unauthenticated_no_requests(self):
        for client, code in [(Client(self.client.credentials, session=self.session), 'live_opt_in'),
                             (Client(Credentials({}), live=True, session=self.session), 'credentials'),
                             (self.client, 'authentication')]:
            with self.assertRaisesRegex(PPIError, code):
                client.get('Configuration/Markets')
        self.session.request.assert_not_called()

    def test_headers_fixed_host_tls_no_redirects_and_no_api_keys_on_reads(self):
        self.session.request.side_effect = [response(token()), response(['BYMA'])]
        self.client.login()
        self.client.get('Configuration/Markets')
        first, second = self.session.request.call_args_list
        self.assertEqual(first.args, ('POST', BASE_URL + 'Account/LoginApi'))
        self.assertEqual(first.kwargs['headers']['ApiKey'], 'private-sentinel')
        self.assertNotIn('ApiKey', second.kwargs['headers'])
        self.assertEqual(second.kwargs['headers']['Authorization'], 'Bearer secret-token-sentinel')
        self.assertTrue(second.kwargs['verify'])
        self.assertFalse(second.kwargs['allow_redirects'])
        self.assertEqual(second.kwargs['timeout'], (5, 10))
        self.assertFalse(self.session.trust_env)

    def test_object_or_singleton_token(self):
        for data in (token(), [token()]):
            self.session.request.return_value = response(data)
            self.client.login()
            self.assertIsNotNone(self.client._token)

    def test_malformed_expired_auth_sanitized(self):
        for data in (token(True), [], [token(), token()], {'accessToken': 'secret'},
                     {'accessToken': 'secret', 'expirationDate': '2027-01-01'}):
            self.session.request.return_value = response(data)
            with self.assertRaisesRegex(PPIError, '^invalid_authentication_response$'):
                self.client.login()
            self.assertIsNone(self.client._token)

    def test_http_redirect_json_and_transport_errors_sanitized(self):
        for item in (response(status=302, raw=b'secret'), response(status=401, raw=b'secret'),
                     response(status=500, raw=b'secret'), response(raw=b'secret-not-json')):
            self.session.request.return_value = item
            with self.assertRaises(PPIError) as caught:
                self.client.login()
            self.assertNotIn('secret', str(caught.exception))
            item.close.assert_called_once()
        self.session.request.side_effect = RuntimeError('secret-exception')
        with self.assertRaisesRegex(PPIError, '^transport_or_json_failure$'):
            self.client.login()

    def test_expiry_and_request_size_budget(self):
        self.session.request.return_value = response(token())
        self.client.login()
        self.client._expiry = datetime.now(timezone.utc) - timedelta(seconds=1)
        with self.assertRaisesRegex(PPIError, 'expired'):
            self.client.get('MarketData/Book')
        self.client.remaining = 0
        with self.assertRaisesRegex(PPIError, 'budget'):
            self.client.login()
        self.client.remaining = 1
        self.session.request.return_value = response(raw=b'x' * 2_000_001)
        with self.assertRaisesRegex(PPIError, 'response_limit'):
            self.client.login()

    def test_allowlist_has_no_execution_routes(self):
        self.assertEqual([x for x, method in ALLOWLIST.items() if method != 'GET'], ['Account/LoginApi', 'Account/RefreshToken'])
        self.assertFalse(any('Order' in x for x in ALLOWLIST))

    def test_caucion_http400_each_stage_no_retry_and_response_closure(self):
        for endpoint, params in (
                ('MarketData/SearchInstrument', {'Ticker': 'EXPLICIT-CAUCION', 'Type': 'CAUCIONES'}),
                ('MarketData/Book', {'Ticker': 'EXPLICIT-CAUCION', 'Type': 'CAUCIONES', 'Settlement': 'INMEDIATA'}),
                ('MarketData/Current', {'Ticker': 'EXPLICIT-CAUCION', 'Type': 'CAUCIONES', 'Settlement': 'INMEDIATA'})):
            with self.subTest(endpoint=endpoint):
                session = Mock()
                failed = response(status=400, raw=b'private-sentinel echoed by server')
                session.request.side_effect = [response(token()), failed]
                client = Client(self.client.credentials, live=True, session=session, max_requests=2)
                try:
                    client.login()
                    with self.assertRaisesRegex(PPIError, '^http_failure_400$'):
                        client.get(endpoint, params)
                    failed.close.assert_called_once()
                    failed.iter_content.assert_not_called()
                    self.assertEqual(session.request.call_count, 2)
                    self.assertEqual(session.request.call_args.kwargs['params'], params)
                    with self.assertRaisesRegex(PPIError, '^request_budget_exhausted$'):
                        client.get(endpoint, params)
                    self.assertEqual(session.request.call_count, 2)
                finally:
                    client.close()
                session.close.assert_called_once()
                self.assertIsNone(client._token)

    def test_forbidden_parameters_and_nested_sensitive_echo_withheld(self):
        with self.assertRaisesRegex(PPIError, 'forbidden_parameters'):
            self.client.get('MarketData/Book', {'ApiKey': 'private-sentinel'})
        self.session.request.assert_not_called()
        with self.assertRaisesRegex(PPIError, 'sensitive_parameters_withheld'):
            self.client.get('MarketData/Book', {'Ticker': 'private-sentinel'})
        self.session.request.side_effect = [response(token()), response({'ticker': 'private-sentinel'}),
                                             response(raw=b'{"nested":["secret-token-\\u0073entinel"]}')]
        self.client.login()
        for _ in range(2):
            with self.assertRaisesRegex(PPIError, '^sensitive_response_withheld$'):
                self.client.get('MarketData/Book')

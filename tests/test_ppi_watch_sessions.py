"""Persistent login, bounded renewal, pooling and cross-provider overlap checks."""
from datetime import datetime, timedelta, timezone
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

from market_making.ppi import client, monitor, remarkets
from market_making.ppi.config import Credentials, KEYS
from test_ppi_monitor import config, raw_book


def response(data=None, status=200, token=None):
    result = Mock()
    result.status_code = status
    result.headers = {"X-Auth-Token": token} if token else {}
    result.iter_content.return_value = [json.dumps(data).encode()]
    return result


def ppi_token(name="access-sentinel", refresh="refresh-sentinel"):
    return {"accessToken": name, "expirationDate": (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat(),
            "refreshToken": refresh}


def future(symbol):
    return {"status": "OK", "instrumentId": {"marketId": "ROFX", "symbol": symbol},
            "marketData": {"BI": [{"price": 110, "size": 100}], "OF": [{"price": 111, "size": 100}]}}


class WatchSessionTests(unittest.TestCase):
    def setUp(self):
        self.ppi_http, self.primary_http = Mock(), Mock()
        self.ppi_credentials = Credentials(dict.fromkeys(KEYS, "private-credential-sentinel"))
        self.primary_credentials = remarkets.Credentials(dict.fromkeys(remarkets.KEYS, "private-primary-sentinel"))
        self.ppi = client.Client(self.ppi_credentials, live=True, session=self.ppi_http)
        self.primary = remarkets.Client(self.primary_credentials, live=True, session=self.primary_http)
        self.addCleanup(self.ppi.close)
        self.addCleanup(self.primary.close)

    def quotes(self, cycles):
        self.ppi_http.request.side_effect = [response(ppi_token())] + [response(raw_book()) for _ in range(cycles)]
        self.primary_http.request.side_effect = [response(token="primary-access-sentinel")] + [
            response(future(contract["symbol"])) for _ in range(cycles) for contract in config()["futures"]]

    def test_two_cycles_use_only_one_login_per_provider(self):
        self.quotes(2)
        session = monitor.WatchSession(self.ppi_credentials, self.primary_credentials)
        with patch.object(monitor, "Client", return_value=self.ppi) as ppi_factory, \
                patch.object(monitor, "RemarketsClient", return_value=self.primary) as primary_factory:
            try:
                first = session.read(config(), 1, True)
                second = session.read(config(), 2, True)
            finally:
                session.close()
        ppi_factory.assert_called_once()
        primary_factory.assert_called_once()
        self.assertEqual(self.ppi_http.request.call_count, 3)
        self.assertEqual(self.primary_http.request.call_count, 5)
        self.assertEqual(sum(c.args[0] == "POST" for c in self.ppi_http.request.call_args_list), 1)
        self.assertEqual(sum(c.args[0] == "POST" for c in self.primary_http.request.call_args_list), 1)
        self.assertEqual(second[0]["cycle"], 2)
        self.assertGreaterEqual(first[0]["acquisition_seconds"], 0)
        self.assertIsNone(self.ppi._refresh_token)
        self.assertIsNone(self.primary._token)

    def test_ppi_renews_before_expiry_with_refresh_endpoint(self):
        self.ppi_http.request.side_effect = [response(ppi_token()), response(ppi_token("renewed-access", "renewed-refresh"))]
        self.ppi.login()
        self.ppi._expiry = datetime.now(timezone.utc) + timedelta(seconds=20)
        self.ppi.begin_watch_cycle()
        self.ppi.ensure_authenticated()
        request = self.ppi_http.request.call_args
        self.assertTrue(request.args[1].endswith("Account/RefreshToken"))
        self.assertEqual(request.kwargs["json"], {"refreshToken": "refresh-sentinel"})
        self.assertNotIn("ApiSecret", request.kwargs["headers"])
        self.assertNotIn("Authorization", request.kwargs["headers"])
        self.assertEqual(self.ppi._token, "renewed-access")
        self.assertEqual(self.ppi._refresh_token, "renewed-refresh")

    def test_refresh_without_refresh_token_uses_login_only_when_needed(self):
        self.ppi_http.request.return_value = response({k: v for k, v in ppi_token().items() if k != "refreshToken"})
        self.ppi.ensure_authenticated()
        self.ppi.ensure_authenticated()
        self.assertEqual(self.ppi_http.request.call_count, 1)
        self.ppi._expiry = datetime.now(timezone.utc)
        self.ppi.begin_watch_cycle()
        self.ppi.ensure_authenticated()
        self.assertEqual(self.ppi_http.request.call_count, 2)

    def test_primary_renews_after_documented_lifetime(self):
        self.primary_http.request.return_value = response(token="primary-access-sentinel")
        self.primary.ensure_authenticated()
        self.primary.ensure_authenticated()
        self.assertEqual(self.primary_http.request.call_count, 1)
        self.primary._renew_at = time.monotonic() - 1
        self.primary.begin_watch_cycle()
        self.primary.ensure_authenticated()
        self.assertEqual(self.primary_http.request.call_count, 2)

    def test_rejected_ppi_access_token_gets_one_refresh_and_retry(self):
        self.ppi_http.request.side_effect = [response(ppi_token()), response(status=401),
                                             response(ppi_token("new-access", "new-refresh")), response(raw_book())]
        self.primary_http.request.side_effect = [response(token="primary-access-sentinel")] + [
            response(future(item["symbol"])) for item in config()["futures"]]
        session = monitor.WatchSession(self.ppi_credentials, self.primary_credentials)
        with patch.object(monitor, "Client", return_value=self.ppi), patch.object(monitor, "RemarketsClient", return_value=self.primary):
            try:
                reports = session.read(config(), 1, True)
            finally:
                session.close()
        self.assertEqual(self.ppi_http.request.call_count, 4)
        self.assertEqual(self.ppi_http.request.call_args.kwargs["headers"]["Authorization"], "Bearer new-access")
        self.assertIsNotNone(reports[0]["books"])

    def test_primary_401_recovers_only_once_per_cycle(self):
        self.ppi_http.request.side_effect = [response(ppi_token()), response(raw_book())]
        self.primary_http.request.side_effect = [response(token="primary-access-sentinel"), response(status=401),
            response(token="new-primary-access"), response(future("GGAL/OCT26")), response(status=401)]
        session = monitor.WatchSession(self.ppi_credentials, self.primary_credentials)
        with patch.object(monitor, "Client", return_value=self.ppi), patch.object(monitor, "RemarketsClient", return_value=self.primary):
            try:
                with self.assertRaisesRegex(client.PPIError, "^http_failure_401$"):
                    session.read(config(), 1)
            finally:
                session.close()
        self.assertEqual(self.primary_http.request.call_count, 5)

    def test_403_does_not_reauthenticate(self):
        self.ppi_http.request.side_effect = [response(ppi_token()), response(status=403)]
        self.primary_http.request.side_effect = [response(token="primary-access-sentinel")] + [
            response(future(item["symbol"])) for item in config()["futures"]]
        session = monitor.WatchSession(self.ppi_credentials, self.primary_credentials)
        with patch.object(monitor, "Client", return_value=self.ppi), patch.object(monitor, "RemarketsClient", return_value=self.primary):
            try:
                with self.assertRaisesRegex(client.PPIError, "^http_failure_403$"):
                    session.read(config(), 1)
            finally:
                session.close()
        self.assertEqual(self.ppi_http.request.call_count, 2)

    def test_throttle_skips_other_future_and_backoff_grows_then_resets(self):
        self.ppi_http.request.side_effect = [response(ppi_token()), response(raw_book())]
        self.primary_http.request.side_effect = [response(token="primary-access-sentinel"), response(status=429)]
        session = monitor.WatchSession(self.ppi_credentials, self.primary_credentials)
        with patch.object(monitor, "Client", return_value=self.ppi), patch.object(monitor, "RemarketsClient", return_value=self.primary):
            try:
                reports = session.read(config(), 1)
            finally:
                session.close()
        self.assertEqual(self.primary_http.request.call_count, 2)
        failures = 0
        for expected in (30, 60, 120, 240, 300, 300):
            delay, failures = monitor.refresh_delay(reports, 1, failures)
            self.assertEqual(delay, expected)
        self.assertEqual(monitor.refresh_delay(monitor.demo_cycle(1), 1, failures), (1, 0))

    def test_provider_reads_overlap_and_client_is_never_used_concurrently(self):
        spot, primary = Mock(), Mock()
        spot.get.return_value = raw_book()
        barrier = threading.Barrier(2)
        def ppi_read(*_):
            barrier.wait(1)
            return raw_book()
        def primary_read(item):
            if item["symbol"] == "GGAL/OCT26":
                barrier.wait(1)
            return future(item["symbol"])
        spot.get.side_effect = ppi_read
        primary.snapshot.side_effect = primary_read
        session = monitor.WatchSession(self.ppi_credentials, self.primary_credentials)
        with patch.object(monitor, "Client", return_value=spot), patch.object(monitor, "RemarketsClient", return_value=primary):
            try:
                reports = session.read(config(), 1)
            finally:
                session.close()
        self.assertEqual(len(reports), 2)
        self.assertEqual(primary.snapshot.call_count, 2)

    def test_refresh_endpoint_cannot_send_arbitrary_payload(self):
        with self.assertRaisesRegex(client.PPIError, "^forbidden_parameters$"):
            self.ppi.request("Account/RefreshToken", method="POST", body={"refreshToken": "invented"})
        self.ppi_http.request.assert_not_called()

    def test_refresh_token_echo_in_quote_is_withheld(self):
        self.ppi_http.request.side_effect = [response(ppi_token()), response({"nested": ["refresh-sentinel"]})]
        self.ppi.login()
        with self.assertRaisesRegex(client.PPIError, "^sensitive_response_withheld$"):
            self.ppi.get("MarketData/Book")

    def test_reused_session_remains_bounded_each_cycle(self):
        self.quotes(1)
        self.ppi.begin_watch_cycle()
        self.ppi.ensure_authenticated()
        self.ppi.remaining = 0
        with self.assertRaisesRegex(client.PPIError, "^request_budget_exhausted$"):
            self.ppi.get("MarketData/Book")
        self.assertEqual(self.ppi_http.request.call_count, 1)
        self.ppi._transport.expired = True
        with self.assertRaisesRegex(client.PPIError, "^response_limit_exceeded$"):
            self.ppi.begin_watch_cycle()

    def test_acquisition_time_counts_toward_interval(self):
        reports = monitor.demo_cycle(1)
        for report in reports:
            report["acquisition_seconds"] = 1.25
        self.assertEqual(monitor.refresh_delay(reports, 5, 0), (3.75, 0))
        self.assertEqual(monitor.refresh_delay(reports, 1, 0), (0, 0))

    def test_cancelled_session_does_not_construct_or_login(self):
        session = monitor.WatchSession(self.ppi_credentials, self.primary_credentials)
        session.cancel()
        with patch.object(monitor, "Client") as factory:
            with self.assertRaisesRegex(client.PPIError, "^response_limit_exceeded$"):
                session.read(config(), 1)
        factory.assert_not_called()
        session.close()


class KeepAliveHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def handle(self):
        try:
            super().handle()
        except OSError:
            pass  # Expected peer shutdown when the test cancels a pooled read.

    def setup(self):
        super().setup()
        self.server.connections += 1

    def do_GET(self):
        self.server.requests += 1
        first = self.server.requests == 1
        data = b"{}" if first else b'{"padding":"' + b'x' * 1000 + b'"}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            if first:
                self.wfile.write(data)
                self.wfile.flush()
            else:
                self.server.slow_seen.set()
                for value in data:
                    self.wfile.write(bytes([value]))
                    self.wfile.flush()
                    time.sleep(.02)
        except OSError:
            pass


class PooledTransportTests(unittest.TestCase):
    def run_pooled(self, provider, cancel=False):
        server = ThreadingHTTPServer(("127.0.0.1", 0), KeepAliveHandler)
        server.daemon_threads = True
        server.connections = server.requests = 0
        server.slow_seen = threading.Event()
        threading.Thread(target=server.serve_forever, daemon=True).start()
        creds = Credentials(dict.fromkeys(KEYS, "fake-ppi-credential")) if provider is client else \
            remarkets.Credentials(dict.fromkeys(remarkets.KEYS, "fake-primary-credential"))
        adapter = provider.Client(creds, live=True)
        adapter._token = "fake-access-token"
        if provider is client:
            adapter._expiry = datetime.now(timezone.utc) + timedelta(minutes=20)
        original = adapter.session.request

        def local_request(method, url, **kwargs):
            self.assertTrue(kwargs["verify"])
            return original(method, f"http://127.0.0.1:{server.server_port}/", **kwargs)

        def read():
            return adapter.get("MarketData/Book") if provider is client else adapter.snapshot(config()["futures"][0])

        errors = []
        try:
            with patch.object(adapter.session, "request", side_effect=local_request):
                adapter.begin_watch_cycle()
                read()  # Fully consume the first response, return its connection to the pool.
                adapter.begin_watch_cycle()
                if cancel:
                    def pending_read():
                        try:
                            read()
                        except client.PPIError as exc:
                            errors.append(str(exc))
                    thread = threading.Thread(target=pending_read, daemon=True)
                    thread.start()
                    self.assertTrue(server.slow_seen.wait(1))
                    started = time.monotonic()
                    adapter.cancel()
                    thread.join(1)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(errors, ["response_limit_exceeded"])
                else:
                    adapter.deadline = time.monotonic() + .2
                    started = time.monotonic()
                    with self.assertRaisesRegex(client.PPIError, "^response_limit_exceeded$"):
                        read()
                self.assertLess(time.monotonic() - started, 1)
                self.assertEqual(server.connections, 1)
                self.assertEqual(server.requests, 2)
                self.assertEqual(len(adapter._transport.sockets), 1)
                with self.assertRaisesRegex(client.PPIError, "^response_limit_exceeded$"):
                    read()
                self.assertEqual(server.requests, 2)
        finally:
            adapter.close()
            server.shutdown()
            server.server_close()

    def test_reused_tcp_connection_is_still_interrupted_at_deadline(self):
        for provider in (client, remarkets):
            with self.subTest(provider=provider.__name__):
                self.run_pooled(provider)

    def test_stop_cancels_reused_tcp_connection(self):
        for provider in (client, remarkets):
            with self.subTest(provider=provider.__name__):
                self.run_pooled(provider, cancel=True)


if __name__ == "__main__":
    unittest.main()

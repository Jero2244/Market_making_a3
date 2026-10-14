"""Local unauthenticated slow-drip regressions (no external network)."""
import gzip
import io
import json
import socketserver
import ssl
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from market_making.ppi import client, monitor, remarkets
from market_making.ppi.config import Credentials, KEYS
from test_ppi_monitor import config
from urllib3.util import connection as urllib3_connection

CERT = Path(__file__).parent / 'fixtures' / 'localhost-test.pem'
KEY = CERT.with_suffix('.key')  # Public test-only key, never production credentials.


class StalledTLS(socketserver.BaseRequestHandler):
    def handle(self):
        self.server.connections += 1
        self.request.settimeout(2)
        try:
            # Consume ClientHello but never send ServerHello. EOF proves the
            # client cancelled the actual connection during SSL wrapping.
            while self.request.recv(65536):
                self.server.hello_seen.set()
            self.server.disconnected.set()
        except OSError:
            pass


class DripHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.request_seen.set()
        self.connection.settimeout(1)
        try:
            if self.path.startswith('/headers'):
                data = b'HTTP/1.1 200 OK\r\nX-Drip: ' + b'x' * 100 + b'\r\nContent-Length: 2\r\n\r\n{}'
                for byte in data:
                    self.connection.sendall(bytes([byte]))
                    time.sleep(.02)
            else:
                body = b'{"padding":"' + b'x' * 10000 + b'"}'
                if self.path.startswith('/gzip'):
                    body = gzip.compress(body)
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                if self.path.startswith('/gzip'):
                    self.send_header('Content-Encoding', 'gzip')
                self.end_headers()
                for byte in body:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    time.sleep(.02)
        except (OSError, ValueError):
            self.server.disconnected.set()


class DeadlineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), DripHandler)
        cls.server.daemon_threads = True
        cls.server.disconnected = threading.Event()
        cls.server.request_seen = threading.Event()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def make_client(self, module):
        if module is client:
            c = client.Client(Credentials(dict(zip(KEYS, ['local-key', 'local-secret', 'local-app', 'local-client']))), live=True)
            c._token = 'local-token'
            c._expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        else:
            c = remarkets.Client(remarkets.Credentials({'PRIMARY_USER': 'local-user', 'PRIMARY_PASSWORD': 'local-password'}), live=True)
            c._token = 'local-token'
        return c

    def test_headers_body_and_compressed_body_interrupt_both_providers(self):
        for module in (client, remarkets):
            for path in ('headers', 'body', 'gzip'):
                with self.subTest(provider=module.__name__, path=path):
                    self.server.disconnected.clear()
                    c = self.make_client(module)
                    original = c.session.request
                    # Only tests rewrite the fixed production URL to local HTTP.
                    def local_request(method, url, **kwargs):
                        kwargs['timeout'] = (.2, .2)
                        return original(method, f'http://127.0.0.1:{self.server.server_port}/{path}', **kwargs)
                    c.deadline = time.monotonic() + .25
                    start = time.monotonic()
                    with patch.object(c.session, 'request', side_effect=local_request) as request:
                        try:
                            with self.assertRaisesRegex(client.PPIError, 'response_limit_exceeded'):
                                if module is client:
                                    c.get('MarketData/Book', {'Ticker': 'GGAL', 'Type': 'ACCIONES', 'Settlement': 'INMEDIATA'})
                                else:
                                    c.snapshot(config()['futures'][0])
                            self.assertLess(time.monotonic() - start, .6)
                            self.assertEqual(request.call_count, 1)
                            self.assertTrue(self.server.disconnected.wait(.6))
                        finally:
                            c.close()
                    self.assertFalse(any(t.name == 'market-data-deadline' for t in threading.enumerate()))

    def test_deadline_failure_closes_both_clients_without_partial_signal(self):
        spot, future = Mock(), Mock()
        spot.get.side_effect = client.PPIError('response_limit_exceeded')
        with patch.object(monitor, 'Client', return_value=spot), \
                patch.object(monitor, 'RemarketsClient', return_value=future), \
                self.assertRaisesRegex(client.PPIError, 'response_limit_exceeded'):
            monitor.live_cycle(config(), Mock(ready=True), 1, primary_credentials=Mock(ready=True))
        spot.get.assert_called_once()
        future.snapshot.assert_not_called()
        spot.close.assert_called_once()
        future.close.assert_called_once()

    def test_watch_deadline_failure_stops_without_retry_or_partial_signals(self):
        from market_making.ppi.cli import main
        output = io.StringIO()
        with patch.object(monitor, 'load_watch_config', return_value=config()), \
                patch.object(monitor, 'load_credentials', return_value=Mock(ready=True)), \
                patch.object(monitor, 'load_primary_credentials', return_value=Mock(ready=True)), \
                patch.object(monitor, 'live_cycle', side_effect=client.PPIError('response_limit_exceeded')) as run, \
                patch.object(monitor.time, 'sleep') as sleep, redirect_stdout(output):
            self.assertEqual(main(['watch', '--live', '--watch-config', 'local.json',
                                   '--iterations', '2', '--json']), 2)
        run.assert_called_once()
        sleep.assert_not_called()
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(records), 2)
        self.assertTrue(all(r['status'] == monitor.UNAVAILABLE and r['selected_direction'] is None
                            and not r['executable'] for r in records))

    def invoke(self, module, c):
        if module is client:
            return c.get('MarketData/Book', {'Ticker': 'GGAL', 'Type': 'ACCIONES', 'Settlement': 'INMEDIATA'})
        return c.snapshot(config()['futures'][0])

    def test_stalled_tls_handshake_after_connection_work_consumes_budget(self):
        for module in (client, remarkets):
            with self.subTest(provider=module.__name__):
                server = socketserver.ThreadingTCPServer(('127.0.0.1', 0), StalledTLS)
                server.daemon_threads = True
                server.connections = 0
                server.hello_seen = threading.Event()
                server.disconnected = threading.Event()
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                c = self.make_client(module)
                original_request = c.session.request
                original_connect = urllib3_connection.create_connection

                def delayed_connect(*args, **kwargs):
                    time.sleep(.5)  # Prior connection work consumes most budget.
                    return original_connect(*args, **kwargs)

                def local_request(method, url, **kwargs):
                    self.assertTrue(kwargs['verify'])
                    # Use the small local CA, as in the verified HTTPS test below.
                    # Loading the platform's full CA bundle can consume the
                    # remaining .3s before ClientHello (not a stalled handshake).
                    # Verification stays enabled; the .8s deadline is unchanged.
                    kwargs['verify'] = str(CERT)
                    return original_request(method, f'https://127.0.0.1:{server.server_address[1]}/', **kwargs)

                try:
                    c.deadline = time.monotonic() + .8
                    start = time.monotonic()
                    with patch.object(c.session, 'request', side_effect=local_request) as request, \
                            patch.object(urllib3_connection, 'create_connection', side_effect=delayed_connect):
                        with self.assertRaisesRegex(client.PPIError, 'response_limit_exceeded'):
                            self.invoke(module, c)
                    self.assertLess(time.monotonic() - start, 1.05)
                    self.assertTrue(server.hello_seen.is_set())
                    self.assertTrue(server.disconnected.wait(.5))
                    self.assertEqual(server.connections, 1)
                    request.assert_called_once()
                    handles = list(c._transport.sockets)
                    self.assertTrue(handles)
                    self.assertTrue(all(sock.fileno() >= 0 for sock in handles))
                    c.close()
                    self.assertTrue(all(sock.fileno() == -1 for sock in handles))
                    self.assertFalse(any(t.name == 'market-data-deadline' for t in threading.enumerate()))
                finally:
                    c.close()
                    server.shutdown()
                    server.server_close()
                    thread.join()

    def test_verified_https_slow_headers_body_and_gzip(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), DripHandler)
        server.daemon_threads = True
        server.disconnected = threading.Event()
        server.request_seen = threading.Event()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(CERT, KEY)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for module in (client, remarkets):
                for path in ('headers', 'body', 'gzip'):
                    with self.subTest(provider=module.__name__, path=path):
                        server.disconnected.clear()
                        server.request_seen.clear()
                        c = self.make_client(module)
                        original = c.session.request

                        def local_request(method, url, **kwargs):
                            self.assertTrue(kwargs['verify'])
                            kwargs['verify'] = str(CERT)  # Explicit local test CA; verification remains enabled.
                            return original(method, f'https://127.0.0.1:{server.server_port}/{path}', **kwargs)

                        try:
                            c.deadline = time.monotonic() + .4
                            start = time.monotonic()
                            with patch.object(c.session, 'request', side_effect=local_request) as request:
                                with self.assertRaisesRegex(client.PPIError, 'response_limit_exceeded'):
                                    self.invoke(module, c)
                            self.assertLess(time.monotonic() - start, .75)
                            self.assertTrue(server.request_seen.is_set())  # Verified handshake completed.
                            self.assertTrue(server.disconnected.wait(.5))
                            request.assert_called_once()
                        finally:
                            handles = list(c._transport.sockets)
                            c.close()
                            self.assertTrue(all(sock.fileno() == -1 for sock in handles))
                        self.assertFalse(any(t.name == 'market-data-deadline' for t in threading.enumerate()))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

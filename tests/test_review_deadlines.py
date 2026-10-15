"""Socket-free tests using real http.client/urllib3 buffering, not yielded chunks."""
import http.client
import io
import socket
import unittest
from decimal import localcontext, ROUND_DOWN, Inexact
from unittest.mock import patch
import requests
from urllib3.response import HTTPResponse as UrllibResponse
from market_making.http_policy import DeadlineAdapter, DeadlineSocket, PrimaryError, response_json
from market_making.execution.execution_state import validate_event


class Clock:
    now = 0
    def __call__(self):
        return self.now


class DripRaw(io.RawIOBase):
    def __init__(self, owner, data, fast_prefix):
        super().__init__()
        self.owner, self.data, self.fast_prefix = owner, data, fast_prefix

    def readable(self):
        return True

    def readinto(self, buffer):
        if not self.data:
            return 0
        if self.fast_prefix:
            count = min(len(buffer), self.fast_prefix)
            self.fast_prefix -= count
        else:
            # A byte every nine seconds defeats a ten-second inactivity timeout.
            wait = min(9, self.owner.timeout)
            self.owner.clock.now += wait
            if wait < 9:
                raise socket.timeout("simulated socket timeout")
            count = 1
        buffer[:count] = self.data[:count]
        self.data = self.data[count:]
        return count


class DripSocket:
    def __init__(self, clock, payload, fast_prefix=0):
        self.clock, self.timeout, self.closed, self.shutdown_called = clock, 10, False, False
        self.reader = DripRaw(self, payload, fast_prefix)
        self.timeouts = []

    def settimeout(self, value):
        self.timeout = value
        self.timeouts.append(value)

    def sendall(self, data):
        pass

    def gettimeout(self):
        return self.timeout

    def makefile(self, mode, buffering):
        return self.reader

    def shutdown(self, how):
        self.shutdown_called = True

    def close(self):
        self.closed = True


class BufferedDeadlineTests(unittest.TestCase):
    def test_requests_adapter_header_acquisition_slow_drip_closes_before_response(self):
        clock = Clock()
        sock = DripSocket(clock, b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n')
        def synthetic_connect(connection):
            connection.sock = sock
            connection.is_verified = True
        # Real requests/urllib3/http.client path, replacing only socket acquisition.
        adapter = DeadlineAdapter(30)
        try:
            with patch("market_making.http_policy.time.monotonic", clock), \
                 patch("urllib3.connection.HTTPSConnection.connect", synthetic_connect):
                with self.assertRaises(requests.RequestException):
                    adapter.send(requests.Request("GET", "https://api.remarkets.primary.com.ar/rest/instruments/all").prepare(),
                                 timeout=(5, 10), stream=True)
        finally:
            adapter.close()
        self.assertEqual(clock.now, 30)
        self.assertTrue(sock.closed)
        self.assertTrue(sock.shutdown_called)
        self.assertTrue(sock.reader.closed)

    def test_actual_buffered_body_slow_drip_ends_at_absolute_deadline(self):
        clock = Clock()
        body = b'{"status":"OK"}'
        headers = b'HTTP/1.1 200 OK\r\nContent-Length: 15\r\n\r\n'
        sock = DripSocket(clock, headers + body, len(headers))
        wire = http.client.HTTPResponse(DeadlineSocket(sock, 30))
        response = requests.Response()
        response._primary_deadline = 30
        with patch("market_making.http_policy.time.monotonic", clock):
            wire.begin()
            response.raw = UrllibResponse(body=wire, original_response=wire, preload_content=False,
                                          headers={"Content-Length": "15"})
            with self.assertRaises(PrimaryError):
                response_json(response)
        self.assertEqual(clock.now, 30)
        self.assertTrue(sock.closed)
        self.assertTrue(sock.shutdown_called)
        self.assertTrue(sock.reader.closed)
        self.assertTrue(response.raw.closed)
        self.assertEqual(sock.timeouts[-1], 3)

    def test_actual_header_line_buffering_slow_drip_ends_and_closes_socket(self):
        clock = Clock()
        sock = DripSocket(clock, b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n')
        wire = http.client.HTTPResponse(DeadlineSocket(sock, 30))
        try:
            with patch("market_making.http_policy.time.monotonic", clock):
                with self.assertRaises(socket.timeout):
                    wire.begin()
        finally:
            wire.close()
        self.assertEqual(clock.now, 30)
        self.assertTrue(sock.closed)
        self.assertTrue(sock.shutdown_called)
        self.assertTrue(sock.reader.closed)

    def test_headers_consume_original_deadline_not_a_new_body_budget(self):
        clock = Clock()
        clock.now = 27
        headers = b'HTTP/1.1 200 OK\r\nContent-Length: 15\r\n\r\n'
        sock = DripSocket(clock, headers + b'{"status":"OK"}', len(headers))
        wire = http.client.HTTPResponse(DeadlineSocket(sock, 30))
        response = requests.Response()
        response._primary_deadline = 30
        with patch("market_making.http_policy.time.monotonic", clock):
            wire.begin()
            response.raw = UrllibResponse(body=wire, original_response=wire, preload_content=False,
                                          headers={"Content-Length": "15"})
            with self.assertRaises(PrimaryError):
                response_json(response)
        self.assertEqual(clock.now, 30)
        self.assertTrue(sock.closed)


class QuantityContextTests(unittest.TestCase):
    def test_high_precision_invariant_independent_of_ambient_context(self):
        base = {"order_key": "fixture", "event_id": "event", "sequence": 1,
                "status": "PARTIALLY_FILLED", "total": "1000000000000000000",
                "cum": "999999999999999999.99999999998", "leaves": "0.00000000002"}
        for precision in (2, 28, 80):
            with self.subTest(precision=precision), localcontext() as ctx:
                ctx.prec, ctx.rounding, ctx.Emin, ctx.Emax = precision, ROUND_DOWN, -1, 1
                ctx.traps[Inexact] = True
                validate_event(base)
                for cum in ("999999999999999999.99999999999", "999999999999999999.99999999997"):
                    with self.assertRaises(PrimaryError):
                        validate_event({**base, "cum": cum})

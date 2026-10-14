"""Interrupt active urllib3 sockets at the batch's monotonic deadline.

Read timeouts measure inactivity, not elapsed time. A separate watchdog shuts
down the socket even while HTTP headers or decompressed body reads are blocked.
The request thread retains ownership of closing responses and connections.
Cancellation owns a duplicate raw socket handle: TLS wrapping detaches the
original Python socket, but the duplicate still shuts down the same connection
throughout the handshake and later encrypted reads.
"""
import socket
import threading
import time
from contextlib import contextmanager

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool


class DeadlineTransport:
    def __init__(self, session):
        self.lock = threading.Lock()
        self.sockets = set()
        self.expired = False
        # Injected test sessions retain their own transport; production sessions
        # always use our socket-registering connection pools.
        if isinstance(session, requests.Session):
            owner = self

            class ConnectionMixin:
                def _new_conn(self):
                    sock = super()._new_conn()
                    owner.register(sock)
                    return sock

            class HTTP(ConnectionMixin, HTTPConnection):
                pass

            class HTTPS(ConnectionMixin, HTTPSConnection):
                pass

            class HTTPPool(HTTPConnectionPool):
                ConnectionCls = HTTP

            class HTTPSPool(HTTPSConnectionPool):
                ConnectionCls = HTTPS

            class Adapter(HTTPAdapter):
                def init_poolmanager(self, *args, **kwargs):
                    super().init_poolmanager(*args, **kwargs)
                    self.poolmanager.pool_classes_by_scheme = {"http": HTTPPool, "https": HTTPSPool}

            session.mount("https://", Adapter(max_retries=0))
            session.mount("http://", Adapter(max_retries=0))

    @staticmethod
    def shutdown(sock):
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def register(self, sock):
        if sock is None:
            return
        # Duplicate BEFORE SSLContext.wrap_socket takes/detaches the raw handle.
        # shutdown() on this owned handle interrupts the shared TCP connection;
        # merely retaining the original socket would leave an invalid fd (-1).
        try:
            cancellation = sock.dup()
        except BaseException:
            sock.close()
            raise
        with self.lock:
            # Each client serializes its requests. A replacement TCP connection
            # supersedes the prior cancellation handle; keep one handle per
            # client rather than leaking duplicates throughout a long watch.
            for previous in self.sockets:
                previous.close()
            self.sockets.clear()
            self.sockets.add(cancellation)
            if self.expired:
                self.shutdown(cancellation)

    def close(self):
        with self.lock:
            sockets, self.sockets = self.sockets, set()
            for sock in sockets:
                sock.close()

    def cancel(self):
        """Interrupt an active read, handshake, or later connection registration."""
        with self.lock:
            self.expired = True
            for sock in self.sockets:
                self.shutdown(sock)

    @contextmanager
    def bound(self, deadline, error):
        stop = threading.Event()

        def expire():
            if not stop.wait(max(0, deadline - time.monotonic())):
                with self.lock:
                    self.expired = True
                    for sock in self.sockets:
                        self.shutdown(sock)

        watchdog = threading.Thread(target=expire, name="market-data-deadline", daemon=True)
        watchdog.start()
        try:
            yield
        finally:
            stop.set()
            watchdog.join()
            if self.expired or time.monotonic() >= deadline:
                raise error("response_limit_exceeded") from None

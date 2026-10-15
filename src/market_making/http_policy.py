"""Fixed REMARKETS transport and bounded, exact JSON decoding. No retries."""
import json
import io
import socket
import time
from decimal import Decimal
import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPSConnection
from urllib3.connectionpool import HTTPSConnectionPool

HOST = "https://api.remarkets.primary.com.ar"
READ_OPERATIONS = frozenset({("POST", "/auth/getToken"),
    ("GET", "/rest/segment/all"), ("GET", "/rest/instruments/all"),
    ("GET", "/rest/instruments/detail"), ("GET", "/rest/marketdata/get")})
ORDER_READ_PATHS = frozenset({"/rest/order/id", "/rest/order/actives", "/rest/order/all"})
MUTATION_PATHS = frozenset({"/rest/order/newSingleOrder", "/rest/order/cancelById"})
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_SECONDS = 30


class DeadlineReader(io.RawIOBase):
    """Enforce the absolute deadline at each raw read, below HTTP buffering."""
    def __init__(self, source, sock, deadline):
        super().__init__()
        self.source, self._sock, self.deadline = source, sock, deadline

    def readable(self):
        return True

    def readinto(self, buffer):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            self.abort()
            raise socket.timeout("Absolute response deadline exceeded.")
        self._sock.settimeout(min(10, remaining))
        try:
            size = self.source.readinto(buffer)
            if time.monotonic() >= self.deadline:
                raise socket.timeout("Absolute response deadline exceeded.")
            return size
        except (OSError, TimeoutError):
            self.abort()
            raise

    def abort(self):
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()

    def close(self):
        if not self.closed:
            self.source.close()
        super().close()


class DeadlineSocket:
    """Socket facade used by http.client for both headers and streamed bodies."""
    def __init__(self, sock, deadline):
        self.sock, self.deadline = sock, deadline

    def __getattr__(self, name):
        return getattr(self.sock, name)

    def makefile(self, mode="rb", buffering=None):
        if mode != "rb":
            raise PrimaryError("Unsupported transport reader.")
        raw = self.sock.makefile("rb", buffering=0)
        return io.BufferedReader(DeadlineReader(raw, self.sock, self.deadline))

    def sendall(self, data, *args, **kwargs):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            self.sock.close()
            raise socket.timeout("Absolute response deadline exceeded.")
        self.sock.settimeout(min(5, remaining))
        self.sock.sendall(data, *args, **kwargs)
        if time.monotonic() >= self.deadline:
            self.sock.close()
            raise socket.timeout("Absolute response deadline exceeded.")


class DeadlineAdapter(HTTPAdapter):
    """Per-request pool, with a deadline established before request acquisition.

    Does not patch global urllib3 pools or rely on yielded application chunks.
    OS DNS resolution is outside Python socket-timeout control (see handoff).
    """
    def __init__(self, deadline):
        self.deadline = deadline
        super().__init__(max_retries=0)

    def init_poolmanager(self, *args, **kwargs):
        deadline = self.deadline
        class Connection(HTTPSConnection):
            def connect(self):
                if time.monotonic() >= deadline:
                    raise socket.timeout("Absolute response deadline exceeded.")
                super().connect()
                if time.monotonic() >= deadline:
                    self.close()
                    raise socket.timeout("Absolute response deadline exceeded.")
                self.sock = DeadlineSocket(self.sock, deadline)
        class Pool(HTTPSConnectionPool):
            ConnectionCls = Connection
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {"https": Pool}


class PrimaryError(RuntimeError):
    """Static, non-sensitive failure message."""


def decimal_json_default(value):
    """Persist exact decoded decimals as strings; never stringify arbitrary objects."""
    if isinstance(value, Decimal) and value.is_finite():
        return str(value)
    raise TypeError("Unsupported evidence value.")


def fixed_request(session, method, path, *, operations, **kwargs):
    if (method, path) not in operations:
        raise PrimaryError("Operation not allowlisted.")
    if set(kwargs) - {"params", "headers"}:
        raise PrimaryError("Transport overrides forbidden.")
    session.trust_env = False
    session.verify = True
    session.proxies = {}
    deadline = time.monotonic() + MAX_RESPONSE_SECONDS
    session.mount("https://", DeadlineAdapter(deadline))
    try:
        response = session.request(method, HOST + path, timeout=(5, 10),
            allow_redirects=False, verify=True, stream=True, **kwargs)
    except requests.RequestException:
        raise PrimaryError("Transport failed; mutation outcome may be uncertain.") from None
    if time.monotonic() >= deadline:
        response.close()
        raise PrimaryError("Absolute response deadline exceeded.")
    response._primary_deadline = deadline
    if not 200 <= response.status_code < 300:
        response.close()
        raise PrimaryError("HTTP response rejected; mutation outcome may be uncertain.")
    return response


def strict_json(raw):
    """Reject ambiguous JSON, excessive structure and non-finite numbers."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def constant(value):
        raise ValueError("nonfinite")

    try:
        if not isinstance(raw, bytes) or len(raw) > MAX_JSON_BYTES:
            raise ValueError("size")
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                           parse_float=Decimal, parse_constant=constant)
        pending = [(value, 0)]
        count = 0
        while pending:
            item, depth = pending.pop()
            count += 1
            if depth > 32 or count > 100000:
                raise ValueError("structure")
            if isinstance(item, Decimal) and (not item.is_finite() or abs(item.adjusted()) > 1000):
                raise ValueError("numeric range")
            children = item.values() if isinstance(item, dict) else item if isinstance(item, list) else ()
            pending.extend((child, depth + 1) for child in children)
        return value
    except (ValueError, TypeError, ArithmeticError, RecursionError, UnicodeError):
        raise PrimaryError("JSON protocol validation failed.") from None


def response_json(response):
    try:
        chunks, size = [], 0
        deadline = getattr(response, "_primary_deadline", None)
        # Real fixed_request responses always carry the original request deadline.
        if not isinstance(deadline, (int, float)):
            deadline = time.monotonic() + MAX_RESPONSE_SECONDS
        for chunk in response.iter_content(chunk_size=65536):
            if time.monotonic() >= deadline:
                raise PrimaryError("Response body deadline exceeded.")
            size += len(chunk)
            if size > MAX_JSON_BYTES:
                raise PrimaryError("JSON response exceeds limit.")
            chunks.append(chunk)
        return strict_json(b"".join(chunks))
    except requests.RequestException:
        raise PrimaryError("Response body transport failed.") from None
    finally:
        response.close()

"""Shared reMarkets WebSocket books; callers fall back to REST when unhealthy.

One login and one connection serve every screen that uses the same Primary
credentials. Each Md frame replaces the entries (BI, OF, LA) it carries for
that symbol. Frames and pongs travel on one ordered connection, so a pong
proves every frame the server sent before it has arrived: an unchanged book
is still current as of the latest pong. That is receipt confirmation, not
exchange freshness.
"""
from datetime import datetime, timezone
import json
import threading
import time

import websocket

from . import remarkets

WS_URL = "wss://api.remarkets.primary.com.ar/"
ENTRIES = ("BI", "OF", "LA")
PING_SECONDS = 2
HEALTHY_SECONDS = 5  # Without a pong or frame for this long, callers use REST.
TOKEN_SECONDS = 23 * 3600


class Stream:
    def __init__(self, credentials, *, client_factory=remarkets.Client,
                 connect=websocket.create_connection, clock=time.monotonic):
        self.credentials = credentials
        self.client_factory = client_factory
        self.connect = connect
        self.clock = clock
        self.condition = threading.Condition()
        self.symbols = set()
        self.pending = set()
        self.books = {}  # symbol -> (marketData entries, frame monotonic, frame wall time)
        self.version = 0
        self.connected = False
        self.confirmed = None  # (monotonic, wall time) of the latest pong or frame.
        self.stopped = threading.Event()
        self.ws = None
        self.client = None
        self.thread = threading.Thread(target=self._run, daemon=True, name="remarkets-stream")

    def start(self):
        self.thread.start()

    def add(self, symbols):
        with self.condition:
            new = set(symbols) - self.symbols
            self.symbols |= new
            self.pending |= new

    def stop(self):
        self.stopped.set()
        with self.condition:
            ws, client = self.ws, self.client
            self.condition.notify_all()
        if client is not None:
            client.cancel()
        if ws is not None:
            try:
                ws.abort()
            except (OSError, websocket.WebSocketException):
                pass

    def healthy(self):
        with self.condition:
            return (self.connected and self.confirmed is not None
                    and self.clock() - self.confirmed[0] <= HEALTHY_SECONDS)

    def latest(self, symbol):
        """(raw snapshot dict, current-as-of monotonic, wall time), or None."""
        with self.condition:
            if not (self.connected and self.confirmed is not None
                    and self.clock() - self.confirmed[0] <= HEALTHY_SECONDS) or symbol not in self.books:
                return None
            entries, received, wall = self.books[symbol]
            if self.confirmed[0] > received:
                received, wall = self.confirmed
            raw = {"status": "OK", "instrumentId": {"marketId": "ROFX", "symbol": symbol},
                   "marketData": dict(entries)}
            return raw, received, wall

    def wait(self, version, timeout):
        """Block until the version differs from `version`, then return the current one."""
        with self.condition:
            if self.version == version and not self.stopped.is_set():
                self.condition.wait(timeout)
            return self.version

    def _mark(self, *, connected=None, books=None, bump=False):
        with self.condition:
            if connected is not None:
                self.connected = connected
            if not self.connected:
                self.books.clear()
                self.confirmed = None
            if books:
                self.books.update(books)
            if bump or connected is not None:
                self.version += 1
                self.condition.notify_all()

    def _subscribe(self, ws, symbols):
        ws.send(json.dumps({"type": "smd", "level": 1, "entries": list(ENTRIES),
                            "products": [{"symbol": s, "marketId": "ROFX"} for s in sorted(symbols)],
                            "depth": remarkets.DEPTH}))

    def _frame(self, data, now, wall):
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        if len(data) > 2_000_000:
            return None
        message = json.loads(data)
        if not isinstance(message, dict) or message.get("type") != "Md":
            return None
        identity = message.get("instrumentId")
        md = message.get("marketData")
        if (not isinstance(identity, dict) or identity.get("marketId") != "ROFX"
                or identity.get("symbol") not in self.symbols or not isinstance(md, dict)):
            return None
        symbol = identity["symbol"]
        with self.condition:
            previous = self.books.get(symbol, ({}, now, wall))[0]
        entries = dict(previous)
        entries.update({key: md[key] for key in ENTRIES if key in md})
        return {symbol: (entries, now, wall)}

    def _session(self):
        self.client = self.client_factory(self.credentials, live=True)
        self.client.begin_watch_cycle(quote_count=1)
        self.client.login()
        token = self.client._token
        issued = self.clock()
        ws = self.connect(WS_URL, header={"X-Auth-Token": token}, timeout=5, redirect_limit=0)
        with self.condition:
            self.ws = ws
            if self.stopped.is_set():
                return
        ws.settimeout(.25)
        with self.condition:
            symbols, self.pending = set(self.symbols), set()
        self._subscribe(ws, symbols)
        last_ping = self.clock()
        alive = self.clock()
        self._mark(connected=True)
        while not self.stopped.is_set():
            now = self.clock()
            if now - issued >= TOKEN_SECONDS or now - alive > HEALTHY_SECONDS + PING_SECONDS:
                return  # Reconnect with a fresh login or after a silent connection.
            with self.condition:
                pending, self.pending = self.pending, set()
            if pending:
                self._subscribe(ws, pending)
            if now - last_ping >= PING_SECONDS:
                ws.ping()
                last_ping = now
            try:
                opcode, data = ws.recv_data(control_frame=True)
            except websocket.WebSocketTimeoutException:
                continue
            now, wall = self.clock(), datetime.now(timezone.utc)
            if opcode == websocket.ABNF.OPCODE_CLOSE:
                return
            if opcode == websocket.ABNF.OPCODE_PONG:
                alive = now
                with self.condition:
                    self.confirmed = (now, wall)
                self._mark(bump=True)
            elif opcode == websocket.ABNF.OPCODE_TEXT:
                books = self._frame(data, now, wall)
                alive = now
                with self.condition:
                    self.confirmed = (now, wall)
                self._mark(books=books, bump=bool(books))

    def _run(self):
        failures = 0
        while not self.stopped.is_set():
            started = self.clock()
            try:
                self._session()
            except Exception:
                # Never surface remote text; callers see an unhealthy stream and use REST.
                pass
            finally:
                self._mark(connected=False)
                with self.condition:
                    ws, client, self.ws, self.client = self.ws, self.client, None, None
                for closer in ((lambda: ws.close(timeout=0)) if ws is not None else None,
                               client.close if client is not None else None):
                    if closer is not None:
                        try:
                            closer()
                        except Exception:
                            pass
            failures = 0 if self.clock() - started > 60 else failures + 1
            self.stopped.wait(min(30, 2 ** min(max(0, failures - 1), 5)))


class Lease:
    """A caller's share of a stream; release() stops it after the last user."""

    def __init__(self, stream, key):
        self.stream = stream
        self._key = key
        self._released = False

    @property
    def version(self):
        return self.stream.version

    def healthy(self):
        return self.stream.healthy()

    def latest(self, symbol):
        return self.stream.latest(symbol)

    def wait(self, version, timeout):
        return self.stream.wait(version, timeout)

    def release(self):
        if self._released:
            return
        self._released = True
        with _lock:
            entry = _streams.get(self._key)
            if entry is None or entry[0] is not self.stream:
                return
            entry[1] -= 1
            if entry[1] > 0:
                return
            del _streams[self._key]
        self.stream.stop()


_lock = threading.Lock()
_streams = {}  # credential values -> [Stream, lease count]


def acquire(credentials, symbols):
    key = tuple(credentials.values.get(k) for k in remarkets.KEYS)
    with _lock:
        entry = _streams.get(key)
        if entry is None:
            entry = _streams[key] = [Stream(credentials), 0]
            entry[0].start()
        entry[1] += 1
        entry[0].add(symbols)
        return Lease(entry[0], key)

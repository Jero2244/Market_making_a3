"""Anonymous A3 viewer feed. No login, account, or order routes are used."""
from datetime import datetime, timezone
from dataclasses import replace
import json
import math
import threading
import time

import requests
import websocket

from market_making.ppi.deadline import DeadlineTransport
from market_making.ppi.models import Level, timestamp
from .models import A3_IDS, DEFAULT_INSTRUMENTS, SYMBOLS, Quote

ORIGIN = "https://visordeprecios.a3mercados.com.ar"
WS_URL = "wss://visordeprecios.a3mercados.com.ar/ws?session_id=&conn_id="
REFRESH_SECONDS = 5


class FeedError(Exception):
    """Fixed, user-facing error text; never a server response or socket URL."""


class TransientFeedError(FeedError):
    pass


def decimal_field(value, *, positive=True):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError
    result = float(value)
    if not math.isfinite(result) or (result <= 0 if positive else result < 0):
        raise ValueError
    return result


def catalog(data):
    if not isinstance(data, dict) or not isinstance(data.get("securities"), list):
        raise FeedError("A3 contract list changed. Restart or update the app.")
    result = {}
    for symbol in SYMBOLS:
        expected = DEFAULT_INSTRUMENTS[symbol]
        matches = [row for row in data["securities"] if isinstance(row, dict)
                   and row.get("symbol") == symbol and row.get("id") == A3_IDS[symbol]]
        if len(matches) != 1:
            raise FeedError(f"A3 could not uniquely match {symbol}.")
        row = matches[0]
        try:
            tick = decimal_field(row["minPriceIncrement"])
            multiplier = decimal_field(row["contractMultiplier"])
            factor = decimal_field(row["factor"])
            if (row.get("currency") != expected.currency or row.get("cfiCode") != "FXXXSX"
                    or row.get("isProduct") is True or factor != 1 or multiplier != expected.multiplier
                    or row.get("maturityDate") != expected.maturity_date):
                raise ValueError
        except (KeyError, TypeError, ValueError, OverflowError):
            raise FeedError("A3 contract units or expiry changed. Check the contract mapping.") from None
        result[symbol] = replace(expected, tick=tick, multiplier=multiplier)
    return result


def _side(price, quantity, name, problems):
    if price in ("", "0") and quantity in ("", "0"):
        return None
    try:
        return Level(decimal_field(price), decimal_field(quantity))
    except (TypeError, ValueError, OverflowError):
        problems.append("invalid_" + name)
        return None


def decode_frame(raw, *, now, receipt=None):
    """Viewer v4.23 M: strings, individually or in JSON string arrays.

    Columns: id, sequence, bid size, bid, ask, ask size, last, LAST TRADE
    timestamp, volume and session statistics. Each M message replaces the
    selected instrument observation, including explicit empty quote fields.
    B: depth frames and global/heartbeat messages are not top-of-book quotes.
    """
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if not isinstance(raw, str) or len(raw) > 2_000_000:
            raise ValueError
        if not raw or raw == "pong":
            return ()
        frames = json.loads(raw) if raw.startswith("[") else [raw]
        if not isinstance(frames, list) or not all(isinstance(frame, str) for frame in frames):
            raise ValueError
        observations = []
        symbols_by_id = {value: key for key, value in A3_IDS.items()}
        for frame in frames:
            if frame.startswith("Q:"):
                raise TransientFeedError("A3 requested a new connection. Reconnecting.")
            if not frame.startswith("M:"):
                continue
            fields = frame[2:].split("|")
            if fields[0] not in symbols_by_id:
                continue
            if len(fields) != 21 or not fields[1].isdigit():
                raise ValueError
            problems = []
            bid = _side(fields[3], fields[2], "bid", problems)
            ask = _side(fields[4], fields[5], "ask", problems)
            if bid and ask and bid.price >= ask.price:
                problems.append("crossed_or_locked_book")
            try:
                last = decimal_field(fields[6]) if fields[6] not in ("", "0") else None
            except (ValueError, OverflowError):
                last = None
            observations.append(Quote(symbols_by_id[fields[0]], "A3", bid, ask, now,
                                      receipt or datetime.now(timezone.utc), last,
                                      timestamp(fields[7]), tuple(problems)))
        return tuple(observations)
    except FeedError:
        raise
    except (ValueError, TypeError, UnicodeError, IndexError, RecursionError):
        raise FeedError("A3 price format changed. Highlights stopped; update the adapter.") from None


def subscription():
    return json.dumps({"_req": "S", "topicType": "md",
                       "topics": ["md." + A3_IDS[symbol] for symbol in SYMBOLS], "replace": True})


class Client:
    def __init__(self, *, session=None, socket_factory=websocket.WebSocket):
        self.session = session if session is not None else requests.Session()
        self.session.trust_env = False
        self.transport = DeadlineTransport(self.session)
        self.socket_factory = socket_factory
        self.ws = None
        self.stopped = threading.Event()
        self.last_transport = None
        self.next_refresh = None

    def _get(self, path):
        response = None
        try:
            with self.transport.bound(time.monotonic() + 15, TransientFeedError):
                response = self.session.get(ORIGIN + path, timeout=(3, 5),
                                            allow_redirects=False, stream=True)
                if response.status_code in (429, 500, 502, 503, 504):
                    raise TransientFeedError("A3 is temporarily unavailable. Retrying.")
                if response.status_code != 200:
                    raise FeedError("A3 public data access failed. Check access or update the app.")
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > 12_000_000:
                        raise FeedError("A3 reference response exceeded the supported size.")
                    chunks.append(chunk)
                return b"".join(chunks)
        except FeedError:
            raise
        except requests.RequestException:
            raise TransientFeedError("A3 connection failed. Retrying.") from None
        finally:
            if response is not None:
                response.close()

    def connect(self):
        self._get("/")
        try:
            instruments = catalog(json.loads(self._get("/api/v2/ref-data")))
        except (ValueError, UnicodeError, RecursionError):
            raise FeedError("A3 reference format changed. Update the adapter.") from None
        if self.stopped.is_set():
            raise TransientFeedError("A3 connection stopped.")
        self.ws = self.socket_factory(enable_multithread=True)
        cookie = "; ".join(f"{c.name}={c.value}" for c in self.session.cookies
                           if c.domain.lstrip(".") == "visordeprecios.a3mercados.com.ar")
        try:
            self.ws.connect(WS_URL, origin=ORIGIN, cookie=cookie or None, timeout=3, redirect_limit=0)
            if self.stopped.is_set():
                self.cancel()
                raise TransientFeedError("A3 connection stopped.")
            self.ws.settimeout(.25)
            self.refresh()
            self.last_transport = time.monotonic()
        except websocket.WebSocketBadStatusException:
            raise FeedError("A3 rejected the price connection. Check access or update the app.") from None
        except (websocket.WebSocketException, OSError):
            raise TransientFeedError("A3 price connection failed. Retrying.") from None
        return instruments

    def refresh(self):
        self.ws.send(subscription())
        self.ws.send("ping")
        self.next_refresh = time.monotonic() + REFRESH_SECONDS

    def read(self):
        try:
            if time.monotonic() >= self.next_refresh:
                self.refresh()
            try:
                raw = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                if time.monotonic() - self.last_transport > 15:
                    raise TransientFeedError("A3 connection stopped responding. Reconnecting.") from None
                return ()
            if raw == "":
                raise TransientFeedError("A3 disconnected. Reconnecting.")
            self.last_transport = time.monotonic()
            return decode_frame(raw, now=self.last_transport)
        except FeedError:
            raise
        except (websocket.WebSocketException, OSError):
            raise TransientFeedError("A3 price connection interrupted. Reconnecting.") from None

    def cancel(self):
        self.stopped.set()
        self.transport.cancel()
        if self.ws is not None:
            try:
                self.ws.abort()
            except (OSError, websocket.WebSocketException):
                pass

    def close(self):
        self.cancel()
        try:
            if self.ws is not None:
                self.ws.close(timeout=0)
        finally:
            self.session.close()
            self.transport.close()

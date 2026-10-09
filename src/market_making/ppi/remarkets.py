"""Read-only, fixed-host REMARKETS simulated futures adapter."""
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

from .client import PPIError
from .config import read_env, status
from .models import Book, Level, number
from .deadline import DeadlineTransport

BASE_URL = "https://api.remarkets.primary.com.ar"
KEYS = ("PRIMARY_USER", "PRIMARY_PASSWORD")
DEPTH = 5


@dataclass(repr=False)
class Credentials:
    values: dict = field(repr=False)

    def __repr__(self):
        return "RemarketsCredentials(<redacted>)"

    @property
    def ready(self):
        return all(status(self.values.get(k)) == "present" and
                   not any(ord(c) < 32 for c in self.values[k]) for k in KEYS)


def load_credentials(path=".env", environ=None):
    local = read_env(path)
    env = os.environ if environ is None else environ
    return Credentials({k: env.get(k, local.get(k, "")) for k in KEYS})


@dataclass(frozen=True)
class RequestIdentity:
    """Trusted caller context, not a field supplied by the remote response."""
    market_id: str
    symbol: str
    depth: int = DEPTH


@dataclass(frozen=True)
class Snapshot:
    raw: object
    request_identity: RequestIdentity


def parse_snapshot(data, instrument, *, receipt=None, max_age=30, depth=DEPTH,
                   request_identity=None):
    """BI/OF only. Numeric timestamps and LA.date are not book timestamps."""
    receipt = receipt or datetime.now(timezone.utc)
    blockers = []
    if isinstance(data, Snapshot):
        request_identity = data.request_identity
        data = data.raw
    expected = {"marketId": instrument["market_id"], "symbol": instrument["symbol"]}
    identity_basis = None
    if request_identity is not None:
        if (not isinstance(request_identity, RequestIdentity) or
                {"marketId": request_identity.market_id, "symbol": request_identity.symbol} != expected or
                type(request_identity.depth) is not int or not 1 <= request_identity.depth <= DEPTH):
            blockers.append("remarkets_request_identity_mismatch")
        else:
            depth = request_identity.depth
            identity_basis = "request-bound"
    if type(depth) is not int or not 1 <= depth <= DEPTH:
        blockers.append("invalid_requested_depth")
        depth = DEPTH
    if not isinstance(data, dict):
        data = {}
        blockers.append("malformed_remarkets_book")
    if data.get("status") != "OK":
        blockers.append("remarkets_status_not_ok")
    if "instrumentId" in data:
        if data["instrumentId"] != expected:
            blockers.append("remarkets_instrument_identity_mismatch")
            identity_basis = None
        else:
            identity_basis = "response-confirmed"
    elif identity_basis is None:
        blockers.append("remarkets_instrument_identity_missing")
    md = data.get("marketData")
    if not isinstance(md, dict):
        md = {}
        blockers.append("malformed_remarkets_market_data")
    levels = []
    normalized = []
    for side, descending in (("BI", True), ("OF", False)):
        best = None
        ordered = ()
        try:
            rows = md[side]
            if not isinstance(rows, list) or not 0 < len(rows) <= depth:
                raise ValueError
            parsed = [Level(number(row["price"]), number(row["size"])) for row in rows]
            if any(row.quantity != int(row.quantity) for row in parsed):
                raise ValueError
            if len({row.price for row in parsed}) != len(parsed):
                raise ValueError
            ordered = tuple(sorted(parsed, key=lambda row: row.price, reverse=descending))
            best = ordered[0]
        except (KeyError, TypeError, ValueError, OverflowError):
            blockers.append("invalid_or_missing_" + side)
        levels.append(best)
        normalized.append(ordered)
    bid, ask = levels
    if bid and ask and bid.price >= ask.price:
        blockers.append("crossed_or_locked_book")
    # No documented book-wide source timestamp semantics for this snapshot.
    # In particular, a last-trade date or numeric epoch must never certify BI/OF.
    blockers.extend(("source_timestamp_missing_or_ambiguous",
                     "source_timestamp_scope_clock_and_session_unverified"))
    return Book(bid, ask, None, None, None, receipt, blockers,
                *normalized, identity_basis, depth)


class Client:
    def __init__(self, credentials, *, live=False, session=None, max_requests=3):
        self.credentials = credentials
        self.live = live
        self.session = session if session is not None else requests.Session()
        self.session.trust_env = False
        self._transport = DeadlineTransport(self.session)
        self._token = None
        self._renew_at = None
        self.remaining = min(max_requests, 3)
        self.deadline = time.monotonic() + 60

    def close(self):
        self._token = None
        self._renew_at = None
        try:
            self.session.close()
        finally:
            self._transport.close()

    def cancel(self):
        self._transport.cancel()

    def begin_watch_cycle(self):
        if self._transport.expired:
            raise PPIError("response_limit_exceeded")
        self.remaining = 5  # Two quotes plus auth and one bounded 401 recovery.
        self.deadline = time.monotonic() + 60

    def ensure_authenticated(self):
        if not self._token or self._renew_at is None or time.monotonic() >= self._renew_at:
            self.login()

    def renew_authentication(self):
        self.login()

    def _request(self, login=False, params=None):
        if not self.live:
            raise PPIError("live_opt_in_required")
        if not self.credentials.ready:
            raise PPIError("credentials_unavailable")
        if not login and not self._token:
            raise PPIError("authentication_missing_or_expired")
        if self._transport.expired:
            raise PPIError("response_limit_exceeded")
        if self.remaining <= 0 or time.monotonic() >= self.deadline:
            raise PPIError("request_budget_exhausted")
        secrets = list(self.credentials.values.values()) + [self._token]
        if params and any(s and s in str(v) for s in secrets for v in params.values()):
            raise PPIError("sensitive_parameters_withheld")
        headers = {"Accept": "application/json"}
        if login:
            headers.update({"X-Username": self.credentials.values[KEYS[0]],
                            "X-Password": self.credentials.values[KEYS[1]]})
        else:
            headers["X-Auth-Token"] = self._token
        self.remaining -= 1
        response = None
        bound = self._transport.bound(self.deadline, PPIError)
        bound.__enter__()
        try:
            remaining = max(.001, self.deadline - time.monotonic())
            response = self.session.request("POST" if login else "GET", BASE_URL +
                ("/auth/getToken" if login else "/rest/marketdata/get"), headers=headers,
                params=params, timeout=(min(5, remaining), min(10, remaining)),
                allow_redirects=False, verify=True, stream=True)
            if response.status_code != 200:
                if response.status_code in (401, 403):
                    self._token = None
                raise PPIError("http_failure_" + str(int(response.status_code)))
            if login:
                token = response.headers.get("X-Auth-Token")
                if not isinstance(token, str) or not token or len(token) > 8192 or any(c.isspace() or ord(c) < 32 for c in token):
                    raise PPIError("invalid_authentication_response")
                self._token = token
                # Primary documents a 24-hour token lifetime; renew one minute early.
                self._renew_at = time.monotonic() + 24 * 3600 - 60
                return None
            size, chunks = 0, []
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > 2_000_000 or time.monotonic() >= self.deadline:
                    raise PPIError("response_limit_exceeded")
                chunks.append(chunk)
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError
                    result[key] = value
                return result
            data = json.loads(b"".join(chunks), object_pairs_hook=unique)
            pending = [data]
            while pending:
                value = pending.pop()
                if isinstance(value, dict):
                    pending.extend(value.keys())
                    pending.extend(value.values())
                elif isinstance(value, list):
                    pending.extend(value)
                elif any(s and s in str(value) for s in secrets):
                    raise PPIError("sensitive_response_withheld")
            return data
        except PPIError:
            raise
        except Exception:
            raise PPIError("transport_or_json_failure") from None
        finally:
            try:
                if response is not None:
                    response.close()
            finally:
                bound.__exit__(None, None, None)

    def login(self):
        self._token = None
        self._renew_at = None
        self._request(login=True)

    def snapshot(self, instrument):
        symbol = instrument.get("symbol")
        if (instrument.get("provider") != "remarkets" or instrument.get("market_id") != "ROFX"
                or not isinstance(symbol, str) or not symbol.strip() or len(symbol) > 200
                or any(ord(c) < 32 for c in symbol)):
            raise PPIError("forbidden_parameters")
        raw = self._request(params={"marketId": "ROFX", "symbol": symbol,
                                    "entries": "BI,OF", "depth": DEPTH})
        return Snapshot(raw, RequestIdentity("ROFX", symbol))

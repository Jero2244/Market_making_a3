"""Reusable read-only REMARKETS market-data lifecycle (no order API).

The consumer factory opens only after authentication and exact catalog validation.
Consumers receive security-filtered recorder-compatible events, never a socket.
"""

from datetime import datetime, timezone
import json
import math
import time

import requests
import websocket

from market_making.market_data.check_connection import (PrimaryError, authenticate, get_json,
                              instrument_id, normalize, require_field, safe_payload)


WS_URL = "wss://api.remarkets.primary.com.ar/"
ENTRIES = ("BI", "OF", "LA", "TV")
TIMESTAMP_KEYS = {"date", "datetime", "timestamp", "transacttime", "servertime", "time"}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def server_timestamps(value, path="", depth=0):
    """Preserve supplied timestamp values verbatim, without inventing an exchange clock."""
    if depth > 12:
        return {}
    found = {}
    if isinstance(value, dict):
        for key, child in value.items():
            location = f"{path}.{key}" if path else key
            if key.lower() in TIMESTAMP_KEYS and isinstance(child, (str, int, float)):
                found[location] = child
            elif isinstance(child, (dict, list)):
                found.update(server_timestamps(child, location, depth + 1))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.update(server_timestamps(child, f"{path}[{index}]", depth + 1))
    return found


def validate_symbol(instruments, market_id, symbol):
    """Require an exact catalog match AND a future in the RFX 20 family."""
    if not isinstance(instruments, list) or not all(isinstance(i, dict) for i in instruments):
        raise PrimaryError("Unexpected instrument catalog format.")
    matches = [i for i in instruments if instrument_id(i) == (market_id, symbol)]
    if len(matches) != 1 or "RFX20" not in normalize(symbol):
        raise PrimaryError("Exact RFX 20 symbol/market pair not uniquely found in catalog.")
    if not str(matches[0].get("cficode", "")).upper().startswith("F"):
        raise PrimaryError("Selected instrument is not catalogued as a future (CFI F...).")


class Freshness:
    """Observation times only. No delta interpretation or carried-forward order book."""

    def __init__(self, threshold):
        self.threshold = threshold
        self.transport = None
        self.instrument = None
        self.sides = {side: None for side in ENTRIES}
        self.last_payload = {side: None for side in ENTRIES}
        self.connected = False

    def reset(self):
        self.connected = True
        self.transport = None
        self.instrument = None
        self.sides = {side: None for side in ENTRIES}
        self.last_payload = {side: None for side in ENTRIES}

    def observe(self, message, now, market_id, symbol):
        self.transport = now
        if not isinstance(message, dict) or message.get("type") != "Md":
            return
        identity = message.get("instrumentId")
        if not isinstance(identity, dict) or (identity.get("marketId"), identity.get("symbol")) != (market_id, symbol):
            return
        self.instrument = now
        data = message.get("marketData")
        if isinstance(data, dict):
            for side in ENTRIES:
                if side in data:
                    self.sides[side] = now
                    self.last_payload[side] = data[side]

    def snapshot(self, now):
        def age(t):
            return None if t is None else round(max(0, now - t), 3)

        def state(t):
            if t is None:
                return "missing"
            return "stale" if now - t > self.threshold else "fresh"

        sides = {}
        for side in ENTRIES:
            status = state(self.sides[side])
            # A missing side is not the same as an explicitly empty side.
            if status == "fresh" and self.last_payload[side] in ([], None):
                status = "empty"
            sides[side] = {"state": status, "age_seconds": age(self.sides[side])}
        return {"connected": self.connected,
                "transport": {"state": state(self.transport) if self.connected else "disconnected",
                              "age_seconds": age(self.transport)},
                "instrument": {"state": state(self.instrument), "age_seconds": age(self.instrument)},
                "entries": sides,
                "book_semantics": "unknown; BI/OF are raw observations, not a reconstructed book"}


def auth_rejected(message):
    if not isinstance(message, dict):
        return False
    if any(message.get(key) in (401, 403, "401", "403") for key in ("status", "code")):
        return True
    if message.get("type") == "Md":
        return False
    text = " ".join(str(message.get(k, "")) for k in ("description", "message", "error", "text"))
    return any(s in text.lower() for s in ("unauthorized", "invalid token", "expired token",
                                            "access denied", "authentication failed", "token expired"))


def subscription_rejected(message):
    if not isinstance(message, dict):
        return False
    status = str(message.get("status", "")).upper()
    kind = str(message.get("type", "")).lower()
    return status in ("ERROR", "REJECTED", "FAIL", "FAILED") or kind in ("error", "err")


def contains_secret(value, secrets):
    """Check *decoded* JSON, including object keys and arrays, before persisting raw text."""
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str) and any(secret and secret in item for secret in secrets):
            return True
        if isinstance(item, dict):
            for key, child in item.items():
                pending.extend((key, child))
        elif isinstance(item, list):
            pending.extend(item)
    return False


def parse_market_frame(raw, secrets=()):
    """One retention rule shared by recorder, fault trigger and evidence reader."""
    duplicate_keys = False

    def pairs(items):
        nonlocal duplicate_keys
        duplicate_keys |= len(dict(items)) != len(items)
        return dict(items)

    def reject_constant(_value):
        # Python's decoder otherwise accepts NaN and +/-Infinity, which are
        # not JSON. Reject the whole frame, including constants nested anywhere.
        raise ValueError("Nonstandard JSON constant")

    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        message = json.loads(text, object_pairs_hook=pairs, parse_constant=reject_constant)
    except (ValueError, TypeError, RecursionError):
        return None, False
    retained = (isinstance(message, dict) and message.get("type") == "Md"
                and isinstance(message.get("instrumentId"), dict)
                and isinstance(message.get("marketData"), dict)
                and not auth_rejected(message)
                and not subscription_rejected(message)
                and not duplicate_keys
                and safe_payload(message, secrets)
                and not contains_secret(message, secrets)
                and not contains_secret(text, secrets))
    return message, retained


class FixedHostWebSocket(websocket.WebSocket):
    """Classify a received non-101 response even if library connect raises.

    websocket-client 1.9.2 raises generic WebSocketException on a redirect with
    redirect_limit=0. Inspect the response, not exception text: network failures
    without a rejected response retain the recorder's bounded retry behavior.
    """

    def connect(self, url, **options):
        try:
            return super().connect(url, **options)
        except websocket.WebSocketException:
            response = getattr(self, "handshake_response", None)
            if response is not None and response.status != 101:
                raise PrimaryError("WebSocket upgrade rejected; stopping without retry") from None
            raise


def connect(token, timeout):
    # Disable redirects before the handshake: never forward a token to a new host.
    # Inspect rejected responses both on the library exception path and on return.
    ws = websocket.create_connection(WS_URL, header={"X-Auth-Token": token},
                                     timeout=timeout, enable_multithread=True,
                                     redirect_limit=0, class_=FixedHostWebSocket)
    if getattr(getattr(ws, "handshake_response", None), "status", None) != 101:
        ws.close()
        raise PrimaryError("WebSocket upgrade rejected (redirect or non-101 response); stopping")
    return ws


def run_session(symbol, market_id, consumer_factory, username, password, *, duration=300, stale_after=20,
        reconnects=5, refresh_after=23 * 3600, interval=5, connect_fn=connect,
        clock=time.monotonic, sleep=time.sleep, shared_secrets=None, depth=2,
        session_factory=requests.Session, authenticate_fn=authenticate,
        get_json_fn=get_json, validate_fn=validate_symbol, utc_fn=utc_now,
        parse_fn=parse_market_frame, timestamps_fn=server_timestamps,
        freshness_factory=Freshness):
    if (any(not math.isfinite(value) or value <= 0
            for value in (duration, stale_after, refresh_after, interval))
            or reconnects < 0 or isinstance(depth, bool) or not isinstance(depth, int)
            or depth not in range(1, 6)):
        raise ValueError("Duration, freshness, renewal and interval must be positive; reconnects nonnegative.")
    # Verify instrument before creating a recording file. Every authentication uses REST.
    with session_factory() as session:
        authenticate_fn(session, username, password)
        token = session.headers["X-Auth-Token"]
        # Keep all validation/preflight and subsequently issued tokens in one
        # registry. Copying here would lose tokens issued by another stage.
        secrets = shared_secrets if shared_secrets is not None else []
        secrets.extend([username, password, token])
        issued_at = clock()
        catalog = get_json_fn(session, "/rest/instruments/all")
        validate_fn(require_field(catalog, "instruments", list), market_id, symbol)
        consumer = consumer_factory()

        class TerminalConsumer:
            def write(self, event):
                try:
                    consumer.write(event)
                except PrimaryError:
                    raise
                except Exception:
                    raise PrimaryError("Event consumer failed; session stopped.") from None

            def close(self):
                consumer.close()

        recorder = TerminalConsumer()
        utc_now = utc_fn
        freshness = freshness_factory(stale_after)
        started = clock()
        deadline = started + duration
        attempts = 0
        try:
            recorder.write({"event": "session", "received_at_utc": utc_now(),
                             "marketId": market_id, "symbol": symbol, "entries": list(ENTRIES),
                             "duration_seconds": duration,
                            "note": "Raw Md frames; book update semantics unverified"})
            while clock() < deadline:
                if clock() - issued_at >= refresh_after:
                    authenticate_fn(session, username, password)
                    token = session.headers["X-Auth-Token"]
                    secrets.append(token)
                    issued_at = clock()
                ws = None
                renewal = False
                try:
                    ws = connect_fn(token, min(5, max(0.001, deadline - clock())))
                    ws.settimeout(min(1.0, interval, max(0.001, deadline - clock())))
                    freshness.reset()
                    recorder.write({"event": "connected", "received_at_utc": utc_now()})
                    ws.send(json.dumps({"type": "smd", "level": 1, "entries": list(ENTRIES),
                                        "products": [{"symbol": symbol, "marketId": market_id}],
                                         "depth": depth}))
                    recorder.write({"event": "subscription_sent", "received_at_utc": utc_now(),
                                    "note": "No acceptance acknowledgement implied"})
                    observed = False
                    last_status = clock()
                    last_ping = clock()
                    connected_at = clock()
                    while clock() < deadline:
                        now = clock()
                        if now - issued_at >= refresh_after:
                            recorder.write({"event": "token_renewal", "received_at_utc": utc_now()})
                            renewal = True
                            break  # reconnect with fresh token, never reuse the old connection
                        if now - last_status >= interval:
                            recorder.write({"event": "freshness", "received_at_utc": utc_now(),
                                            "status": freshness.snapshot(now)})
                            last_status = now
                        if now - last_ping >= interval:
                            ws.ping()
                            last_ping = now
                        if freshness.transport is not None and now - freshness.transport > stale_after:
                            raise ConnectionError("Transport heartbeat stale")
                        if freshness.transport is None and now - connected_at > stale_after:
                            raise ConnectionError("Transport heartbeat missing")
                        try:
                            ws.settimeout(min(1.0, interval, max(0.001, deadline - clock())))
                            opcode, frame = ws.recv_data(control_frame=True)
                        except websocket.WebSocketTimeoutException:
                            continue
                        now = clock()
                        if opcode == websocket.ABNF.OPCODE_CLOSE:
                            raise ConnectionError("WebSocket closed")
                        if opcode == websocket.ABNF.OPCODE_PONG:
                            freshness.transport = now
                            continue
                        if opcode != websocket.ABNF.OPCODE_TEXT:
                            continue
                        message, retained = parse_fn(frame, secrets)
                        # Terminal errors are metadata only, never retained as Md
                        # and never observed by freshness or recovery evidence.
                        if auth_rejected(message):
                            recorder.write({"event": "authentication_rejected", "received_at_utc": utc_now()})
                            raise PrimaryError("WebSocket authentication rejected; stopping (no retry).")
                        if subscription_rejected(message):
                            recorder.write({"event": "subscription_rejected", "received_at_utc": utc_now()})
                            raise PrimaryError("WebSocket subscription/server rejected request; stopping.")
                        if retained:
                            raw = frame.decode("utf-8") if isinstance(frame, bytes) else frame
                            recorder.write({"event": "message", "received_at_utc": utc_now(),
                                            "server_timestamps": timestamps_fn(message),
                                            "raw": raw})
                        else:
                            # Malformed JSON, error/control frames, and ANY decoded Md
                            # containing credentials can echo secrets under arbitrary
                            # JSON escaping. Never persist their raw content or paths.
                            recorder.write({"event": "control_message", "received_at_utc": utc_now(),
                                            "note": "Payload withheld for security"})
                        freshness.observe(message if retained else None, now, market_id, symbol)
                        if not observed and freshness.instrument == now:
                            observed = True
                            recorder.write({"event": "selected_instrument_observed",
                                            "received_at_utc": utc_now()})
                except websocket.WebSocketBadStatusException as exc:
                    if exc.status_code in (401, 403):
                        raise PrimaryError("WebSocket authentication rejected; stopping (no retry).") from None
                    recorder.write({"event": "disconnected", "received_at_utc": utc_now(),
                                    "reason": "HandshakeHTTP", "http_status": exc.status_code})
                except (websocket.WebSocketException, OSError, ConnectionError) as exc:
                    # Do not print exceptions: server URLs / headers may include credentials.
                    if isinstance(exc, PrimaryError):
                        raise
                    recorder.write({"event": "disconnected", "received_at_utc": utc_now(),
                                    "reason": type(exc).__name__})
                finally:
                    freshness.connected = False
                    if ws is not None:
                        try:
                            ws.close()
                        except (websocket.WebSocketException, OSError):
                            pass
                if clock() >= deadline:
                    break
                if renewal:
                    continue
                if attempts >= reconnects:
                    raise PrimaryError("Reconnect budget exhausted; recording stopped.")
                attempts += 1
                wait = min(2 ** (attempts - 1), 30, max(0, deadline - clock()))
                recorder.write({"event": "reconnecting", "received_at_utc": utc_now(),
                                "attempt": attempts, "delay_seconds": wait,
                                "status": freshness.snapshot(clock())})
                sleep(wait)
            recorder.write({"event": "ended", "received_at_utc": utc_now(),
                             "reason": "duration_deadline", "duration_seconds": duration,
                             "elapsed_seconds": clock() - started,
                             "deadline_reached": clock() >= deadline,
                             "status": freshness.snapshot(clock())})
        finally:
            recorder.close()





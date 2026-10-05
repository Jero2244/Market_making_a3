"""Bounded synchronous DEMO order-report collector. Outbound application traffic: os only."""
import json
import math
import time
from datetime import datetime, timezone
import requests
import websocket

from market_making.market_data.check_connection import BASE_URL, PrimaryError
from market_making.market_data.websocket_session import (
    WS_URL, FixedHostWebSocket, auth_rejected, subscription_rejected)
from market_making.execution.order_reports import OrderReports, identifier, strict_json


def validate_bounds(duration, interval, reconnects, refresh_after):
    if (any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
            for v in (duration, interval, refresh_after))
            or not 0 < duration <= 3600 or not 0 < interval <= 30
            or not 0 < refresh_after <= 86400 or isinstance(reconnects, bool)
            or not isinstance(reconnects, int) or not 0 <= reconnects <= 10):
        raise ValueError("Invalid bounded collection configuration")


def authenticate(session, username, password, timeout):
    # No netrc, environment proxies, redirects, catalog, reconciliation or mutation.
    session.trust_env = False
    response = session.post(BASE_URL + "/auth/getToken", timeout=timeout,
                            allow_redirects=False,
                            headers={"X-Username": username, "X-Password": password})
    try:
        if not 200 <= response.status_code < 300:
            raise PrimaryError("Authentication rejected")
        token = response.headers.get("X-Auth-Token")
        if not isinstance(token, str) or not token or "\r" in token or "\n" in token:
            raise PrimaryError("Invalid authentication response")
        return token
    finally:
        response.close()


def connect(token, timeout):
    sock = websocket.create_connection(WS_URL, header={"X-Auth-Token": token},
        timeout=timeout, redirect_limit=0, class_=FixedHostWebSocket,
        http_no_proxy=["*"], http_proxy_host=None, http_proxy_auth=None,
        enable_multithread=True)
    if getattr(getattr(sock, "handshake_response", None), "status", None) != 101:
        sock.close()
        raise PrimaryError("WebSocket upgrade rejected")
    return sock


def run_session(account, consumer, username, password, *, duration=60, interval=1,
                reconnects=2, refresh_after=23 * 3600, connect_fn=connect,
                authenticate_fn=authenticate, session_factory=requests.Session,
                clock=time.monotonic, sleep=time.sleep):
    """Consumer receives allowlisted observations only, never a socket or secrets.

    Return a bounded observational summary; it cannot assert monitoring readiness.
    Any sink failure is terminal and cleanup still runs. Total frames/memory bounded.
    """
    reports = None
    sock = None
    http = None
    sink_failed = False
    def emit(event):
        nonlocal sink_failed
        if sink_failed:
            raise PrimaryError("Evidence sink failed")
        try:
            consumer.write({**event, **reports.snapshot(),
                            "received_at_utc": datetime.now(timezone.utc).isoformat()})
        except Exception:
            sink_failed = True
            raise PrimaryError("Evidence sink failed") from None

    try:
        validate_bounds(duration, interval, reconnects, refresh_after)
        if not identifier(account) or not username or not password:
            raise ValueError("Missing or invalid DEMO configuration")
        secrets = [username, password]
        reports = OrderReports(account, secrets)
        started = clock()
        deadline = started + duration
        http = session_factory()
        http.trust_env = False
        issued = None
        token = None
        attempts = 0
        frames = 0
        connections = 0
        emit({"event": "session", "mode": "read_only_demo_order_reports"})
        while clock() < deadline:
            if issued is None or clock() - issued >= refresh_after:
                token = authenticate_fn(http, username, password, min(5, max(.001, deadline - clock())))
                secrets.append(token)
                issued = clock()
            if clock() >= deadline:
                break
            renewal = False
            try:
                connections += 1
                if connections > 100:
                    reports.gap("CONNECTION_BUDGET_EXHAUSTED")
                    raise PrimaryError("Connection budget exhausted")
                sock = connect_fn(token, min(5, max(.001, deadline - clock())))
                reports.connected()
                emit({"event": "connected"})
                sock.settimeout(min(interval, max(.001, deadline - clock())))
                sock.send(json.dumps({"type": "os", "account": {"id": account}}))
                emit({"event": "subscription_sent"})
                last_ping = clock()
                while clock() < deadline:
                    if clock() - issued >= refresh_after:
                        renewal = True
                        reports.gap("TOKEN_RENEWAL_GAP")
                        emit({"event": "token_renewal"})
                        break
                    if clock() - last_ping >= interval:
                        sock.ping()
                        last_ping = clock()
                    sock.settimeout(min(interval, max(.001, deadline - clock())))
                    try:
                        opcode, frame = sock.recv_data(control_frame=True)
                    except websocket.WebSocketTimeoutException:
                        continue
                    frames += 1
                    if frames > 10000:
                        reports.gap("FRAME_BUDGET_EXHAUSTED")
                        raise PrimaryError("Frame budget exhausted")
                    if opcode == websocket.ABNF.OPCODE_CLOSE:
                        raise ConnectionError()
                    if opcode in (websocket.ABNF.OPCODE_PING, websocket.ABNF.OPCODE_PONG):
                        continue
                    if opcode != websocket.ABNF.OPCODE_TEXT:
                        reports.gap("UNSUPPORTED_FRAME")
                        emit({"event": "payload_withheld"})
                        continue
                    message = strict_json(frame)
                    if message is None:
                        reports.gap("INVALID_JSON")
                        emit({"event": "payload_withheld"})
                        continue
                    # Report envelopes take precedence over heuristics on free
                    # text. Optional envelope metadata is not an auth response
                    # and must never suppress correlated execution evidence.
                    if message.get("type") == "or":
                        event = reports.observe(message)
                        if event is not None:
                            emit(event)
                        continue
                    if auth_rejected(message):
                        reports.gap("AUTHENTICATION_REJECTED")
                        raise PrimaryError("WebSocket authentication rejected")
                    if subscription_rejected(message):
                        reports.gap("SERVER_REJECTED")
                        raise PrimaryError("WebSocket request rejected")
                    event = reports.observe(message)
                    if event is not None:
                        emit(event)
            except websocket.WebSocketBadStatusException:
                reports.gap("UPGRADE_REJECTED")
                raise PrimaryError("WebSocket upgrade rejected") from None
            except (websocket.WebSocketException, OSError, ConnectionError):
                reports.gap("DISCONNECTION_GAP")
                emit({"event": "disconnected"})
            finally:
                socket_close_failed = False
                if sock is not None:
                    try:
                        sock.close()
                    except Exception:
                        reports.gap("SOCKET_CLOSE_FAILED")
                        socket_close_failed = True
                    sock = None
                if not sink_failed:
                    emit({"event": "disconnected", "reason": "socket_closed"})
                if socket_close_failed:
                    raise PrimaryError("Socket cleanup failed") from None
            if clock() >= deadline:
                break
            if renewal:
                continue
            if attempts >= reconnects:
                raise PrimaryError("Reconnect budget exhausted")
            attempts += 1
            emit({"event": "reconnecting", "attempt": attempts})
            sleep(min(2 ** (attempts - 1), 30, max(0, deadline - clock())))
        emit({"event": "ended", "reason": "duration_deadline"})
        return reports.snapshot()
    except KeyboardInterrupt:
        if reports is not None and not sink_failed:
            reports.gap("INTERRUPTED")
            emit({"event": "interrupted"})
        raise
    except Exception:
        if reports is not None and not sink_failed:
            reports.gap("COLLECTION_FAILED")
            emit({"event": "failed"})
        raise PrimaryError("Read-only collection failed; readiness unverified") from None
    finally:
        close_failed = False
        try:
            if http is not None:
                http.close()
        except Exception:
            close_failed = True
        try:
            consumer.close()
        except Exception:
            close_failed = True
        if close_failed:
            raise PrimaryError("Collection cleanup failed") from None

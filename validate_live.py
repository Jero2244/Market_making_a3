"""Explicit opt-in, fixed REMARKETS host, read-only live evidence collection."""

import argparse
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import sys

import requests
import websocket

from check_connection import (BASE_URL, PrimaryError, authenticate, get_json,
                              instrument_id, load_dotenv, require_field, safe_payload)
from stream_market_data import (ENTRIES, connect, run, server_timestamps,
                                auth_rejected, subscription_rejected, parse_market_frame, validate_symbol)

BA = timezone(timedelta(hours=-3), "America/Argentina/Buenos_Aires")
SCHEDULE_SOURCE = "https://a3mercados.com.ar/info-de-mercado/datos-de-mercado"
STAGES = ("credentials", "authentication", "discovery", "contract", "snapshot",
          "session", "stream", "recovery")
STATUSES = {"PASS", "FAIL", "BLOCKED", "NOT_RUN"}


def stage(report, name, status, reason):
    if name not in STAGES or status not in STATUSES:
        raise ValueError("Invalid stage or status")
    report["stages"][name] = {"status": status, "reason": reason}


def write_json(path, value, secrets=()):
    if not safe_payload(value, secrets):
        raise PrimaryError("Artifact withheld: sensitive content detected.")
    with path.open("x", encoding="utf-8") as f:
        json.dump(value, f, indent=2, ensure_ascii=True)
        f.flush()
        os.fsync(f.fileno())


def session_gate(now, duration, evidence):
    """Published production schedule is not proof of a demo trading session.

    Require date-specific operator-reviewed calendar/demo evidence, and report
    this provenance explicitly rather than calling it automatic verification.
    """
    local = now.astimezone(BA)
    if not isinstance(evidence, dict):
        return False, "Calendar and REMARKETS session evidence not supplied"
    if (evidence.get("date") != local.date().isoformat()
            or evidence.get("trading_day") is not True
            or evidence.get("remarkets_session_confirmed") is not True
            or not isinstance(evidence.get("calendar_source"), str)
            or not evidence["calendar_source"].startswith("https://")
            or not isinstance(evidence.get("remarkets_source"), str)
            or not evidence["remarkets_source"].startswith("https://")):
        return False, "Date-specific calendar/demo confirmation missing"
    start = local.replace(hour=10, minute=30, second=0, microsecond=0)
    end = local.replace(hour=17, minute=0, second=0, microsecond=0)
    if local.weekday() >= 5 or not start <= local or local + timedelta(seconds=duration + 5) > end:
        return False, "Outside full-run RFX20 trading window (Buenos Aires 10:30-17:00)"
    return True, "Within published schedule; calendar/demo confirmation operator supplied"


def maturity(instrument):
    value = instrument.get("maturityDate")
    if not isinstance(value, str):
        return None
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


class InducedLocalDisconnect(ConnectionError):
    """Validation-induced local socket close, NOT an exchange/network outage."""


class FaultConnector:
    """One fault only, after selected Md is returned for normal recorder processing."""

    def __init__(self, market_id, symbol, secrets, connect_fn=connect, before_connect=None):
        self.identity = (market_id, symbol)
        self.secrets = secrets
        self.connect_fn = connect_fn
        self.before_connect = before_connect
        self.induced = False
        self.connections = 0

    def __call__(self, token, timeout):
        if self.before_connect is not None:
            self.before_connect(self.connections)
        self.secrets.append(token)
        try:
            real = self.connect_fn(token, timeout)
        except websocket.WebSocketBadStatusException as exc:
            # A bounded demo experiment must not retry a service rejection,
            # rate limit or maintenance response. Transport failures still use
            # the recorder's bounded three-retry budget.
            raise PrimaryError("Demo upgrade rejected; stopping without retry") from None
        self.connections += 1
        owner = self

        class Socket:
            pending_fault = False

            def settimeout(self, value):
                real.settimeout(value)

            def send(self, raw):
                message = json.loads(raw)
                if (message.get("type") != "smd"
                        or message.get("products") != [{"symbol": owner.identity[1],
                                                         "marketId": owner.identity[0]}]
                        or message.get("entries") != list(ENTRIES)
                        or set(message) != {"type", "products", "entries", "level", "depth"}):
                    raise PrimaryError("WebSocket operation not allowlisted")
                return real.send(raw)

            def ping(self):
                return real.ping()

            def close(self):
                return real.close()

            def recv_data(self, control_frame=True):
                if self.pending_fault:
                    owner.induced = True
                    real.close()
                    raise InducedLocalDisconnect("Validation-induced local disconnect")
                opcode, raw = real.recv_data(control_frame=control_frame)
                if opcode == websocket.ABNF.OPCODE_TEXT and not owner.induced:
                    message, retained = parse_market_frame(raw, owner.secrets)
                    if (retained and instrument_id(message) == owner.identity
                            and any(message["marketData"].get(k) not in (None, [], {}) for k in ENTRIES)):
                        self.pending_fault = True
                return opcode, raw

        return Socket()


def timestamp_age(value, received):
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if not math.isfinite(value) or value < 1e9:
                return None
            stamp = datetime.fromtimestamp(value / 1000 if value >= 1e12 else value, timezone.utc)
        elif isinstance(value, str):
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                return None
        else:
            return None
        return round((received - stamp).total_seconds(), 3)
    except (ValueError, OverflowError, OSError):
        return None


def summarize(events, identity, secrets=(), expected_duration=None):
    """Evidence must be in order; a pong/old pre-fault Md cannot satisfy recovery."""
    sequence = []
    availability = {k: {"observations": 0, "nonempty": 0, "empty": 0} for k in ENTRIES}
    stamps = []
    selected = 0
    terminal_rejection = False
    terminal_recorder_error = False
    fault = recovered = False
    recovery_phase = 0
    faults = 0
    for index, event in enumerate(events):
        kind = event.get("event")
        if kind == "error":
            terminal_recorder_error = True
            recovered = False
            break
        if kind in ("authentication_rejected", "subscription_rejected"):
            terminal_rejection = True
            recovered = False
            break
        if kind == "disconnected":
            recovered = False
            if event.get("reason") == "InducedLocalDisconnect":
                faults += 1  # Count even faults before any selected Md.
                fault = bool(selected)
                sequence.append({"line": index + 1, "event": "induced_local_disconnect"})
            recovery_phase = 1 if fault else 0
        elif fault and kind == "reconnecting":
            recovery_phase = 2
            sequence.append({"line": index + 1, "event": kind})
        elif fault and kind == "connected":
            if recovery_phase == 2:
                recovery_phase = 3
                sequence.append({"line": index + 1, "event": kind})
            else:
                recovery_phase = 1  # A prior attempt's retry cannot be reused.
        elif recovery_phase == 3 and kind == "subscription_sent":
            recovery_phase = 4
            sequence.append({"line": index + 1, "event": kind})
        if kind != "message":
            continue
        message, retained = parse_market_frame(event["raw"], secrets)
        if auth_rejected(message) or subscription_rejected(message):
            terminal_rejection = True
            recovered = False
            break
        if not retained or instrument_id(message) != identity:
            continue
        for key in ENTRIES:
            if key in message["marketData"]:
                count = availability[key]
                count["observations"] += 1
                count["empty" if message["marketData"][key] in ([], None, {}) else "nonempty"] += 1
        if not any(message["marketData"].get(k) not in (None, [], {}) for k in ENTRIES):
            continue
        selected += 1
        sequence.append({"line": index + 1, "event": "selected_Md"})
        if fault and recovery_phase == 4:
            recovered = True
        received = datetime.fromisoformat(event["received_at_utc"])
        for path, value in server_timestamps(message).items():
            stamps.append({"line": index + 1, "field": path, "value": value,
                           "age_seconds_at_receipt": timestamp_age(value, received)})
    end = events[-1] if events else {}
    elapsed = end.get("elapsed_seconds")
    sessions = [e for e in events if e.get("event") == "session"]
    session = sessions[0] if len(sessions) == 1 else {}
    session_consistent = bool(events and events[0] == session
                              and (session.get("marketId"), session.get("symbol")) == identity
                              and session.get("duration_seconds") == expected_duration
                              and expected_duration is not None
                              and isinstance(expected_duration, (int, float))
                              and not isinstance(expected_duration, bool)
                              and math.isfinite(expected_duration) and expected_duration > 0)
    completed = (session_consistent and sum(e.get("event") == "ended" for e in events) == 1
                 and end.get("event") == "ended"
                 and end.get("reason") == "duration_deadline"
                 and end.get("deadline_reached") is True
                 and end.get("duration_seconds") == expected_duration
                 and isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool)
                 and math.isfinite(elapsed) and elapsed >= expected_duration)
    return {"selected_messages": selected, "recovered": recovered and faults == 1, "sequence": sequence,
             "induced_local_disconnects": faults,
              "duration_completed": completed,
              "session_duration_evidence_consistent": session_consistent,
             "full_300_seconds_completed": completed and expected_duration == 300,
             "end_evidence": {k: end.get(k) for k in ("event", "reason", "duration_seconds", "elapsed_seconds", "deadline_reached")},
             "requested_entries_nonempty": all(v["nonempty"] > 0 for v in availability.values()),
             "any_requested_entry_nonempty": any(v["nonempty"] > 0 for v in availability.values()),
             "entries_without_nonempty_observation": [k for k, v in availability.items() if not v["nonempty"]],
             "terminal_rejection": terminal_rejection,
             "terminal_recorder_error": terminal_recorder_error,
            "availability": availability, "server_timestamps": stamps,
            "freshness_note": "Ages only for timezone-aware ISO or epoch timestamps; null means unknown."
                              " Last trade age is not book age; negative ages indicate clock skew."
                              " Receipt freshness is not exchange freshness; delta semantics unverified."}


def observation_acceptance(summary, stream_error=None):
    """Transport acceptance is independent of entry completeness/session verification.

    A selected Md must contain at least one nonempty requested entry. Both stream
    and ordered recovery acceptance require duration completion and no terminal
    recorder error. Missing LA or other entries remains a separate limitation.
    """
    stream_error = stream_error or ("RecordedTerminalError" if summary.get("terminal_recorder_error") else None)
    stream_pass = bool(summary["selected_messages"] and summary["any_requested_entry_nonempty"]
                       and summary["duration_completed"] and not summary["terminal_rejection"]
                       and not stream_error)
    return {"stream_pass": stream_pass,
             "recovery_pass": bool(summary["recovered"] and stream_pass),
             "ordered_transport_recovery_observed": summary["recovered"],
            "duration_completed": summary["duration_completed"],
            "full_300_seconds_completed": summary["full_300_seconds_completed"],
            "entry_completeness": {"all_requested_entries_nonempty": summary["requested_entries_nonempty"],
                                   "entries_without_nonempty_observation": summary["entries_without_nonempty_observation"]},
            "terminal_rejection": summary["terminal_rejection"],
            "recorder_error_class": stream_error}


def validate(output, *, live=False, symbol=None, duration=300, session_evidence=None, exploratory_demo=False):
    if not math.isfinite(duration) or not 0 < duration <= 300:
        raise ValueError("Duration must be finite and in (0, 300]")
    output.mkdir(parents=True, exist_ok=False)
    now = datetime.now(timezone.utc)
    report = {"environment": "REMARKETS demo", "base_url": BASE_URL,
               "mode": "exploratory-demo" if exploratory_demo else "strict-session",
               "observation_scope": "Demo transport/data observations only; not market-hours verification" if exploratory_demo else "Operator-confirmed session",
              "started_at_utc": now.isoformat(), "started_at_buenos_aires": now.astimezone(BA).isoformat(),
              "duration_seconds": duration, "fault_kind": "induced local disconnect, not real outage",
              "schedule": {"source": SCHEDULE_SOURCE, "reviewed_on": "2026-10-02",
                           "hours_buenos_aires": "10:30-17:00", "calendar_demo_verified": False},
              "stages": {s: {"status": "NOT_RUN", "reason": "Prerequisite not completed"} for s in STAGES}}
    secrets = [os.getenv("PRIMARY_USER", ""), os.getenv("PRIMARY_PASSWORD", "")]
    active = "credentials"
    try:
        if not live:
            stage(report, "credentials", "NOT_RUN", "Explicit --live opt-in absent; no network calls")
            return report
        if not all(secrets):
            stage(report, "credentials", "BLOCKED", "PRIMARY_USER/PRIMARY_PASSWORD missing")
            return report
        stage(report, "credentials", "PASS", "Required credentials present (values withheld)")
        with requests.Session() as session:
            active = "authentication"
            authenticate(session, *secrets)
            secrets.append(session.headers["X-Auth-Token"])
            stage(report, active, "PASS", "Real fixed-host REST authentication completed")
            active = "discovery"
            segments = get_json(session, "/rest/segment/all")
            catalog = get_json(session, "/rest/instruments/all")
            instruments = require_field(catalog, "instruments", list)
            write_json(output / "discovery.json", {"received_at_utc": datetime.now(timezone.utc).isoformat(),
                                                  "segments": segments, "catalog": catalog}, secrets)
            stage(report, active, "PASS", "Authenticated discovery saved to discovery.json")
            active = "contract"
            candidates = []
            for item in instruments:
                market, candidate = instrument_id(item)
                if market != "ROFX" or (symbol and candidate != symbol):
                    continue
                try:
                    validate_symbol(instruments, market, candidate)
                    candidates.append(candidate)
                except PrimaryError:
                    continue
            chosen = detail = None
            for candidate in sorted(set(candidates))[:5]:
                response = get_json(session, "/rest/instruments/detail", marketId="ROFX", symbol=candidate)
                instrument = require_field(response, "instrument", dict)
                if (instrument_id(instrument) != ("ROFX", candidate)
                        or not str(instrument.get("cficode", "")).upper().startswith("F")):
                    continue
                expiry = maturity(instrument)
                if expiry and expiry >= now.astimezone(BA).date():
                    chosen, detail = candidate, response
                    break
            if chosen is None:
                stage(report, active, "BLOCKED", "No exact eligible unexpired RFX20 future (max 5 detail probes)")
                return report
            report["selected"] = {"marketId": "ROFX", "symbol": chosen}
            write_json(output / "contract.json", detail, secrets)
            stage(report, active, "PASS", "Exact catalog future with known nonexpired maturity selected")
            active = "snapshot"
            snapshot = get_json(session, "/rest/marketdata/get", marketId="ROFX", symbol=chosen,
                                entries=",".join(ENTRIES), depth=2)
            require_field(snapshot, "marketData", dict)
            write_json(output / "snapshot.json", {"received_at_utc": datetime.now(timezone.utc).isoformat(),
                                                 "response": snapshot}, secrets)
            stage(report, active, "PASS", "Real REST snapshot saved; empty entries do not imply liquidity")
        active = "session"
        eligible, reason = session_gate(datetime.now(timezone.utc), duration, session_evidence)
        stage(report, active, "PASS" if eligible else "BLOCKED", reason)
        if not eligible and not exploratory_demo:
            stage(report, "stream", "BLOCKED", "Verified trading-session prerequisite unavailable")
            stage(report, "recovery", "BLOCKED", "No selected live data/fault sequence tested")
            return report
        if exploratory_demo:
            # Never promote strict session acceptance through the experiment.
            stage(report, "session", "BLOCKED", "Exploratory demo observations do not confirm calendar/demo market session")
        else:
            write_json(output / "session_evidence.json", session_evidence, secrets)
            report["schedule"]["calendar_demo_verified"] = "operator-confirmed, not independently verified"
        active = "stream"
        def check_window(connections):
            if exploratory_demo:
                return
            # Recorder repeats REST authentication/discovery before its deadline;
            # recheck after that preflight, not only before it.
            eligible, _ = session_gate(datetime.now(timezone.utc), duration if not connections else 0,
                                       session_evidence)
            if not eligible:
                raise PrimaryError("Trading window no longer eligible")

        connector = FaultConnector("ROFX", chosen, secrets, before_connect=check_window)
        stream_error = None
        try:
            run(chosen, "ROFX", output / "stream.jsonl", secrets[0], secrets[1],
                duration=duration, reconnects=3, connect_fn=connector, shared_secrets=secrets)
        except (PrimaryError, OSError, ValueError) as exc:
            stream_error = type(exc).__name__
        events = []
        if (output / "stream.jsonl").exists():
            events = [json.loads(line) for line in (output / "stream.jsonl").read_text(encoding="utf-8").splitlines()]
        summary = summarize(events, ("ROFX", chosen), secrets, expected_duration=duration)
        write_json(output / "stream_summary.json", summary, secrets)
        acceptance = observation_acceptance(summary, stream_error)
        report["observations"] = acceptance
        stream_pass = acceptance["stream_pass"]
        scope = "Exploratory demo observation: " if exploratory_demo else ""
        stage(report, "stream", "PASS" if stream_pass else
              "FAIL" if stream_error or summary["terminal_rejection"] else "BLOCKED",
              scope + ("Selected Md with at least one nonempty requested entry, duration deadline recorded; entry completeness assessed separately" if stream_pass
                       else "Missing selected/nonempty entries/deadline evidence or terminal failure"))
        recovery_pass = acceptance["recovery_pass"]
        stage(report, "recovery", "PASS" if recovery_pass else
              "FAIL" if connector.induced else "BLOCKED", scope + "Ordered data/fault/retry/connect/resubscribe/new data"
              if recovery_pass else "Ordered recovery observed, but duration incomplete or terminal failure"
              if summary["recovered"] else "Post-reconnect evidence absent or terminal recorder failure")
        return report
    except KeyboardInterrupt:
        stage(report, active, "FAIL", "Operator interrupted validation; no completion implied")
        return report
    except (PrimaryError, requests.RequestException, OSError, ValueError, TypeError) as exc:
        # Never persist exception strings, URLs, headers, or server error payloads.
        stage(report, active, "BLOCKED" if isinstance(exc, (PrimaryError, requests.RequestException))
              else "FAIL", "Stage stopped; sanitized exception class: " + type(exc).__name__)
        return report
    finally:
        report["ended_at_utc"] = datetime.now(timezone.utc).isoformat()
        write_json(output / "report.json", report, secrets)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Explicitly permit read-only demo network calls")
    parser.add_argument("--exploratory-demo", action="store_true", help="Additional opt-in for demo observations without strict session acceptance (exit remains nonzero)")
    parser.add_argument("--output-dir", type=Path, required=True, help="NEW exclusive evidence directory")
    parser.add_argument("--symbol", help="Exact future; absent means deterministic eligible discovery")
    parser.add_argument("--duration", type=float, default=300, help="Stream seconds, maximum 300")
    parser.add_argument("--session-evidence", type=Path, help="Operator-reviewed date/calendar/demo JSON")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--no-env", action="store_true")
    args = parser.parse_args(argv)
    try:
        if not args.no_env:
            load_dotenv(args.env_file)
        evidence = json.loads(args.session_evidence.read_text(encoding="utf-8")) if args.session_evidence else None
        report = validate(args.output_dir, live=args.live, symbol=args.symbol,
                           duration=args.duration, session_evidence=evidence, exploratory_demo=args.exploratory_demo)
        for name, result in report["stages"].items():
            print(f"{name}: {result['status']}")
        return 0 if all(s["status"] == "PASS" for s in report["stages"].values()) else 2
    except (OSError, ValueError, PrimaryError):
        print("Validation stopped; input/artifact error (details withheld).", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

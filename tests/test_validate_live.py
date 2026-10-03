import json
import os
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

import websocket

import check_connection as rest
import stream_market_data as md
import validate_live as live
from test_stream_market_data import Clock, Socket, SYMBOL


IDENTITY = ("ROFX", SYMBOL)
CATALOG = [{"instrumentId": {"marketId": "ROFX", "symbol": SYMBOL}, "cficode": "FXXXSX"}]


def message(symbol=SYMBOL, data=None):
    return json.dumps({"type": "Md", "instrumentId": {"marketId": "ROFX", "symbol": symbol},
                       "marketData": data if data is not None else {"BI": [], "TV": 0}})


def event(raw=None, **kwargs):
    return {"received_at_utc": "2026-10-02T15:00:00+00:00", **kwargs,
            **({"event": "message", "raw": raw} if raw is not None else {})}


class LiveTests(unittest.TestCase):
    def validator_stream(self, folder, frame_sets, *, exploratory=False, duration=12,
                         connect_fn=None, omit_end=False):
        """Exercise full validator + actual recorder/wrapper with offline sockets."""
        clock = Clock()
        sockets = [Socket(clock, [raw if isinstance(raw, tuple) else (websocket.ABNF.OPCODE_TEXT, raw) for raw in frames])
                   for frames in frame_sets]
        tokens = iter(["initial-issued-secret-A", "recorder-issued-secret-B"])

        def login(session, username, password):
            session.headers["X-Auth-Token"] = next(tokens)

        def payload(session, path, **params):
            if path == "/rest/segment/all":
                return {"status": "OK", "segments": []}
            if path == "/rest/instruments/all":
                return {"status": "OK", "instruments": CATALOG}
            if path == "/rest/instruments/detail":
                return {"status": "OK", "instrument": {**CATALOG[0], "maturityDate": "20991231"}}
            return {"status": "OK", "marketData": {"BI": []}}

        connector_type = live.FaultConnector
        recorder_run = md.run
        created = []

        def connector(*args, **kwargs):
            instance = connector_type(*args, **kwargs, connect_fn=connect_fn or (lambda t, s: sockets.pop(0)))
            created.append(instance)
            return instance

        def record(*args, **kwargs):
            recorder_run(*args, **kwargs, clock=clock, sleep=clock.sleep)
            if omit_end:
                path = args[2]
                rows = path.read_text().splitlines()
                path.write_text("\n".join(rows[:-1]) + "\n")

        with patch.dict(os.environ, {"PRIMARY_USER": "integration-secret-user",
                                     "PRIMARY_PASSWORD": "integration-secret-password"}), \
                patch.object(live, "authenticate", side_effect=login), \
                patch.object(md, "authenticate", side_effect=login), \
                patch.object(live, "get_json", side_effect=payload), \
                patch.object(md, "get_json", side_effect=payload), \
                patch.object(live, "session_gate", return_value=(True, "Offline verified-session fixture")), \
                 patch.object(live, "FaultConnector", side_effect=connector), \
                 patch.object(live, "run", side_effect=record):
            report = live.validate(folder, live=True, duration=duration, session_evidence={"offline_fixture": True}, exploratory_demo=exploratory)
        return report, created[0]

    def test_full_validator_initial_token_echo_withheld_plain_and_escaped_nested(self):
        token_a = "initial-issued-secret-A"
        plain = message(data={"BI": [], "nested": [{token_a: [token_a]}]})
        escaped = plain.replace(token_a, "".join("\\u%04x" % ord(c) for c in token_a))
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "validation"
            full = message(data={"BI": [{"price": 1}], "OF": [{"price": 2}], "LA": {"price": 1}, "TV": 0})
            report, connector = self.validator_stream(output, [[plain, escaped, message()], [full]])
            self.assertEqual(report["stages"]["recovery"]["status"], "PASS")
            self.assertTrue(connector.induced)
            self.assertIn(token_a, connector.secrets)
            self.assertIn("recorder-issued-secret-B", connector.secrets)
            events = [json.loads(line) for line in (output / "stream.jsonl").read_text().splitlines()]
            self.assertEqual(sum(e["event"] == "control_message" for e in events), 2)
            self.assertEqual(sum(e["event"] == "message" for e in events), 2)
            summary = json.loads((output / "stream_summary.json").read_text())
            first_data_line = summary["sequence"][0]["line"]
            self.assertTrue(all(e["event"] != "disconnected" for e in events[:first_data_line]))
            for path in output.iterdir():
                text = path.read_text()
                self.assertNotIn(token_a, text)
                self.assertNotIn("recorder-issued-secret-B", text)
                # Decode retained raw frames to detect unicode-escaped leaks too.
                if path.suffix == ".jsonl":
                    for e in events:
                        if "raw" in e:
                            self.assertTrue(rest.safe_payload(json.loads(e["raw"]), connector.secrets))

    def test_full_validator_reconnected_auth_md_never_passes_recovery(self):
        for status in (401, 403, "401", "403"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as folder:
                rejected = json.loads(message())
                rejected["status"] = status
                output = Path(folder) / "validation"
                report, connector = self.validator_stream(output, [[message()], [json.dumps(rejected)]])
                self.assertTrue(connector.induced)
                self.assertEqual(connector.connections, 2)
                self.assertEqual(report["stages"]["stream"]["status"], "FAIL")
                self.assertEqual(report["stages"]["recovery"]["status"], "FAIL")
                summary = json.loads((output / "stream_summary.json").read_text())
                self.assertFalse(summary["recovered"])
                self.assertEqual(summary["selected_messages"], 1)
                events = [json.loads(line) for line in (output / "stream.jsonl").read_text().splitlines()]
                self.assertEqual(sum(e["event"] == "message" for e in events), 1)
                # Defensively reject historical unsafe recordings in summarizer.
                prefix = [event(message()), event(event="disconnected", reason="InducedLocalDisconnect"),
                          event(event="reconnecting"), event(event="connected"), event(event="subscription_sent")]
                self.assertFalse(live.summarize(prefix + [event(json.dumps(rejected))], IDENTITY)["recovered"])

    def test_full_validator_terminal_auth_after_new_data_still_not_recovery_pass(self):
        rejected = json.loads(message())
        rejected["code"] = "403"
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "validation"
            report, _ = self.validator_stream(output, [[message()], [message(), json.dumps(rejected)]])
            summary = json.loads((output / "stream_summary.json").read_text())
            self.assertFalse(summary["recovered"])
            self.assertTrue(summary["terminal_rejection"])
            self.assertEqual(report["stages"]["recovery"]["status"], "FAIL")

    def test_auth_md_cannot_trigger_initial_fault(self):
        for status in (401, 403, "401", "403"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as folder:
                rejected = json.loads(message())
                rejected["status"] = status
                report, connector = self.validator_stream(Path(folder) / "validation", [[json.dumps(rejected)]])
                self.assertFalse(connector.induced)
                self.assertEqual(connector.connections, 1)
                self.assertNotEqual(report["stages"]["recovery"]["status"], "PASS")

    def test_opt_in_missing_credentials_and_exclusive_artifacts(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True), \
                patch.object(live, "authenticate") as auth:
            report = live.validate(Path(folder) / "no_opt_in")
            self.assertTrue(all(s["status"] == "NOT_RUN" for s in report["stages"].values()))
            report = live.validate(Path(folder) / "missing", live=True)
            self.assertEqual(report["stages"]["credentials"]["status"], "BLOCKED")
            auth.assert_not_called()
            with self.assertRaises(FileExistsError):
                live.validate(Path(folder) / "missing", live=True)

    def test_duration_bounds_no_auth_no_files(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(live, "authenticate") as auth:
            for value in (0, -1, 301, float("inf"), float("nan")):
                with self.assertRaises(ValueError):
                    live.validate(Path(folder) / "invalid", live=True, duration=value)
            auth.assert_not_called()
            self.assertFalse((Path(folder) / "invalid").exists())

    def test_status_allowlist(self):
        report = {"stages": {}}
        for status in live.STATUSES:
            live.stage(report, "stream", status, "reason")
            self.assertEqual(report["stages"]["stream"]["status"], status)
        with self.assertRaises(ValueError):
            live.stage(report, "stream", "SUCCESS", "reason")
        session = Mock()
        for method, path in (("GET", "/rest/order/newSingleOrder"), ("POST", "/rest/instruments/all"),
                             ("GET", "https://evil.example"), ("GET", "/rest/instruments/all?redirect=1")):
            with self.assertRaises(rest.PrimaryError):
                rest.request(session, method, path)
        session.request.assert_not_called()
        session.request.return_value.status_code = 200
        rest.request(session, "GET", "/rest/instruments/all")
        args, kwargs = session.request.call_args
        self.assertEqual(args[1], rest.BASE_URL + "/rest/instruments/all")
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["timeout"], (10, 30))

    def test_session_gate_and_provenance(self):
        now = datetime(2026, 10, 2, 16, tzinfo=timezone.utc)
        evidence = {"date": "2026-10-02", "trading_day": True, "remarkets_session_confirmed": True,
                    "calendar_source": "https://example.org/calendar", "remarkets_source": "https://example.org/demo"}
        self.assertFalse(live.session_gate(now, 300, None)[0])
        self.assertTrue(live.session_gate(now, 300, evidence)[0])
        self.assertFalse(live.session_gate(now.replace(hour=20), 300, evidence)[0])
        for key, value in (("trading_day", False), ("date", "2026-10-01"),
                           ("remarkets_session_confirmed", False), ("calendar_source", "")):
            self.assertFalse(live.session_gate(now, 300, {**evidence, key: value})[0])

    def test_safe_rest_payloads_and_artifacts(self):
        with tempfile.TemporaryDirectory() as folder:
            for payload in ({"nested": ["secret-value"]}, {"secret-value": 3},
                            {"X-Auth-Token": "unknown"}, {"password": "unknown"}):
                with self.assertRaises(rest.PrimaryError):
                    live.write_json(Path(folder) / "unsafe.json", payload, ["secret-value"])
                self.assertFalse((Path(folder) / "unsafe.json").exists())
        session = Mock()
        session._primary_secrets = ["secret-value"]
        session.request.return_value.status_code = 200
        session.request.return_value.json.return_value = {"status": "OK", "instruments": [{"echo": "secret-value"}]}
        with self.assertRaises(rest.PrimaryError):
            rest.get_json(session, "/rest/instruments/all")

    def test_safe_error_report_missing_contract_and_session_block(self):
        def login(session, username, password):
            session.headers["X-Auth-Token"] = "very-secret-token"

        def payload(session, path, **params):
            if path == "/rest/segment/all":
                return {"status": "OK", "segments": []}
            if path == "/rest/instruments/all":
                return {"status": "OK", "instruments": CATALOG}
            if path == "/rest/instruments/detail":
                return {"status": "OK", "instrument": {**CATALOG[0], "maturityDate": "20991231"}}
            return {"status": "OK", "marketData": {"BI": []}}

        with tempfile.TemporaryDirectory() as folder, \
                patch.dict(os.environ, {"PRIMARY_USER": "very-secret-user", "PRIMARY_PASSWORD": "very-secret-pass"}), \
                patch.object(live, "authenticate", side_effect=login), \
                patch.object(live, "get_json", side_effect=payload), patch.object(live, "run") as stream:
            report = live.validate(Path(folder) / "session", live=True)
            self.assertEqual(report["stages"]["snapshot"]["status"], "PASS")
            self.assertEqual(report["stages"]["stream"]["status"], "BLOCKED")
            stream.assert_not_called()
            report = live.validate(Path(folder) / "contract", live=True, symbol="OTHER")
            self.assertEqual(report["stages"]["contract"]["status"], "BLOCKED")
            self.assertEqual(report["stages"]["snapshot"]["status"], "NOT_RUN")
            with patch.object(live, "authenticate", side_effect=rest.PrimaryError("very-secret-pass")):
                report = live.validate(Path(folder) / "error", live=True)
                self.assertEqual(report["stages"]["authentication"]["status"], "BLOCKED")
            for path in Path(folder).rglob("*.json"):
                self.assertNotIn("very-secret", path.read_text())

    def test_real_recorder_fault_wrapper_ordered_recovery_and_bound(self):
        clock = Clock()
        sockets = [Socket(clock, [(websocket.ABNF.OPCODE_PONG, ""),
                                  (websocket.ABNF.OPCODE_TEXT, message("OTHER")),
                                  (websocket.ABNF.OPCODE_TEXT, message())]),
                   Socket(clock, [(websocket.ABNF.OPCODE_TEXT, message(data={"OF": [{"price": 1}]}))])]
        opened = []

        def connect(token, timeout):
            self.assertLessEqual(timeout, 5)
            sock = sockets.pop(0)
            opened.append(sock)
            return sock

        connector = live.FaultConnector(*IDENTITY, ["secret-user", "secret-pass"], connect_fn=connect)
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(md, "authenticate", side_effect=lambda s, u, p: s.headers.update({"X-Auth-Token": "secret-token"})), \
                patch.object(md, "get_json", return_value={"instruments": CATALOG}):
            path = Path(folder) / "stream.jsonl"
            md.run(SYMBOL, "ROFX", path, "secret-user", "secret-pass", duration=9, stale_after=20,
                   connect_fn=connector, clock=clock, sleep=clock.sleep)
            events = [json.loads(line) for line in path.read_text().splitlines()]
            summary = live.summarize(events, IDENTITY)
            self.assertTrue(connector.induced)
            self.assertEqual(connector.connections, 2)
            self.assertTrue(summary["recovered"])
            self.assertEqual(summary["selected_messages"], 2)
            self.assertEqual([e["event"] for e in summary["sequence"]],
                             ["selected_Md", "induced_local_disconnect", "reconnecting", "connected", "subscription_sent", "selected_Md"])
            self.assertTrue(all(s.closed for s in opened))
            self.assertEqual(clock.value, 9)
            self.assertEqual(len(opened[1].sent), 1)
            self.assertNotIn("secret-", path.read_text())
            with self.assertRaises(rest.PrimaryError):
                live.FaultConnector(*IDENTITY, [], connect_fn=lambda t, v: opened[0])(
                    "secret-token", 1).send(json.dumps({"type": "no"}))

    def test_post_reconnect_evidence_and_honest_freshness(self):
        initial = event(message(data={"BI": [], "TV": 0, "LA": {"date": "2026-10-02T14:00:00Z"}}))
        prefix = [initial, event(event="disconnected", reason="InducedLocalDisconnect"),
                  event(event="reconnecting"), event(event="connected"), event(event="subscription_sent")]
        for suffix in ([], [event(message("OTHER"))], [event(message(data={}))],
                       [event(event="disconnected"), event(message())]):
            self.assertFalse(live.summarize(prefix + suffix, IDENTITY)["recovered"])
        summary = live.summarize(prefix + [event(message())], IDENTITY)
        self.assertTrue(summary["recovered"])
        self.assertEqual(summary["availability"]["BI"]["empty"], 2)
        self.assertEqual(summary["availability"]["TV"]["nonempty"], 2)
        self.assertEqual(summary["availability"]["OF"]["observations"], 0)
        self.assertEqual(summary["server_timestamps"][0]["age_seconds_at_receipt"], 3600)
        self.assertIsNone(live.timestamp_age("2026-10-02T14:00:00", datetime.now(timezone.utc)))

    def test_exploratory_separation_and_complete_300_deadline(self):
        full = message(data={"BI": [{"price": 1}], "OF": [{"price": 2}], "LA": {"price": 1}, "TV": 0})
        # Repeated real offline fixture frames prevent heartbeat exhaustion.
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "experiment"
            report, connector = self.validator_stream(output, [[full], [full] * 300], exploratory=True, duration=300)
            self.assertEqual(report["mode"], "exploratory-demo")
            self.assertEqual(report["stages"]["session"]["status"], "BLOCKED")
            self.assertEqual(report["stages"]["stream"]["status"], "PASS")
            self.assertEqual(report["stages"]["recovery"]["status"], "PASS")
            self.assertFalse(all(s["status"] == "PASS" for s in report["stages"].values()))
            summary = json.loads((output / "stream_summary.json").read_text())
            self.assertTrue(summary["full_300_seconds_completed"])
            self.assertEqual(summary["end_evidence"]["elapsed_seconds"], 300)
            self.assertEqual(summary["induced_local_disconnects"], 1)
            self.assertEqual(connector.connections, 2)
            self.assertFalse((output / "session_evidence.json").exists())

    def test_exploratory_still_needs_live_opt_in(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(live, "authenticate") as auth:
            report = live.validate(Path(folder) / "no_network", exploratory_demo=True)
            auth.assert_not_called()
            self.assertTrue(all(s["status"] == "NOT_RUN" for s in report["stages"].values()))

    def test_exploratory_cli_aggregate_is_nonzero(self):
        from contextlib import redirect_stdout
        from io import StringIO
        report = {"stages": {s: {"status": "PASS"} for s in live.STAGES}}
        report["stages"]["session"]["status"] = "BLOCKED"
        with patch.object(live, "validate", return_value=report) as validator, redirect_stdout(StringIO()):
            status = live.main(["--live", "--exploratory-demo", "--no-env", "--output-dir", "unused_offline_fixture"])
        self.assertEqual(status, 2)
        self.assertTrue(validator.call_args.kwargs["exploratory_demo"])
        self.assertTrue(validator.call_args.kwargs["live"])

    def test_incomplete_deadline_or_empty_entries_cannot_pass(self):
        full = message(data={"BI": [{"price": 1}], "OF": [{"price": 2}], "LA": {"price": 1}, "TV": 0})
        session = event(event="session", marketId=IDENTITY[0], symbol=IDENTITY[1], duration_seconds=300)
        prefix = [session, event(full), event(event="disconnected", reason="InducedLocalDisconnect"),
                  event(event="reconnecting"), event(event="connected"), event(event="subscription_sent"), event(full)]
        end = event(event="ended", reason="duration_deadline", duration_seconds=300, elapsed_seconds=300, deadline_reached=True)
        self.assertTrue(live.summarize(prefix + [end], IDENTITY, expected_duration=300)["duration_completed"])
        for changed in ({"elapsed_seconds": 299}, {"deadline_reached": False}, {"duration_seconds": 12}, {"reason": "interrupt"}):
            self.assertFalse(live.summarize(prefix + [{**end, **changed}], IDENTITY, expected_duration=300)["duration_completed"])
        self.assertFalse(live.summarize(prefix, IDENTITY, expected_duration=300)["duration_completed"])
        short = {**end, "duration_seconds": 12, "elapsed_seconds": 12}
        short_prefix = [{**session, "duration_seconds": 12}] + prefix[1:]
        self.assertTrue(live.summarize(short_prefix + [short], IDENTITY, expected_duration=12)["duration_completed"])
        self.assertFalse(live.summarize(prefix + [short], IDENTITY, expected_duration=12)["full_300_seconds_completed"])
        self.assertFalse(live.summarize(prefix + [end, event(event="authentication_rejected")], IDENTITY, expected_duration=300)["duration_completed"])

    def test_full_validator_observed_transport_with_missing_la(self):
        observed = message(data={"BI": [{"price": 1}], "OF": [{"price": 2}], "LA": None, "TV": 0})
        with tempfile.TemporaryDirectory() as folder:
            report, connector = self.validator_stream(Path(folder) / "partial", [[observed], [observed] * 300],
                                                       exploratory=True, duration=300)
            self.assertEqual(report["stages"]["stream"]["status"], "PASS")
            self.assertEqual(report["stages"]["recovery"]["status"], "PASS")
            self.assertEqual(report["stages"]["session"]["status"], "BLOCKED")
            self.assertEqual(report["observations"]["entry_completeness"],
                             {"all_requested_entries_nonempty": False, "entries_without_nonempty_observation": ["LA"]})
            self.assertTrue(report["observations"]["full_300_seconds_completed"])
            self.assertTrue(report["observations"]["ordered_transport_recovery_observed"])
            self.assertEqual(connector.connections, 2)

    def test_full_validator_empty_wrong_symbol_pong_and_missing_deadline(self):
        cases = ([[message(data={"BI": [], "OF": [], "LA": None, "TV": None})]],
                 [[message("OTHER")]], [[(websocket.ABNF.OPCODE_PONG, "")] * 12])
        for frames in cases:
            with self.subTest(frames=frames), tempfile.TemporaryDirectory() as folder:
                report, connector = self.validator_stream(Path(folder) / "no_selected", frames, exploratory=True)
                self.assertEqual(report["stages"]["stream"]["status"], "BLOCKED")
                self.assertNotEqual(report["stages"]["recovery"]["status"], "PASS")
                self.assertFalse(connector.induced)
        with tempfile.TemporaryDirectory() as folder:
            report, _ = self.validator_stream(Path(folder) / "no_end", [[message()], [message()]],
                                              exploratory=True, omit_end=True)
            self.assertFalse(report["observations"]["duration_completed"])
            self.assertNotEqual(report["stages"]["stream"]["status"], "PASS")
            self.assertNotEqual(report["stages"]["recovery"]["status"], "PASS")

    def test_full_validator_terminal_after_observed_recovery(self):
        for rejection in ({"type": "error", "status": "REJECTED"}, {"type": "Md", "status": 401}):
            with self.subTest(rejection=rejection), tempfile.TemporaryDirectory() as folder:
                report, _ = self.validator_stream(Path(folder) / "rejected", [[message()], [message(), json.dumps(rejection)]], exploratory=True)
                self.assertTrue(report["observations"]["terminal_rejection"])
                self.assertEqual(report["stages"]["stream"]["status"], "FAIL")
                self.assertEqual(report["stages"]["recovery"]["status"], "FAIL")

    def test_full_validator_all_upgrade_rejections_one_attempt_no_retry(self):
        for status in (301, 302, 307, 308, 401, 403, 429, 503):
            for raised in (False, True):
                with self.subTest(status=status, raised=raised), tempfile.TemporaryDirectory() as folder:
                    sock = Mock()
                    sock.handshake_response.status = status
                    kwargs = {"side_effect": websocket.WebSocketBadStatusException("withheld", status_code=status)} if raised else {"return_value": sock}
                    with patch.object(md.websocket, "create_connection", **kwargs) as create:
                        output = Path(folder) / "rejected"
                        report, connector = self.validator_stream(output, [], exploratory=True, connect_fn=md.connect)
                    self.assertEqual(create.call_count, 1)
                    self.assertEqual(create.call_args.kwargs["redirect_limit"], 0)
                    if not raised:
                        sock.close.assert_called_once()
                    self.assertEqual(connector.connections, 0)
                    self.assertEqual(report["stages"]["stream"]["status"], "FAIL")
                    self.assertNotEqual(report["stages"]["recovery"]["status"], "PASS")
                    rows = [json.loads(line) for line in (output / "stream.jsonl").read_text().splitlines()]
                    self.assertFalse(any(row["event"] in ("reconnecting", "subscription_sent", "ended") for row in rows))

    def test_real_library_upgrade_rejections_one_fixed_host_handshake(self):
        import websocket._core as core

        # Keep real create_connection and WebSocket.connect. Only the underlying
        # socket transport and HTTP handshake are replaced; no network is used.
        for status in (301, 302, 307, 308, 401, 403, 429, 503):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as folder:
                transport_socket = Mock()
                response = Mock(status=status, headers={"location": "wss://redirect.invalid/"})
                handshake_options = ({"return_value": response} if status in (301, 302, 307, 308)
                                     else {"side_effect": websocket.WebSocketBadStatusException("withheld", status_code=status)})
                with patch.object(core, "connect", return_value=(transport_socket, ("api.remarkets.primary.com.ar", 443, "/"))) as transport, \
                        patch.object(core, "handshake", **handshake_options) as handshake:
                    output = Path(folder) / "rejected"
                    report, connector = self.validator_stream(output, [], exploratory=True, connect_fn=md.connect)
                self.assertEqual(transport.call_count, 1)
                self.assertEqual(handshake.call_count, 1)
                self.assertEqual(transport.call_args.args[0], md.WS_URL)
                self.assertEqual(handshake.call_args.args[1], md.WS_URL)
                self.assertEqual(handshake.call_args.kwargs["redirect_limit"], 0)
                self.assertEqual(handshake.call_args.kwargs["header"], {"X-Auth-Token": "recorder-issued-secret-B"})
                transport_socket.close.assert_called_once()
                self.assertEqual(connector.connections, 0)
                self.assertEqual(report["stages"]["stream"]["status"], "FAIL")
                self.assertFalse(report["observations"]["recovery_pass"])
                rows = [json.loads(line) for line in (output / "stream.jsonl").read_text().splitlines()]
                self.assertFalse(any(row["event"] in ("reconnecting", "subscription_sent", "ended") for row in rows))

    def test_real_library_transport_failure_retains_bounded_retries(self):
        import websocket._core as core

        with tempfile.TemporaryDirectory() as folder, \
                patch.object(core, "connect", side_effect=OSError("offline transport failure")) as transport, \
                patch.object(core, "handshake") as handshake:
            output = Path(folder) / "transport"
            report, _ = self.validator_stream(output, [], exploratory=True, connect_fn=md.connect)
            self.assertEqual(transport.call_count, 4)
            handshake.assert_not_called()
            self.assertTrue(all(call.args[0] == md.WS_URL for call in transport.call_args_list))
            rows = [json.loads(line) for line in (output / "stream.jsonl").read_text().splitlines()]
            self.assertEqual(sum(row["event"] == "reconnecting" for row in rows), 3)
            self.assertEqual(report["stages"]["stream"]["status"], "FAIL")

    def test_offline_retrospective_preserves_original_and_exclusive_output(self):
        from hashlib import sha256
        from reanalyze_demo import reanalyze
        with tempfile.TemporaryDirectory() as folder:
            original = Path(folder) / "original"
            self.validator_stream(original, [[message()], [message()]], exploratory=True)
            before = {p.name: sha256(p.read_bytes()).hexdigest() for p in original.iterdir()}
            output = Path(folder) / "retrospective"
            with patch.object(live, "authenticate") as auth, patch.object(md, "connect") as connect:
                result = reanalyze(original, output)
                auth.assert_not_called()
                connect.assert_not_called()
            self.assertEqual(result["original_file_sha256"], before)
            self.assertEqual({p.name: sha256(p.read_bytes()).hexdigest() for p in original.iterdir()}, before)
            self.assertTrue(result["no_new_network_activity"])
            self.assertEqual(result["retrospective_observational_stages"]["session"]["status"], "BLOCKED")
            self.assertEqual(result["aggregate_exit_equivalent"], 2)
            with self.assertRaises(FileExistsError):
                reanalyze(original, output)
            with self.assertRaises(ValueError):
                reanalyze(original, original / "nested")

    def test_maintenance_upgrade_stops_without_retry(self):
        connector = live.FaultConnector(*IDENTITY, [], connect_fn=Mock(side_effect=websocket.WebSocketBadStatusException("withheld", status_code=503)))
        with self.assertRaises(rest.PrimaryError):
            connector("secret-token", 1)
        self.assertEqual(connector.connections, 0)
        self.assertFalse(connector.induced)


if __name__ == "__main__":
    unittest.main()

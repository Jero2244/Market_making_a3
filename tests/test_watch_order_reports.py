import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from market_making.execution import watch_order_reports as cli
from market_making.execution.demo_execution import DemoClient


class NeverEnv:
    def get(self, *args):
        raise AssertionError("Credential environment read")


class WatchTests(unittest.TestCase):
    def main(self, argv, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return cli.main(argv, **kwargs)

    def test_default_no_env_network_or_output(self):
        runner = Mock(side_effect=AssertionError("network"))
        factory = Mock(side_effect=AssertionError("output"))
        self.assertEqual(self.main([], environ=NeverEnv(), runner=runner, evidence_factory=factory), 2)
        runner.assert_not_called()
        factory.assert_not_called()

    def test_help_no_env_network_or_output(self):
        factory = Mock()
        runner = Mock()
        with self.assertRaises(SystemExit) as exit:
            self.main(["--help"], environ=NeverEnv(), runner=runner, evidence_factory=factory)
        self.assertEqual(exit.exception.code, 0)
        factory.assert_not_called()
        runner.assert_not_called()

    def test_invalid_args_and_bounds_before_env_or_file(self):
        for args in (["--live"], ["--live", "--output", "unused", "--duration", "nan"],
                     ["--live", "--output", "unused", "--interval", "0"],
                     ["--live", "--output", "unused", "--reconnects", "11"]):
            factory = Mock()
            runner = Mock()
            self.assertEqual(self.main(args, environ=NeverEnv(), runner=runner, evidence_factory=factory), 2)
            factory.assert_not_called()
            runner.assert_not_called()
        for flag in ("--url", "--account", "--send", "--cancel", "--dotenv"):
            with self.assertRaises(SystemExit) as exit:
                self.main(["--live", flag, "x"], environ=NeverEnv())
            self.assertEqual(exit.exception.code, 2)

    def test_collision_before_credentials_preserves_existing(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "evidence.jsonl"
            path.write_text("existing", encoding="utf-8")
            runner = Mock()
            self.assertEqual(self.main(["--live", "--output", str(path)], environ=NeverEnv(), runner=runner), 2)
            self.assertEqual(path.read_text(encoding="utf-8"), "existing")
            runner.assert_not_called()

    def test_missing_credentials_blocked_evidence_no_network(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "evidence.jsonl"
            runner = Mock()
            self.assertEqual(self.main(["--live", "--output", str(path)], environ={}, runner=runner), 2)
            runner.assert_not_called()
            evidence = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(evidence["orders_sent"], 0)
            self.assertEqual(evidence["readiness"], "unverified")

    def test_mock_live_exit_semantics_and_no_rest_mutation(self):
        env = {"PRIMARY_USER": "USER_SECRET", "PRIMARY_PASSWORD": "PASS_SECRET", "PRIMARY_ACCOUNT": "DEMO_ACCOUNT"}
        for result, expected in (({"qualifying_reports": 1}, 0), ({"qualifying_reports": 0}, 2),
                                 (RuntimeError("TOKEN_SECRET"), 2), (KeyboardInterrupt(), 130)):
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "evidence.jsonl"
                def runner(account, sink, user, password, **kwargs):
                    self.assertEqual((account, user, password), ("DEMO_ACCOUNT", "USER_SECRET", "PASS_SECRET"))
                    sink.write({"event": "mock", "orders_sent": 0, "readiness": "unverified", "gaps": []})
                    if isinstance(result, BaseException):
                        raise result
                    return result
                with patch.object(DemoClient, "status", side_effect=AssertionError("REST reconciliation")), patch.object(DemoClient, "monitor_ready", side_effect=AssertionError("preflight")):
                    self.assertEqual(self.main(["--live", "--output", str(path)], environ=env, runner=runner), expected)
                self.assertNotIn("SECRET", path.read_text(encoding="utf-8"))
                path.unlink()  # close observed even on exception/interrupt

    def test_sink_reservation_failure_before_env(self):
        runner = Mock()
        self.assertEqual(self.main(["--live", "--output", "new"], environ=NeverEnv(), runner=runner,
            evidence_factory=Mock(side_effect=OSError("blocked"))), 2)
        runner.assert_not_called()

    def test_sink_close_failure_cannot_report_success(self):
        sink = Mock()
        sink.close.side_effect = OSError("secret")
        env = {"PRIMARY_USER": "u", "PRIMARY_PASSWORD": "p", "PRIMARY_ACCOUNT": "DEMO"}
        self.assertEqual(self.main(["--live", "--output", "new"], environ=env,
            evidence_factory=lambda path: sink, runner=Mock(return_value={"qualifying_reports": 1})), 2)

    def test_injected_live_real_session_real_evidence_no_execution_calls(self):
        from market_making.execution.websocket_order_session import run_session
        from market_making.market_data import check_connection
        from test_websocket_order_session import Clock, Socket, text
        from test_order_reports import report
        clock = Clock()
        sock = Socket(clock, [text(report(cumQty=20)), text(report(status="CANCELLED", cumQty=20))])
        http = Mock()
        env = {"PRIMARY_USER": "USER_SECRET", "PRIMARY_PASSWORD": "PASS_SECRET", "PRIMARY_ACCOUNT": "DEMO_ACCOUNT"}
        def runner(account, sink, user, password, **kwargs):
            return run_session(account, sink, user, password, **kwargs, clock=clock,
                sleep=clock.sleep, connect_fn=lambda token, timeout: sock,
                authenticate_fn=lambda session, user, password, timeout: "TOKEN_SECRET",
                session_factory=lambda: http)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "new.jsonl"
            with patch.object(DemoClient, "call", side_effect=AssertionError("execution endpoint")), patch.object(DemoClient, "monitor_ready", side_effect=AssertionError("monitor readiness")), patch.object(check_connection, "load_dotenv", side_effect=AssertionError("dotenv")):
                self.assertEqual(self.main(["--live", "--duration", "3", "--output", str(path)], environ=env, runner=runner), 0)
            evidence = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertTrue(all(e["orders_sent"] == 0 and e["readiness"] == "unverified" for e in evidence))
            self.assertTrue(evidence[-1]["fill_observed"])
            self.assertEqual(sock.sent, [{"type": "os", "account": {"id": "DEMO_ACCOUNT"}}])
            http.get.assert_not_called()
            http.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()

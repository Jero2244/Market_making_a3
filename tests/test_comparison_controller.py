"""Source failure, cancellation, generation and independent live-worker checks."""
from dataclasses import replace
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import Mock, patch

from market_making.comparison import a3
from market_making.comparison.controller import ComparisonController, Options, backoff, demo_quotes
from market_making.comparison.models import DEFAULT_INSTRUMENTS, SYMBOLS
from market_making.ppi import remarkets
from market_making.ppi.client import PPIError
from test_real_comparison import quotes, raw_snapshot


class IdleA3:
    def __init__(self):
        self.stopped = threading.Event()

    def connect(self):
        return dict(DEFAULT_INSTRUMENTS)

    def read(self):
        self.stopped.wait(.01)
        return ()

    def cancel(self):
        self.stopped.set()

    def close(self):
        self.cancel()


class ControllerTests(unittest.TestCase):
    def join(self, controller):
        for thread in controller._threads:
            thread.join(2)
        self.assertFalse(controller.running)

    def test_settings_validation_and_backoff_bound(self):
        for options in (Options(min_ticks=-1), Options(min_ticks=float("inf")),
                        Options(max_age=0), Options(max_age=float("nan"))):
            with self.assertRaises(ValueError):
                options.validate()
        self.assertEqual([backoff(n) for n in range(1, 9)], [1, 2, 4, 8, 16, 30, 30, 30])

    def test_demo_is_offline_and_stop_clears_before_worker_exit(self):
        controller = ComparisonController()
        with patch.object(remarkets, "load_credentials", side_effect=AssertionError("credential access")), \
                patch.object(a3.Client, "connect", side_effect=AssertionError("network")):
            self.assertTrue(controller.start(Options(demo=True)))
            deadline = time.monotonic() + 2
            while controller.snapshot()["rows"][0]["A3"] is None and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertTrue(controller.snapshot()["rows"][0]["differences"][0].qualifies)
            controller.settings(3, 10)
            self.assertFalse(controller.snapshot()["rows"][0]["differences"][0].qualifies)
            controller.stop()
            self.assertIsNone(controller.snapshot()["rows"][0]["A3"])
            self.join(controller)
            self.assertTrue(controller.start(Options(demo=True)))
            self.assertEqual(controller.generation, 2)
            controller.stop()
            self.join(controller)

    def test_generation_and_old_responses_cannot_restore_stopped_quotes(self):
        controller = ComparisonController(clock=lambda: 100)
        controller.generation = 2
        controller._publish(1, "A3", (quotes()[0],))
        self.assertEqual(controller.quotes["A3"], {})
        controller._publish(2, "A3", (quotes()[0],))
        controller._publish(2, "A3", (replace(quotes()[0], received_monotonic=99),))
        self.assertEqual(controller.quotes["A3"][SYMBOLS[0]].received_monotonic, 100)
        controller.stop()
        controller._publish(2, "A3", (quotes()[0],))
        self.assertEqual(controller.quotes["A3"], {})

    def test_failure_immediately_invalidates_source_and_fatal_cancels_both(self):
        controller = ComparisonController()
        for quote in quotes():
            controller._publish(0, quote.source, (quote,))
        controller._invalidate(0, "A3", "Retrying")
        self.assertFalse(controller.quotes["A3"])
        self.assertTrue(controller.quotes["reMarkets"])
        client = Mock()
        controller._clients.append(client)
        controller._invalidate(0, "reMarkets", "Check credentials", fatal=True)
        self.assertEqual(controller.error, "Check credentials")
        self.assertTrue(controller._stop.is_set())
        client.cancel.assert_called_once()
        self.assertFalse(controller.quotes["reMarkets"])

    def test_stale_quote_expires_without_another_network_event(self):
        clock = [100]
        controller = ComparisonController(clock=lambda: clock[0])
        controller._threads = [Mock(is_alive=Mock(return_value=True))]
        for q in quotes():
            controller._publish(0, q.source, (q,))
        self.assertTrue(controller.snapshot()["rows"][0]["differences"][0].qualifies)
        clock[0] = 111
        self.assertFalse(controller.snapshot()["rows"][0]["differences"][0].qualifies)

    def test_stop_interrupts_blocked_primary_read_and_discards_response(self):
        started = threading.Event()
        cancelled = threading.Event()
        client = Mock()
        def read(instrument, **kwargs):
            started.set()
            cancelled.wait(2)
            return raw_snapshot(instrument["symbol"])
        client.snapshot.side_effect = read
        client.cancel.side_effect = cancelled.set
        controller = ComparisonController(a3_factory=IdleA3, remarkets_factory=Mock(return_value=client))
        with patch.object(remarkets, "load_credentials", return_value=Mock(ready=True)):
            controller.start(Options(env_file=Path("unused")))
            self.assertTrue(started.wait(2))
            self.assertFalse(controller.start(Options(demo=True)))
            controller.stop()
            self.join(controller)
        self.assertTrue(cancelled.is_set())
        self.assertTrue(all(row["reMarkets"] is None for row in controller.snapshot()["rows"]))
        client.close.assert_called_once()

    def test_primary_renewal_is_bounded_and_requests_last_only_as_context(self):
        controller = ComparisonController()
        client = Mock()
        calls = []
        def read(instrument, **kwargs):
            self.assertTrue(kwargs["include_last"])
            calls.append(instrument["symbol"])
            if len(calls) == 1:
                raise PPIError("http_failure_401")
            if len(calls) == 3:
                controller._stop.set()
            return raw_snapshot(instrument["symbol"])
        client.snapshot.side_effect = read
        controller.remarkets_factory = Mock(return_value=client)
        with patch.object(remarkets, "load_credentials", return_value=Mock(ready=True)):
            controller._simulated(0, controller._stop, Options(env_file=Path("unused")))
        client.renew_authentication.assert_called_once()
        self.assertEqual(calls, [SYMBOLS[0], SYMBOLS[0], SYMBOLS[1]])
        client.begin_watch_cycle.assert_called_once()
        client.close.assert_called_once()

    def test_missing_credentials_and_decode_failures_stop_cleanly(self):
        for factory, ready, expected in ((IdleA3, False, "PRIMARY_USER"),
                                         (Mock(side_effect=a3.FeedError("A3 format changed")), True, "A3 format")):
            controller = ComparisonController(a3_factory=factory, remarkets_factory=Mock())
            with patch.object(remarkets, "load_credentials", return_value=Mock(ready=ready)):
                controller.start(Options(env_file=Path("unused")))
                self.join(controller)
            self.assertIn(expected, controller.error)

    def test_transient_a3_connection_is_recreated_after_backoff(self):
        controller = ComparisonController()
        first = Mock()
        first.connect.side_effect = a3.TransientFeedError("Temporary")
        second = Mock()
        second.connect.return_value = dict(DEFAULT_INSTRUMENTS)
        def read():
            controller._stop.set()
            return ()
        second.read.side_effect = read
        controller.a3_factory = Mock(side_effect=[first, second])
        with patch.object(controller._stop, "wait", return_value=False) as waiting:
            controller._real(0, controller._stop, Options())
        self.assertEqual(controller.a3_factory.call_count, 2)
        waiting.assert_called_once_with(1)
        first.close.assert_called_once()
        second.close.assert_called_once()

    def test_primary_transient_failure_clears_quotes_without_error_payload(self):
        controller = ComparisonController()
        controller._publish(0, "reMarkets", (quotes()[1],))
        client = Mock()
        client.snapshot.side_effect = PPIError("http_failure_503")
        controller.remarkets_factory = Mock(return_value=client)
        with patch.object(remarkets, "load_credentials", return_value=Mock(ready=True)), \
                patch.object(controller._stop, "wait", return_value=True):
            controller._simulated(0, controller._stop, Options(env_file=Path("unused")))
        self.assertEqual(controller.quotes["reMarkets"], {})
        self.assertIn("retry", controller.statuses["reMarkets"])
        self.assertIsNone(controller.error)


if __name__ == "__main__":
    unittest.main()

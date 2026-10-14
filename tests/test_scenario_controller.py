"""Bounded PPI reads, cleanup and cancellation with no live credentials."""
from datetime import date
import threading
import unittest
from unittest.mock import Mock, patch

from market_making.desktop.scenario_controller import ScenarioController
from market_making.ppi.client import PPIError
from market_making.scenarios.calendar import sessions_between


class ControllerTests(unittest.TestCase):
    def setup_mocks(self):
        loader = patch("market_making.desktop.scenario_controller.load_credentials", return_value=Mock(ready=True))
        factory = patch("market_making.desktop.scenario_controller.Client")
        self.addCleanup(loader.stop)
        self.addCleanup(factory.stop)
        return loader.start(), factory.start()

    def collect(self, controller, generation):
        controller._jobs[generation].thread.join(10)
        self.assertFalse(controller._jobs[generation].thread.is_alive())
        events = []
        while not controller.events.empty():
            events.append(controller.events.get_nowait())
        return events

    def test_spot_only_uses_ppi_current_and_closes_client(self):
        loader, factory = self.setup_mocks()
        factory.return_value.get.return_value = {"price": 100, "date": "2026-10-14T12:00:00Z"}
        controller = ScenarioController()
        generation = controller.start("spot", "ignored.env", reference_date=date(2026, 10, 14))
        events = self.collect(controller, generation)
        factory.assert_called_once_with(loader.return_value, live=True, max_requests=12)
        factory.return_value.get.assert_called_once_with("MarketData/Current",
                                                       {"Ticker": "GGAL", "Type": "ACCIONES", "Settlement": "INMEDIATA"})
        factory.return_value.close.assert_called_once()
        self.assertEqual(events[0][2].price, 100)
        self.assertIsNone(events[0][3])

    def test_missing_credentials_prevent_client_construction(self):
        loader, factory = self.setup_mocks()
        loader.return_value.ready = False
        controller = ScenarioController()
        generation = controller.start("spot", "ignored.env", reference_date=date(2026, 10, 14))
        events = self.collect(controller, generation)
        factory.assert_not_called()
        self.assertIn("credentials", events[0][3])

    def test_history_fetches_six_disjoint_annual_chunks(self):
        _, factory = self.setup_mocks()
        def query(endpoint, params):
            self.assertEqual(endpoint, "MarketData/Search")
            days = sessions_between(date.fromisoformat(params["DateFrom"]), date.fromisoformat(params["DateTo"]))
            return [{"date": day.isoformat(), "price": 100} for day in days]
        factory.return_value.get.side_effect = query
        controller = ScenarioController()
        generation = controller.start("history", "ignored.env", reference_date=date(2026, 10, 14), years=5)
        events = self.collect(controller, generation)
        self.assertEqual(factory.return_value.get.call_count, 6)
        self.assertIsNone(events[0][3])
        sample = events[0][2]
        self.assertGreater(len(sample.returns), 1000)
        self.assertEqual(sample.requested_end, date(2026, 10, 13))
        self.assertTrue(all(day < date(2026, 10, 14) for day, _ in sample.closes))

    def test_one_401_recovery_but_second_rejection_stops(self):
        _, factory = self.setup_mocks()
        client = factory.return_value
        client.get.side_effect = [PPIError("http_failure_401"), PPIError("http_failure_401")]
        controller = ScenarioController()
        generation = controller.start("spot", "ignored.env", reference_date=date(2026, 10, 14))
        events = self.collect(controller, generation)
        client.renew_authentication.assert_called_once()
        self.assertEqual(client.get.call_count, 2)
        self.assertIn("login", events[0][3])
        client.close.assert_called_once()

    def test_malformed_and_provider_errors_sanitized(self):
        _, factory = self.setup_mocks()
        for error in (PPIError("http_failure_403"), PPIError("http_failure_429"), RuntimeError("SECRET provider body")):
            with self.subTest(error=type(error)):
                factory.return_value.get.side_effect = error
                controller = ScenarioController()
                generation = controller.start("spot", "ignored.env", reference_date=date(2026, 10, 14))
                events = self.collect(controller, generation)
                self.assertIsNone(events[0][2])
                self.assertTrue(events[0][3])
                self.assertNotIn("SECRET", repr(events))
        factory.return_value.get.side_effect = None
        factory.return_value.get.return_value = {"price": 0}
        controller = ScenarioController()
        generation = controller.start("spot", "ignored.env", reference_date=date(2026, 10, 14))
        self.assertTrue(self.collect(controller, generation)[0][3])

    def test_cancel_interrupts_client_and_discards_late_response(self):
        _, factory = self.setup_mocks()
        entered, release = threading.Event(), threading.Event()
        def query(*_):
            entered.set()
            release.wait(5)
            return {"price": 100}
        factory.return_value.get.side_effect = query
        controller = ScenarioController()
        generation = controller.start("spot", "ignored.env", reference_date=date(2026, 10, 14))
        try:
            self.assertTrue(entered.wait(5))
            controller.cancel()
            factory.return_value.cancel.assert_called_once()
        finally:
            release.set()
        self.assertEqual(self.collect(controller, generation), [])
        factory.return_value.close.assert_called_once()

    def test_new_request_supersedes_old_worker(self):
        _, factory = self.setup_mocks()
        first, second = Mock(), Mock()
        entered, release = threading.Event(), threading.Event()
        def old_query(*_):
            entered.set()
            release.wait(5)
            return {"price": 100}
        first.get.side_effect = old_query
        second.get.return_value = {"price": 200}
        factory.side_effect = [first, second]
        controller = ScenarioController()
        old = controller.start("spot", "ignored.env", reference_date=date(2026, 10, 14))
        try:
            self.assertTrue(entered.wait(5))
            new = controller.start("spot", "ignored.env", reference_date=date(2026, 10, 14))
            controller._jobs[new].thread.join(5)
        finally:
            release.set()
        events = self.collect(controller, old)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], new)
        self.assertEqual(events[0][2].price, 200)
        first.cancel.assert_called_once()
        first.close.assert_called_once()
        second.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()

"""Account worker cancellation and source switching in the Futures P/L screen."""
from datetime import date
import threading
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from market_making.desktop.scenario_controller import ScenarioController
from market_making.desktop.scenario_tab import ScenarioTab
from market_making.ppi.client import PPIError
from test_remarkets_account import fixture, normalize


class AccountControllerTests(unittest.TestCase):
    def setUp(self):
        self.data = fixture()
        loader = patch("market_making.desktop.scenario_controller.load_primary_credentials", return_value=Mock(ready=True))
        factory = patch("market_making.desktop.scenario_controller.RemarketsClient")
        ppi = patch("market_making.desktop.scenario_controller.Client", side_effect=AssertionError("PPI must not be used"))
        for item in (loader, factory, ppi):
            self.addCleanup(item.stop)
        self.loader, self.factory = loader.start(), factory.start()
        ppi.start()
        self.client = self.factory.return_value
        self.client.accounts.return_value = self.data["accounts"]
        self.client.account_read.side_effect = lambda kind, account: self.data[{"positions": "positions", "details": "details", "report": "report"}[kind]]
        self.client.ggal_instrument.side_effect = self.data["instruments"].__getitem__

    def collect(self, controller, generation):
        controller._jobs[generation].thread.join(10)
        self.assertFalse(controller._jobs[generation].thread.is_alive())
        return list(controller.events.queue)

    def test_account_only_reads_ggal_metadata_and_closes(self):
        controller = ScenarioController()
        generation = controller.start("account", "ignored.env", reference_date=date(2026, 10, 14))
        events = self.collect(controller, generation)
        self.assertIsNone(events[0][3])
        self.assertEqual(len(events[0][2].positions), 3)
        self.assertEqual([call.args[0] for call in self.client.ggal_instrument.call_args_list],
                         ["GGAL/OCT26", "GGAL/DIC26", "GGAL/JUN27"])
        self.factory.assert_called_once_with(self.loader.return_value, live=True, max_requests=32)
        self.client.close.assert_called_once()

    def test_missing_credentials_and_arbitrary_remote_errors_are_sanitized(self):
        self.loader.return_value.ready = False
        controller = ScenarioController()
        generation = controller.start("account", "ignored.env", reference_date=date(2026, 10, 14))
        events = self.collect(controller, generation)
        self.factory.assert_not_called()
        self.assertIn("PRIMARY_USER", events[0][3])
        self.loader.return_value.ready = True
        self.client.accounts.side_effect = RuntimeError("PRIVATE_PASSWORD")
        generation = controller.start("account", "ignored.env", reference_date=date(2026, 10, 14))
        events = self.collect(controller, generation)
        self.assertIsNone(events[-1][2])
        self.assertNotIn("PRIVATE_PASSWORD", repr(events))
        self.client.close.assert_called_once()

    def test_account_401_recovers_once(self):
        self.client.accounts.side_effect = [PPIError("http_failure_401"), self.data["accounts"]]
        controller = ScenarioController()
        generation = controller.start("account", "ignored.env", reference_date=date(2026, 10, 14))
        events = self.collect(controller, generation)
        self.assertIsNone(events[0][3])
        self.client.renew_authentication.assert_called_once()
        self.client.accounts.side_effect = PPIError("http_failure_401")
        generation = controller.start("account", "ignored.env", reference_date=date(2026, 10, 14))
        events = self.collect(controller, generation)
        self.assertIn("account access failed", events[-1][3])

    def test_cancel_discards_late_account_data(self):
        entered, release = threading.Event(), threading.Event()
        def query():
            entered.set()
            release.wait(5)
            return self.data["accounts"]
        self.client.accounts.side_effect = query
        controller = ScenarioController()
        generation = controller.start("account", "ignored.env", reference_date=date(2026, 10, 14))
        try:
            self.assertTrue(entered.wait(5))
            controller.cancel()
        finally:
            release.set()
        self.assertEqual(self.collect(controller, generation), [])
        self.client.cancel.assert_called_once()
        self.client.close.assert_called_once()


class AccountSourceControlsTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.env = tk.StringVar(self.root, value="ignored.env")
        self.tab = ScenarioTab(self.root, env_variable=self.env, get_reports=lambda: [])
        self.addCleanup(self.tab.close)
        self.tab.reference_date.set("2026-10-14")
        self.snapshot = normalize(fixture())

    def import_snapshot(self, snapshot=None):
        self.tab.position_mode.set("reMarkets account")
        self.tab.controller.events.put((self.tab.controller.generation, "account", snapshot or self.snapshot, None))
        self.tab.poll()

    def test_import_calculates_ggal_only_and_restores_manual_inputs(self):
        manual = self.tab.positions["2026-10"]
        for key, value in (("contracts", "7"), ("direction", "Short"), ("entry", "6000"), ("current", "6100")):
            manual[key].set(value)
        self.import_snapshot()
        self.assertEqual(self.tab.positions["2026-10"]["contracts"].get(), "3")
        self.assertEqual(self.tab.positions["2026-12"]["direction"].get(), "Short")
        self.assertIn("2027-06", self.tab.positions)
        self.assertIn("3 other instrument(s) excluded", self.tab.account_status.get())
        self.assertIn("158,000,000.00", self.tab.account_balances.get())
        for row in self.tab.positions.values():
            self.assertTrue(all(str(widget.cget("state")) == "disabled" for widget in row["widgets"].values()))
        self.tab.spot.set("6000")
        self.tab.move.set("5")
        self.tab.assumed_tna.set("0")
        self.tab.recalculate()
        self.assertEqual(self.tab.result.total_from_today, -43500)
        self.tab.position_mode.set("Manual simulation")
        self.assertEqual(self.tab.positions["2026-10"]["contracts"].get(), "7")
        self.assertEqual(self.tab.positions["2026-10"]["direction"].get(), "Short")
        self.assertEqual(self.tab.positions["2026-10"]["current"].get(), "6100")
        self.assertNotIn("2027-06", self.tab.positions)
        self.assertEqual(self.tab.account_balances.get(), "")
        self.assertIsNone(self.tab.result)
        self.tab.position_mode.set("reMarkets account")
        self.tab.position_mode.set("reMarkets account")  # Clicking the selected radio must preserve the manual backup.
        self.tab.position_mode.set("Manual simulation")
        self.assertEqual(self.tab.positions["2026-10"]["contracts"].get(), "7")

    def test_refresh_empty_account_clears_old_positions(self):
        self.import_snapshot()
        empty = fixture()
        empty["positions"]["positions"] = []
        empty["details"]["detailedPosition"]["report"] = {}
        self.import_snapshot(normalize(empty))
        self.assertNotIn("2027-06", self.tab.positions)
        self.assertTrue(all(row["contracts"].get() == "0" for row in self.tab.positions.values()))
        self.assertIn("0 open GGAL", self.tab.account_status.get())

    def test_credentials_change_or_source_switch_discards_queued_import(self):
        self.import_snapshot()
        generation = self.tab.controller.generation
        self.tab.pending_kind = "account"
        self.tab.controller.events.put((generation, "account", self.snapshot, None))
        self.env.set("new-login.env")
        self.tab.poll()
        self.assertIsNone(self.tab.account_snapshot)
        self.assertTrue(all(row["contracts"].get() == "0" for row in self.tab.positions.values()))
        self.assertIn("Credentials file changed", self.tab.account_status.get())
        self.tab.pending_kind = "account"
        generation = self.tab.controller.generation
        self.tab.position_mode.set("Manual simulation")
        self.tab.controller.events.put((generation, "account", self.snapshot, None))
        self.tab.poll()
        self.assertEqual(self.tab.position_mode.get(), "Manual simulation")
        self.assertTrue(all(row["contracts"].get() == "0" for row in self.tab.positions.values()))

    def test_copy_account_to_editable_simulation(self):
        self.import_snapshot()
        self.tab.account_to_manual()
        self.assertEqual(self.tab.position_mode.get(), "Manual simulation")
        self.assertEqual(self.tab.positions["2027-06"]["contracts"].get(), "2")
        self.tab.positions["2027-06"]["contracts"].set("5")
        self.assertEqual(str(self.tab.positions["2027-06"]["widgets"]["entry"].cget("state")), "normal")
        self.tab.position_mode.set("reMarkets account")
        self.assertEqual(self.tab.positions["2027-06"]["contracts"].get(), "2")
        self.tab.position_mode.set("Manual simulation")
        self.assertEqual(self.tab.positions["2027-06"]["contracts"].get(), "5")

    def test_fetch_uses_settings_file_and_failed_refresh_does_not_keep_old_holdings(self):
        self.import_snapshot()
        with patch.object(self.tab.controller, "start") as start, \
                patch("market_making.desktop.scenario_tab.today", return_value=date(2026, 10, 14)):
            self.tab.fetch("account")
        self.assertEqual(start.call_args.args[0], "account")
        self.assertEqual(str(start.call_args.args[1]), "ignored.env")
        self.assertIsNone(self.tab.account_snapshot)
        self.tab.controller.events.put((self.tab.controller.generation, "account", None, "Login failed"))
        self.tab.poll()
        self.assertIn("Login failed", self.tab.account_status.get())
        self.tab.spot.set("6000")
        self.tab.recalculate()
        self.assertIsNone(self.tab.result)
        self.assertIn("Load the reMarkets account", self.tab.summary.get())

"""Offline checks for package commands and stable repository resources."""

from contextlib import redirect_stdout
from importlib.metadata import distribution
import io
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from market_making.execution.smoke_demo_order import LOCK
from market_making.paths import DEFAULT_REPLAY_POLICY, PROJECT_ROOT
from market_making.validation import collect_session_sources, reanalyze_demo, replay_readiness


COMMANDS = {
    "ppi-readonly": "market_making.ppi.cli",
    "check-connection": "market_making.market_data.check_connection",
    "stream-market-data": "market_making.market_data.stream_market_data",
    "view-order-book": "market_making.market_data.view_order_book",
    "smoke-demo-order": "market_making.execution.smoke_demo_order",
    "validate-live": "market_making.validation.validate_live",
    "collect-session-sources": "market_making.validation.collect_session_sources",
    "reanalyze-demo": "market_making.validation.reanalyze_demo",
    "replay-readiness": "market_making.validation.replay_readiness",
}


class ProjectLayoutTests(unittest.TestCase):
    def test_repository_resources(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(PROJECT_ROOT, root)
        self.assertEqual(LOCK, root / ".demo_order_lock.json")
        self.assertEqual(DEFAULT_REPLAY_POLICY, root / "config" / "replay_acceptance_policy.json")
        self.assertTrue(DEFAULT_REPLAY_POLICY.is_file())

    def test_installed_entry_points(self):
        entries = {entry.name: entry for entry in distribution("rfx20-market-making").entry_points
                   if entry.group == "console_scripts"}
        self.assertEqual(set(entries), set(COMMANDS))
        for name, module in COMMANDS.items():
            with self.subTest(command=name):
                self.assertEqual(entries[name].value, module + ":main")
                self.assertTrue(callable(entries[name].load()))

    def test_module_help_outside_repository(self):
        # --help must work without credentials, evidence, or a project-root CWD.
        with TemporaryDirectory() as folder:
            for name, module in COMMANDS.items():
                with self.subTest(command=name):
                    result = subprocess.run(
                        [sys.executable, "-m", module, "--help"], cwd=folder,
                        capture_output=True, text=True, timeout=30,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("--help", result.stdout)
                    self.assertEqual(list(Path(folder).iterdir()), [])

    def test_replay_default_policy_outside_repository(self):
        previous = Path.cwd()
        with TemporaryDirectory() as folder:
            try:
                os.chdir(folder)
                with patch.object(replay_readiness, "audit", return_value={"overall_replay_status": "BLOCKED"}) as audit, \
                        redirect_stdout(io.StringIO()):
                    replay_readiness.main(["--original-dir", "original", "--output-dir", "output"])
                audit.assert_called_once_with(Path("original"), Path("output"), DEFAULT_REPLAY_POLICY)
            finally:
                os.chdir(previous)

    def test_collect_command_delegates(self):
        with patch.object(collect_session_sources, "collect") as collect:
            collect_session_sources.main(["--output-dir", "output"])
        collect.assert_called_once_with(Path("output"))

    def test_reanalyze_command_delegates(self):
        result = {"retrospective_observational_stages": {"session": {"status": "BLOCKED"}}}
        with patch.object(reanalyze_demo, "reanalyze", return_value=result) as reanalyze, \
                redirect_stdout(io.StringIO()):
            reanalyze_demo.main(["--original-dir", "original", "--output-dir", "output"])
        reanalyze.assert_called_once_with(Path("original"), Path("output"))


if __name__ == "__main__":
    unittest.main()

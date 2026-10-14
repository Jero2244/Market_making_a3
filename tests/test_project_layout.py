"""Offline checks for package commands and stable repository resources."""

from importlib.metadata import distribution
from importlib.util import find_spec
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from market_making.paths import PROJECT_ROOT


COMMANDS = {
    "ppi-readonly": "market_making.ppi.cli",
}


class ProjectLayoutTests(unittest.TestCase):
    def test_repository_resources(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(PROJECT_ROOT, root)
        self.assertTrue((root / "config" / "ppi_ggal_watch.example.json").is_file())

    def test_installed_entry_points(self):
        entries = {entry.name: entry for entry in distribution("ggal-desk").entry_points
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

    def test_gui_entry_point(self):
        entries = {entry.name: entry.value for entry in distribution("ggal-desk").entry_points
                   if entry.group == "gui_scripts"}
        self.assertEqual(entries, {"ggal-desk": "market_making.desktop.app:main"})

    def test_retired_packages_are_outside_the_active_installation(self):
        for name in ("execution", "validation", "market_data"):
            with self.subTest(package=name):
                self.assertIsNone(find_spec("market_making." + name))


if __name__ == "__main__":
    unittest.main()

"""Offline desktop lifecycle checks; no credentials or broker requests."""
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock, patch

from market_making.desktop.controller import MonitorController, WatchOptions, default_env_file
from market_making.ppi import monitor


class DesktopControllerTests(unittest.TestCase):
    def collect(self, controller):
        controller._thread.join(3)
        self.assertFalse(controller.running)
        events = []
        while not controller.events.empty():
            events.append(controller.events.get_nowait())
        self.assertEqual(events[-1][0], "finished")
        return events

    def test_input_validation(self):
        for options in (WatchOptions(interval=.5), WatchOptions(interval=float('nan')),
                        WatchOptions(caucion_tna=-1), WatchOptions(caucion_tna=float('inf')),
                        WatchOptions(watch_config=Path('local.json'), caucion_tna=30)):
            with self.subTest(options=options), self.assertRaises(ValueError):
                options.validate()
        WatchOptions().validate()
        WatchOptions(interval=1).validate()
        WatchOptions(demo=True, interval=1).validate()
        WatchOptions(caucion_tna=0).validate()

    def test_demo_never_loads_credentials_or_network(self):
        controller = MonitorController()
        original = monitor.demo_cycle

        def one_cycle(cycle, **kwargs):
            result = original(cycle, **kwargs)
            # Stop after the report has been enqueued, in the interval wait.
            controller._stop.wait = lambda _: True
            return result

        with patch.object(monitor, 'demo_cycle', side_effect=one_cycle), \
                patch.object(monitor, 'load_credentials', side_effect=AssertionError('credential read')), \
                patch.object(monitor, 'load_primary_credentials', side_effect=AssertionError('credential read')), \
                patch.object(monitor, 'live_cycle', side_effect=AssertionError('network')):
            self.assertTrue(controller.start(WatchOptions(demo=True)))
            events = self.collect(controller)
        reports = [payload for kind, payload in events if kind == 'reports'][0]
        self.assertEqual(len(reports), 2)
        self.assertTrue(all(report['mode'] == 'DEMO' and not report['executable'] for report in reports))

    def test_missing_credentials_block_before_network(self):
        controller = MonitorController()
        with patch.object(monitor, 'load_credentials', return_value=Mock(ready=False)), \
                patch.object(monitor, 'load_primary_credentials', return_value=Mock(ready=True)), \
                patch.object(monitor, 'live_cycle') as live:
            controller.start(WatchOptions())
            events = self.collect(controller)
        live.assert_not_called()
        reports = [payload for kind, payload in events if kind == 'reports'][0]
        self.assertEqual(reports[0]['blockers'], ['credentials_unavailable'])
        self.assertIsNone(reports[0]['books'])

    def test_auth_and_unknown_errors_stop_without_leaking(self):
        for error, expected in ((monitor.PPIError('http_failure_401'), 'authentication_failure_stop'),
                                (monitor.PPIError('SECRET remote body'), 'safe_market_data_failure_stop'),
                                (RuntimeError('SECRET arbitrary exception'), 'local_watch_configuration_or_processing_failure')):
            controller = MonitorController()
            with patch.object(monitor, 'load_credentials', return_value=Mock(ready=True)), \
                    patch.object(monitor, 'load_primary_credentials', return_value=Mock(ready=True)), \
                    patch.object(monitor, 'live_cycle', side_effect=error) as live:
                controller.start(WatchOptions())
                events = self.collect(controller)
            live.assert_called_once()
            reports = [payload for kind, payload in events if kind == 'reports'][0]
            self.assertEqual(reports[0]['blockers'], [expected])
            self.assertNotIn('SECRET', repr(events))

    def test_transient_error_retries_and_clears_books(self):
        controller = MonitorController()
        calls = []

        def read(*args, **kwargs):
            controller._stop.wait = lambda _: False
            calls.append(args[2])
            if len(calls) == 1:
                raise monitor.PPIError('http_failure_503')
            raise monitor.PPIError('http_failure_403')

        with patch.object(monitor, 'load_credentials', return_value=Mock(ready=True)), \
                patch.object(monitor, 'load_primary_credentials', return_value=Mock(ready=True)), \
                patch.object(monitor, 'live_cycle', side_effect=read):
            controller.start(WatchOptions())
            events = self.collect(controller)
        reports = [payload for kind, payload in events if kind == 'reports']
        self.assertEqual(calls, [1, 2])
        self.assertEqual(reports[0][0]['blockers'], ['transient_market_data_failure'])
        self.assertTrue(all(report['books'] is None for pair in reports for report in pair))

    def test_stop_discards_inflight_result_and_prevents_duplicate_worker(self):
        controller = MonitorController()
        entered, release = threading.Event(), threading.Event()

        # Keep the original function separate from the patched reference.
        original = monitor.demo_cycle
        def delayed(cycle, **kwargs):
            entered.set()
            release.wait(2)
            return original(cycle)

        with patch.object(monitor, 'demo_cycle', side_effect=delayed):
            controller.start(WatchOptions(demo=True))
            self.assertTrue(entered.wait(1))
            self.assertFalse(controller.start(WatchOptions(demo=True)))
            controller.stop()
            release.set()
            events = self.collect(controller)
        self.assertFalse(any(kind == 'reports' for kind, _ in events))

    def test_expired_contracts_block_before_credentials(self):
        controller = MonitorController()
        config = monitor.default_watch_config()
        config['futures'][0]['maturity'] = '2000-01-01T00:00:00+00:00'
        with patch.object(monitor, 'default_watch_config', return_value=config), \
                patch.object(monitor, 'load_credentials') as credentials:
            controller.start(WatchOptions())
            events = self.collect(controller)
        credentials.assert_not_called()
        self.assertEqual([payload for kind, payload in events if kind == 'reports'][0][0]['blockers'], ['expired_watch_maturity'])

    def test_frozen_env_path_is_beside_exe_outside_project(self):
        with patch.object(sys, 'frozen', True, create=True), \
                patch.object(sys, 'executable', str(Path('build/portable/GGALDesk.exe').resolve())), \
                patch.object(Path, 'is_file', return_value=False):
            self.assertEqual(default_env_file(), Path(sys.executable).parent / '.env')


if __name__ == '__main__':
    unittest.main()

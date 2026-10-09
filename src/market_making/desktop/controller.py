"""Background read-only monitoring; never call Tk from a worker thread."""
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from queue import Queue
import math
import sys
import threading

from market_making.paths import PROJECT_ROOT
from market_making.ppi import monitor
from market_making.ppi.models import timestamp


def default_env_file():
    if not getattr(sys, "frozen", False):
        return PROJECT_ROOT / ".env"
    folder = Path(sys.executable).resolve().parent
    if (folder / ".env").is_file():
        return folder / ".env"
    # A build in this project's dist folder can reuse its existing local file.
    if (folder.parent / "pyproject.toml").is_file() and (folder.parent / "src").is_dir():
        return folder.parent / ".env"
    return folder / ".env"


@dataclass(frozen=True)
class WatchOptions:
    demo: bool = False
    interval: float = 5
    caucion_tna: float | None = None
    env_file: Path | None = None
    watch_config: Path | None = None
    manual_check: bool = False

    def validate(self):
        if not math.isfinite(self.interval) or not 1 <= self.interval <= 86400:
            raise ValueError("Use a refresh interval between 1 and 86400 seconds.")
        if self.caucion_tna is not None and (not math.isfinite(self.caucion_tna) or self.caucion_tna < 0):
            raise ValueError("Enter a non-negative caucion TNA, or leave it blank.")
        if self.watch_config is not None and self.caucion_tna is not None and not self.demo:
            raise ValueError("Clear the caucion field when using a full assessment configuration.")


class MonitorController:
    """Single worker with interruptible waits and sanitized queue messages."""

    def __init__(self):
        self.events = Queue()
        self._stop = threading.Event()
        self._thread = None
        self._session = None

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self, options):
        options.validate()
        if self.running:
            return False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(options,), daemon=True,
                                        name="ggal-readonly-monitor")
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        if self._session is not None:
            self._session.cancel()

    def _run(self, options):
        mode = "DEMO" if options.demo else "LIVE-PROXY"
        cycle = 0
        session = None
        failures = 0

        def failed(reason):
            return [monitor.empty_result(expiry, mode, cycle, datetime.now(timezone.utc),
                                         [reason], manual_check=options.manual_check)
                    for expiry in monitor.TARGETS]

        try:
            if not options.demo:
                config = (monitor.load_watch_config(options.watch_config) if options.watch_config else
                          monitor.default_watch_config(options.caucion_tna))
                now = datetime.now(timezone.utc)
                if any(timestamp(item["maturity"]) <= now for item in config["futures"]):
                    self.events.put(("reports", failed("expired_watch_maturity")))
                    return
                env_file = options.env_file or default_env_file()
                credentials = monitor.load_credentials(env_file)
                primary = monitor.load_primary_credentials(env_file)
                if not credentials.ready or not primary.ready:
                    self.events.put(("reports", failed("credentials_unavailable")))
                    return
                session = monitor.WatchSession(credentials, primary)
                self._session = session
                if self._stop.is_set():
                    session.cancel()
            while not self._stop.is_set():
                cycle += 1
                self.events.put(("status", "Checking prices..."))
                stop_on_error = False
                try:
                    reports = (monitor.demo_cycle(cycle, manual_check=options.manual_check) if options.demo else
                               monitor.live_cycle(config, credentials, cycle, primary_credentials=primary,
                                                  manual_check=options.manual_check, session=session))
                except monitor.PPIError as exc:
                    code = str(exc)
                    auth = code in {"http_failure_401", "http_failure_403", "credentials_unavailable",
                                    "authentication_missing_or_expired", "invalid_authentication_response"}
                    stop_on_error = auth or code not in monitor.TRANSIENT_FAILURES
                    reason = ("authentication_failure_stop" if auth else
                              "safe_market_data_failure_stop" if stop_on_error else "transient_market_data_failure")
                    reports = failed(reason)
                # A stopped session must never publish a late response.
                if self._stop.is_set():
                    break
                self.events.put(("reports", reports))
                if stop_on_error:
                    break
                delay, failures = monitor.refresh_delay(reports, options.interval, failures)
                duration = reports[0].get("acquisition_seconds")
                timing = f" / last read {duration:.2f}s" if duration is not None else ""
                self.events.put(("status", (f"Provider backoff / retry in {delay:g}s" if failures else
                                             f"Monitoring / refresh every {options.interval:g}s") + timing))
                if self._stop.wait(delay):
                    break
        except Exception:
            # Neither remote exception text nor local config contents belong in the UI.
            if not self._stop.is_set():
                self.events.put(("reports", failed("local_watch_configuration_or_processing_failure")))
        finally:
            try:
                if session is not None:
                    session.close()
            finally:
                self._session = None
                self.events.put(("finished", None))

"""Cancellable, bounded one-shot PPI reads; workers never access Tk widgets."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from queue import Queue
import threading

from market_making.ppi.client import Client, PPIError
from market_making.ppi.config import load_credentials
from market_making.ppi.models import timestamp
from market_making.scenarios.calculation import finite
from market_making.scenarios.history import annual_chunks, lookback_start, normalize_history


@dataclass(frozen=True)
class SpotQuote:
    price: float
    source_time: datetime | None
    receipt_time: datetime


@dataclass
class _Job:
    stopped: threading.Event = field(default_factory=threading.Event)
    client: Client | None = None
    thread: threading.Thread | None = None


ERRORS = {
    "credentials_unavailable": "PPI credentials are unavailable. Choose your .env in Settings.",
    "authentication": "PPI login or market-data access failed. Check your credentials and try again.",
    "request": "PPI data could not be fetched. Check the connection and market-data access, then try again.",
    "processing": "PPI returned unusable data or the trading calendar could not be loaded.",
}


class ScenarioController:
    def __init__(self):
        self.events = Queue()
        self.generation = 0
        self._jobs = {}
        self._lock = threading.Lock()

    @property
    def running(self):
        with self._lock:
            job = self._jobs.get(self.generation)
            return job is not None and job.thread.is_alive()

    def cancel(self):
        with self._lock:
            self.generation += 1
            for job in self._jobs.values():
                job.stopped.set()
                if job.client is not None:
                    job.client.cancel()

    def start(self, kind, env_file, *, reference_date, years=3):
        if kind not in ("spot", "history"):
            raise ValueError("Unknown scenario data request.")
        if kind == "history":
            lookback_start(reference_date, years)
        self.cancel()
        with self._lock:
            generation = self.generation
            self._jobs = {key: job for key, job in self._jobs.items() if job.thread.is_alive()}
            job = _Job()
            job.thread = threading.Thread(target=self._run,
                                          args=(generation, job, kind, Path(env_file), reference_date, years),
                                          name="ggal-scenario-data", daemon=True)
            self._jobs[generation] = job
            job.thread.start()
        return generation

    @staticmethod
    def _read(client, endpoint, params, recovered):
        client.ensure_authenticated()
        try:
            return client.get(endpoint, params)
        except PPIError as exc:
            if str(exc) != "http_failure_401" or recovered[0]:
                raise
            recovered[0] = True
            client.renew_authentication()
            return client.get(endpoint, params)

    def _run(self, generation, job, kind, env_file, reference_date, years):
        client = None
        error = None
        result = None
        try:
            if job.stopped.is_set():
                return
            credentials = load_credentials(env_file)
            if not credentials.ready:
                raise PPIError("credentials_unavailable")
            # Five years span at most six annual chunks, with authentication and
            # one 401 recovery still within a twelve-request/120-second budget.
            client = Client(credentials, live=True, max_requests=12)
            with self._lock:
                job.client = client
                if job.stopped.is_set():
                    client.cancel()
                    return
            params = {"Ticker": "GGAL", "Type": "ACCIONES", "Settlement": "INMEDIATA"}
            recovered = [False]
            if kind == "spot":
                raw = self._read(client, "MarketData/Current", params, recovered)
                result = SpotQuote(finite(raw["price"], "PPI spot price", positive=True),
                                   timestamp(raw.get("date")), datetime.now(timezone.utc))
            else:
                # Always exclude today's unfinished daily record, even if a
                # provider includes it in a historical response.
                completed_before = min(reference_date, datetime.now(timezone(timedelta(hours=-3))).date())
                end = completed_before - timedelta(days=1)
                start = lookback_start(reference_date, years)
                records = []
                for first, last in annual_chunks(start, end):
                    if job.stopped.is_set():
                        return
                    raw = self._read(client, "MarketData/Search",
                                     dict(params, DateFrom=first.isoformat(), DateTo=last.isoformat()), recovered)
                    if not isinstance(raw, list) or len(raw) > 10000:
                        raise ValueError("Invalid historical data response.")
                    records.extend(raw)
                if job.stopped.is_set():
                    return
                result = normalize_history(records, start, end, completed_before=completed_before)
        except PPIError as exc:
            code = str(exc)
            error = ("credentials_unavailable" if code == "credentials_unavailable" else
                     "authentication" if code in ("http_failure_401", "http_failure_403",
                                                  "authentication_missing_or_expired", "invalid_authentication_response")
                     else "request")
        except Exception:
            # Never expose remote bodies, raw local configs, or arbitrary errors.
            error = "processing"
        finally:
            try:
                if client is not None:
                    client.close()
            except Exception:
                result, error = None, "request"
            with self._lock:
                if not job.stopped.is_set() and generation == self.generation:
                    self.events.put((generation, kind, result, ERRORS.get(error)))

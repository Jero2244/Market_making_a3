"""Independent cancellable source workers and generation-bound observations."""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import math
import threading
import time

from market_making.ppi import remarkets
from market_making.ppi.client import PPIError
from market_making.ppi.models import Level
from market_making.ppi.monitor import TRANSIENT_FAILURES
from . import a3
from .models import DEFAULT_INSTRUMENTS, SYMBOLS, Quote, compare, remarkets_quote


@dataclass(frozen=True)
class Options:
    demo: bool = False
    env_file: Path | None = None
    min_ticks: float = 1
    max_age: float = 10

    def validate(self):
        if not math.isfinite(self.min_ticks) or not 0 <= self.min_ticks <= 1_000_000:
            raise ValueError("Use a minimum difference from 0 to 1,000,000 ticks.")
        if not math.isfinite(self.max_age) or not .5 <= self.max_age <= 3600:
            raise ValueError("Use a maximum observation age from 0.5 to 3600 seconds.")


def backoff(failures):
    return min(30, 2 ** min(max(0, failures - 1), 5))


def demo_quotes(cycle, now):
    """Cycle through buy, sell, ordinary, empty, invalid and old observations."""
    quotes = []
    phase = (cycle - 1) % 6
    for index, symbol in enumerate(SYMBOLS):
        tick = Decimal(str(DEFAULT_INSTRUMENTS[symbol].tick))
        base = 5800 + index * 200
        def price(offset):
            return float((base + offset) * tick)
        real_bid, real_ask = Level(price(10), 8), Level(price(12), 10)
        sim_bid, sim_ask = Level(price(9), 4), Level(price(13), 6)
        if phase == 0:
            sim_ask, sim_bid = Level(price(8), 6), Level(price(7), 4)
        elif phase == 1:
            sim_bid, sim_ask = Level(price(15), 4), Level(price(16), 6)
        elif phase == 3:
            real_bid, sim_bid = None, None
        elif phase == 4:
            real_bid = Level(price(20), 8)
        receipt = datetime.now(timezone.utc)
        quotes.append(Quote(symbol, "A3", real_bid, real_ask, now - 20 if phase == 5 else now,
                            receipt, price(11), receipt))
        quotes.append(Quote(symbol, "reMarkets", sim_bid, sim_ask, now, receipt,
                            price(10), receipt, acquisition_seconds=.02))
    return tuple(quotes)


class ComparisonController:
    def __init__(self, *, a3_factory=a3.Client, remarkets_factory=remarkets.Client,
                 clock=time.monotonic):
        self.a3_factory = a3_factory
        self.remarkets_factory = remarkets_factory
        self.clock = clock
        self.lock = threading.RLock()
        self.generation = 0
        self._threads = []
        self._clients = []
        self._stop = threading.Event()
        self.options = Options()
        self.quotes = {"A3": {}, "reMarkets": {}}
        self.statuses = {"A3": "Stopped", "reMarkets": "Stopped"}
        self.instruments = dict(DEFAULT_INSTRUMENTS)
        self.error = None

    @property
    def running(self):
        with self.lock:
            return any(thread.is_alive() for thread in self._threads)

    def start(self, options):
        options.validate()
        with self.lock:
            if self.running:
                return False
            self.generation += 1
            generation = self.generation
            self.options = options
            self._stop = threading.Event()
            self.quotes = {"A3": {}, "reMarkets": {}}
            self.instruments = dict(DEFAULT_INSTRUMENTS)
            self.statuses = {source: "Starting" for source in self.quotes}
            self.error = None
            workers = ((self._demo,) if options.demo else (self._real, self._simulated))
            self._threads = [threading.Thread(target=worker, args=(generation, self._stop, options),
                                              daemon=True, name="ggal-comparison-" + worker.__name__)
                             for worker in workers]
            for thread in self._threads:
                thread.start()
            return True

    def stop(self):
        with self.lock:
            self._stop.set()
            self.quotes = {"A3": {}, "reMarkets": {}}
            self.statuses = {source: "Stopped" for source in self.quotes}
            clients = list(self._clients)
        for client in clients:
            client.cancel()

    def settings(self, min_ticks, max_age):
        with self.lock:
            options = Options(self.options.demo, self.options.env_file, min_ticks, max_age)
            options.validate()
            self.options = options

    def _current(self, generation):
        return generation == self.generation and not self._stop.is_set()

    def _publish(self, generation, source, observations):
        with self.lock:
            if not self._current(generation):
                return
            for quote in observations:
                if quote.source != source or quote.symbol not in SYMBOLS:
                    continue
                previous = self.quotes[source].get(quote.symbol)
                if previous is None or previous.received_monotonic <= quote.received_monotonic:
                    self.quotes[source][quote.symbol] = quote
            self.statuses[source] = "Synthetic demo" if self.options.demo else "Connected"

    def _invalidate(self, generation, source, message, *, fatal=False):
        with self.lock:
            if not self._current(generation):
                return
            self.quotes[source].clear()
            self.statuses[source] = message
            clients = []
            if fatal:
                self.error = message
                self._stop.set()
                for other in self.quotes:
                    self.quotes[other].clear()
                    if other != source:
                        self.statuses[other] = "Stopped"
                clients = list(self._clients)
        for client in clients:
            client.cancel()

    def _register(self, client, stop):
        with self.lock:
            self._clients.append(client)
        if stop.is_set():
            client.cancel()

    def _release(self, client):
        try:
            client.close()
        except Exception:
            # Cancellation may already have closed a transport in another thread.
            # Cleanup errors must not resurrect observations or expose responses.
            pass
        finally:
            with self.lock:
                if client in self._clients:
                    self._clients.remove(client)

    def snapshot(self):
        now = self.clock()
        with self.lock:
            active = self.running and not self._stop.is_set()
            rows = []
            for symbol in SYMBOLS:
                real = self.quotes["A3"].get(symbol)
                simulated = self.quotes["reMarkets"].get(symbol)
                spec = self.instruments[symbol]
                differences = compare(spec, real, simulated, now=now, min_ticks=self.options.min_ticks,
                                      max_age=self.options.max_age, active=active)
                rows.append({"symbol": symbol, "instrument": spec, "A3": real,
                             "reMarkets": simulated, "differences": differences})
            return {"generation": self.generation, "active": active, "demo": self.options.demo,
                    "statuses": dict(self.statuses), "error": self.error, "rows": rows,
                    "now": now, "max_age": self.options.max_age}

    def _demo(self, generation, stop, options):
        cycle = 0
        while not stop.is_set():
            cycle += 1
            quotes = demo_quotes(cycle, self.clock())
            for source in ("A3", "reMarkets"):
                self._publish(generation, source, tuple(q for q in quotes if q.source == source))
            if stop.wait(2):
                break

    def _real(self, generation, stop, options):
        failures = 0
        while not stop.is_set():
            client = None
            try:
                client = self.a3_factory()
                self._register(client, stop)
                instruments = client.connect()
                with self.lock:
                    if self._current(generation):
                        self.instruments = instruments
                        self.statuses["A3"] = "Connected / waiting for quotes"
                while not stop.is_set():
                    observations = client.read()
                    if observations:
                        self._publish(generation, "A3", observations)
                        failures = 0
            except a3.TransientFeedError:
                failures += 1
                delay = backoff(failures)
                self._invalidate(generation, "A3", f"A3 unavailable / retry in {delay:g}s")
            except a3.FeedError as exc:
                self._invalidate(generation, "A3", str(exc), fatal=True)
                break
            except Exception:
                self._invalidate(generation, "A3", "A3 could not process prices. Restart or update the app.", fatal=True)
                break
            finally:
                if client is not None:
                    self._release(client)
            if not stop.is_set() and stop.wait(backoff(failures)):
                break

    def _simulated(self, generation, stop, options):
        failures = 0
        try:
            from market_making.desktop.controller import default_env_file
            credentials = remarkets.load_credentials(options.env_file or default_env_file())
            if not credentials.ready:
                self._invalidate(generation, "reMarkets",
                                 "Choose a credentials file in Settings with PRIMARY_USER and PRIMARY_PASSWORD.", fatal=True)
                return
            while not stop.is_set():
                client = None
                try:
                    client = self.remarkets_factory(credentials, live=True)
                    self._register(client, stop)
                    while not stop.is_set():
                        cycle_started = self.clock()
                        client.begin_watch_cycle(quote_count=len(SYMBOLS))
                        client.ensure_authenticated()
                        renewed = False
                        for symbol in SYMBOLS:
                            if stop.is_set():
                                break
                            started = self.clock()
                            instrument = {"symbol": symbol, "market_id": "ROFX", "provider": "remarkets"}
                            try:
                                raw = client.snapshot(instrument, include_last=True)
                            except PPIError as exc:
                                if str(exc) != "http_failure_401" or renewed:
                                    raise
                                renewed = True
                                self._invalidate(generation, "reMarkets", "Renewing reMarkets login / highlights paused")
                                client.renew_authentication()
                                raw = client.snapshot(instrument, include_last=True)
                            finished = self.clock()
                            quote = remarkets_quote(raw, symbol, now=finished, acquisition_seconds=finished - started)
                            self._publish(generation, "reMarkets", (quote,))
                        failures = 0
                        if stop.wait(max(0, 1 - (self.clock() - cycle_started))):
                            break
                except PPIError as exc:
                    code = str(exc)
                    transient = code in TRANSIENT_FAILURES or code == "response_limit_exceeded"
                    failures += 1
                    message = (f"reMarkets unavailable / retry in {backoff(failures):g}s" if transient else
                               "reMarkets login or price access failed. Check Primary credentials and restart.")
                    self._invalidate(generation, "reMarkets", message, fatal=not transient)
                    if not transient:
                        break
                finally:
                    if client is not None:
                        self._release(client)
                if not stop.is_set() and stop.wait(backoff(failures)):
                    break
        except Exception:
            self._invalidate(generation, "reMarkets", "reMarkets could not process prices. Check Settings and restart.", fatal=True)

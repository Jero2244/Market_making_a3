"""Single-frame observations, never an authoritative or reconstructed book."""
import time
import math
from market_making.market_data.websocket_session import ENTRIES, parse_market_frame


class Observation:
    def __init__(self, market_id, symbol, *, depth=5, stale_after=20,
                 clock=time.monotonic, secrets=None, display=None):
        if (isinstance(depth, bool) or not isinstance(depth, int) or depth not in range(1, 6)
                or not math.isfinite(stale_after) or stale_after <= 0):
            raise ValueError("Depth 1..5 and positive finite receipt threshold required.")
        self.market_id, self.symbol = market_id, symbol
        self.depth, self.stale_after = depth, stale_after
        self.clock = clock
        self.secrets = secrets if secrets is not None else []
        self.display = display
        self.generation = 0
        self.connected = False
        self.latest = None
        self.received = None
        self.state = "disconnected"
        self.eligible = False

    def write(self, event):
        kind = event["event"]
        if kind == "connected":
            self.generation += 1
            self.connected = True
            self.latest = self.received = None
            self.state = "generation-reset"
        elif kind in ("disconnected", "reconnecting", "ended", "token_renewal"):
            self.connected = False
            self.latest = self.received = None
            self.state = "disconnected"
        elif kind == "message":
            message, retained = parse_market_frame(event["raw"], self.secrets)
            if not retained:
                return
            identity = message["instrumentId"]
            if (identity.get("marketId"), identity.get("symbol")) != (self.market_id, self.symbol):
                return
            # Replace the WHOLE observation. Omitted sides never inherit values.
            self.latest = message["marketData"]
            self.received = self.clock()
            self.state = "observed"
            if any(key in self.latest for key in ENTRIES):
                self.eligible = True
        if self.display and kind in ("connected", "message", "freshness", "disconnected",
                                     "reconnecting", "ended", "token_renewal"):
            self.display(self.snapshot())

    def snapshot(self):
        age = None if self.received is None else max(0, self.clock()-self.received)
        stale = age is not None and age > self.stale_after
        entries = {}
        for key in ENTRIES:
            if not self.connected:
                state = "disconnected"
            elif self.latest is None:
                state = "generation-reset"
            elif key not in self.latest:
                state = "omitted"
            elif stale:
                state = "stale"
            elif self.latest[key] in (None, []):
                state = "empty"
            else:
                state = "observed"
            value = self.latest.get(key) if self.latest is not None and key in self.latest else None
            entries[key] = {"state": state}
            if self.latest is not None and key in self.latest:
                entries[key]["raw_observation"] = value[:self.depth] if isinstance(value, list) else value
        return {"marketId": self.market_id, "symbol": self.symbol,
                "generation": self.generation, "connected": self.connected,
                "state": "stale" if stale else self.state,
                "partial": self.latest is not None and not all(k in self.latest for k in ("BI", "OF")),
                "receipt_age_seconds": age, "entries": entries,
                "book_semantics": "unknown update semantics; raw single-frame observations only",
                "exchange_freshness": "unverified; receipt age is NOT exchange quote age"}

    def close(self):
        self.connected = False
        self.latest = self.received = None
        self.state = "disconnected"

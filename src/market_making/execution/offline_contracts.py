"""Version 1 synthetic contracts. No network adapters or broker authority.

Times are integer UTC/monotonic nanoseconds. All money/quantity is bounded exact
decimal text; normalized units are contracts and one explicitly named currency.
Caller declarations are fixture evidence, never external attestations.
"""
from dataclasses import dataclass
from decimal import Decimal, Context, localcontext
from hashlib import sha256
import json

from market_making.http_policy import PrimaryError


def require(condition, message):
    if not condition:
        raise PrimaryError(message)


def decimal(value, signed=False):
    require(isinstance(value, str) and 0 < len(value) <= 48, "Exact decimal text required")
    try:
        result = Decimal(value)
        require(result.is_finite() and abs(result.adjusted()) <= 12
                and len(result.as_tuple().digits) <= 32
                and result.as_tuple().exponent >= -12
                and (signed or result >= 0), "Invalid decimal")
        return result
    except ArithmeticError:
        raise PrimaryError("Invalid decimal") from None


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class Identity:
    environment: str
    account: str
    instrument: str
    specification: str
    schema_version: int = 1

    def validate(self):
        require(type(self.schema_version) is int and self.schema_version == 1, "Identity contract version")
        require(self.environment == "OFFLINE_SIMULATION", "Only offline simulation supported")
        require(all(isinstance(v, str) and 0 < len(v) <= 128 for v in
                    (self.account, self.instrument, self.specification)), "Missing identity")


@dataclass(frozen=True)
class Clock:
    utc: int
    mono: int
    uncertainty: int
    valid_until: int
    schema_version: int = 1

    def validate(self):
        require(type(self.schema_version) is int and self.schema_version == 1, "Clock contract version")
        require(all(type(v) is int and v >= 0 for v in
                    (self.utc, self.mono, self.uncertainty, self.valid_until)), "Invalid clock")
        require(self.mono < self.valid_until, "Clock calibration expired")

    def age(self, source, received, max_age):
        self.validate()
        received.validate()
        require(type(source) is int and type(max_age) is int and max_age > 0,
                "Missing source time")
        require(self.mono >= received.mono and self.utc >= received.utc, "Clock rollback")
        require(self.mono < received.valid_until, "Receive clock uncertainty expired")
        uncertainty = self.uncertainty + received.uncertainty
        require(abs((self.utc - received.utc) - (self.mono - received.mono)) <= uncertainty,
                "UTC/monotonic disagreement")
        require(source <= received.utc - received.uncertainty,
                "Future or uncertain source timestamp")
        require(self.utc - source + uncertainty <= max_age, "Stale source")


@dataclass(frozen=True)
class Book:
    identity: Identity
    generation: int
    sequence: int
    baseline: bool
    semantics: str
    bid: str
    ask: str
    bid_utc: int
    ask_utc: int
    received: Clock
    schema_version: int = 1

    def validate(self, identity, now, max_age):
        require(type(self.schema_version) is int and self.schema_version == 1, "Book contract version")
        self.identity.validate()
        require(self.identity == identity and self.semantics == "synthetic_full_book_source_utc_v1",
                "Book identity/semantics mismatch")
        require(type(self.generation) is int and self.generation > 0
                and type(self.sequence) is int and self.sequence > 0
                and type(self.baseline) is bool, "Invalid book sequencing")
        bid, ask = decimal(self.bid), decimal(self.ask)
        require(0 < bid < ask, "Empty/crossed book")
        now.age(self.bid_utc, self.received, max_age)
        now.age(self.ask_utc, self.received, max_age)


class Freshness:
    """Sticky generation quarantine; reconnect needs a strictly newer baseline."""
    def __init__(self, identity, max_age):
        identity.validate()
        self.identity, self.max_age = identity, max_age
        self.book = None
        self.floor = 0
        self.blocked = False

    def disconnect(self):
        if self.book:
            self.floor = max(self.floor, self.book.generation)
        self.book = None
        self.blocked = True

    def accept(self, book, now):
        try:
            book.validate(self.identity, now, self.max_age)
            if self.book is None or book.generation != self.book.generation:
                require(book.baseline and book.sequence == 1 and book.generation > self.floor
                        and (self.book is None or book.generation > self.book.generation),
                        "Fresh generation baseline required")
            else:
                require(not self.blocked, "Generation quarantined")
                if book == self.book:
                    return self.book
                require(not book.baseline and book.sequence == self.book.sequence + 1,
                        "Sequence gap/duplicate conflict/rollback")
                require(book.bid_utc >= self.book.bid_utc and book.ask_utc >= self.book.ask_utc
                        and book.received.mono >= self.book.received.mono,
                        "Source/receive rollback")
            self.floor = book.generation
            self.book, self.blocked = book, False
            return book
        except (PrimaryError, AttributeError, TypeError):
            self.blocked = True
            raise

    def current(self, now):
        require(self.book is not None and not self.blocked, "Book unavailable")
        try:
            self.book.validate(self.identity, now, self.max_age)
        except PrimaryError:
            self.blocked = True
            raise
        return self.book


@dataclass(frozen=True)
class Metadata:
    identity: Identity
    currency: str
    unit: str
    tick: str
    lot: str
    multiplier: str
    low: str
    high: str
    margin_per_contract: str
    schema_version: int = 1

    def validate(self):
        require(type(self.schema_version) is int and self.schema_version == 1, "Metadata contract version")
        self.identity.validate()
        require(self.unit == "contracts" and isinstance(self.currency, str) and self.currency,
                "Unknown currency/units")
        require(all(decimal(v) > 0 for v in (self.tick, self.lot, self.multiplier)),
                "Invalid metadata")
        require(decimal(self.low) < decimal(self.high), "Invalid price bounds")
        decimal(self.margin_per_contract)


@dataclass(frozen=True)
class Fill:
    execution_id: str
    order_id: str
    side: str
    quantity: str


@dataclass(frozen=True)
class WorkingOrder:
    order_id: str
    side: str
    quantity: str
    price: str
    uncertain: bool


@dataclass(frozen=True)
class HistoricalOrder:
    order_id: str
    side: str
    quantity: str
    status: str
    cumulative: str


@dataclass(frozen=True)
class AccountEvidence:
    identity: Identity
    snapshot_id: str
    source_utc: int
    received: Clock
    metadata: Metadata
    baseline_position: str
    position: str
    valuation: str
    cash: str
    margin_available: str
    loss: str
    drawdown: str
    fills: tuple
    working: tuple
    history_ids: tuple
    complete_kinds: tuple
    consistent_snapshot: bool
    historical: tuple = ()
    schema_version: int = 1

    def validate(self, identity, now, max_age):
        require(type(self.schema_version) is int and self.schema_version == 1, "Account contract version")
        self.metadata.validate()
        require(self.identity == identity == self.metadata.identity and self.snapshot_id,
                "Account/specification identity mismatch")
        require(self.consistent_snapshot is True and set(self.complete_kinds) ==
                {"positions", "balances", "margin", "working_orders", "history", "fills", "metadata"},
                "Incomplete/inconsistent account snapshot")
        now.age(self.source_utc, self.received, max_age)
        require(decimal(self.valuation) > 0, "Missing valuation")
        for value in (self.cash, self.margin_available, self.loss, self.drawdown):
            decimal(value)
        require(len(set(self.history_ids)) == len(self.history_ids)
                and all(isinstance(v, str) and v for v in self.history_ids), "Invalid history")
        history = {}
        for order in self.historical:
            require(isinstance(order, HistoricalOrder) and order.order_id in self.history_ids
                    and order.order_id not in history and order.side in {"BUY", "SELL"}
                    and order.status in {"UNKNOWN", "PENDING_NEW", "NEW", "PARTIALLY_FILLED",
                                         "PENDING_CANCEL", "FILLED", "CANCELLED", "REJECTED", "EXPIRED"}
                    and 0 < decimal(order.quantity) and decimal(order.cumulative) <= decimal(order.quantity),
                    "Invalid historical order")
            require(order.status != "FILLED" or decimal(order.cumulative) == decimal(order.quantity),
                    "Historical fill mismatch")
            require(order.status != "REJECTED" or decimal(order.cumulative) == 0,
                    "Historical rejection has fills")
            require(order.status not in {"PENDING_NEW", "NEW"} or decimal(order.cumulative) == 0,
                    "Unfilled status has fills")
            require(order.status != "PARTIALLY_FILLED" or
                    0 < decimal(order.cumulative) < decimal(order.quantity), "Partial status fill mismatch")
            require(order.status != "PENDING_CANCEL" or
                    decimal(order.cumulative) < decimal(order.quantity), "Pending cancel already fully filled")
            history[order.order_id] = order
        require(set(history) == set(self.history_ids), "Historical records missing")
        seen = {}
        with localcontext(Context(prec=128)):
            position = decimal(self.baseline_position, True)
            for fill in self.fills:
                require(isinstance(fill, Fill) and fill.execution_id and fill.order_id in self.history_ids
                        and fill.side in {"BUY", "SELL"} and decimal(fill.quantity) > 0,
                        "Invalid fill/history")
                if fill.execution_id in seen:
                    require(seen[fill.execution_id] == fill, "Conflicting execution identity")
                    continue
                seen[fill.execution_id] = fill
                position += decimal(fill.quantity) * (1 if fill.side == "BUY" else -1)
            require(position == decimal(self.position, True), "Inventory does not match baseline+fills")
            for order in history.values():
                relevant = [f for f in seen.values() if f.order_id == order.order_id]
                require(all(f.side == order.side for f in relevant)
                        and sum((decimal(f.quantity) for f in relevant), decimal("0")) == decimal(order.cumulative),
                        "Historical cumulative fills mismatch")
        ids = set()
        for order in self.working:
            require(isinstance(order, WorkingOrder) and order.order_id in self.history_ids
                    and order.order_id not in ids and order.side in {"BUY", "SELL"}
                    and type(order.uncertain) is bool and decimal(order.quantity) > 0
                    and decimal(order.price) > 0, "Invalid working order")
            ids.add(order.order_id)
            historical = history[order.order_id]
            require(order.side == historical.side
                    and historical.status not in {"FILLED", "CANCELLED", "REJECTED", "EXPIRED"}
                    and (historical.status != "UNKNOWN" or order.uncertain),
                    "Working/history consistency failure")
            with localcontext(Context(prec=128)):
                remaining = decimal(historical.quantity) - decimal(historical.cumulative)
                require((order.uncertain and remaining <= decimal(order.quantity) <= decimal(historical.quantity))
                        or (not order.uncertain and decimal(order.quantity) == remaining),
                        "Working quantity does not conservatively cover remaining exposure")
        require(all(order.status in {"FILLED", "CANCELLED", "REJECTED", "EXPIRED"}
                    or order.order_id in ids for order in history.values()),
                "Pending/uncertain historical order missing from capacity snapshot")
        return self

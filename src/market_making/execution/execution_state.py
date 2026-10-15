"""Offline normalized execution contract, NOT a Primary report adapter.

The reducer is authoritative only for its supplied synthetic event history.
It cannot prove broker completeness, reconnect continuity or account readiness.
Observers in order_reports.py remain non-authoritative and are not wired here.
"""
from copy import deepcopy
from decimal import Context, Decimal, localcontext
from pathlib import Path
from market_making.http_policy import PrimaryError
from market_making.execution.demo_execution import durable, identifier
from market_making.execution.journal import journal_lease, read_journal

STATUSES = {"PENDING_NEW", "NEW", "PARTIALLY_FILLED", "PENDING_CANCEL",
            "FILLED", "CANCELLED", "REJECTED", "EXPIRED"}
TERMINAL = {"FILLED", "CANCELLED", "REJECTED", "EXPIRED"}
FIELDS = {"order_key", "event_id", "sequence", "status", "total", "cum", "leaves"}


def amount(value):
    if not isinstance(value, str) or len(value) > 64:
        raise PrimaryError("Normalized quantities require exact bounded decimal strings.")
    try:
        result = Decimal(value)
        if not result.is_finite() or result < 0 or abs(result.adjusted()) > 18:
            raise ValueError()
        return result
    except (ValueError, ArithmeticError):
        raise PrimaryError("Invalid normalized quantity.") from None


def validate_event(event):
    if not isinstance(event, dict) or set(event) != FIELDS:
        raise PrimaryError("Normalized event schema invalid.")
    identifier(event["order_key"])
    identifier(event["event_id"])
    if (not isinstance(event["order_key"], str) or not isinstance(event["event_id"], str)
            or type(event["sequence"]) is not int or not 0 < event["sequence"] < 2**63
            or not isinstance(event["status"], str) or event["status"] not in STATUSES):
        raise PrimaryError("Normalized event identity/sequence/status invalid.")
    total, cum, leaves = (amount(event[k]) for k in ("total", "cum", "leaves"))
    status = event["status"]
    valid = total > 0 and cum <= total and leaves <= total
    if status in {"PENDING_NEW", "NEW"}:
        valid = valid and cum == 0 and leaves == total
    elif status in {"PENDING_CANCEL", "PARTIALLY_FILLED"}:
        # Inputs have <=64 coefficient digits and adjusted exponent in [-18,18].
        # Their sum needs at most 102 digits including carry; never use ambient
        # precision, exponent limits, rounding mode or traps for this invariant.
        with localcontext(Context(prec=128, Emin=-1000, Emax=1000)):
            valid = valid and cum + leaves == total
        if status == "PARTIALLY_FILLED":
            valid = valid and 0 < cum < total and leaves > 0
    elif status == "FILLED":
        valid = valid and cum == total and leaves == 0
    else:
        valid = valid and leaves == 0
        if status == "REJECTED":
            valid = valid and cum == 0
    if not valid:
        raise PrimaryError("Normalized event quantities inconsistent.")
    return total, cum, leaves


def initial_state(order_key):
    identifier(order_key)
    if not isinstance(order_key, str):
        raise PrimaryError("Normalized order key must be a string.")
    return {"schema_version": 1, "order_key": order_key, "events": [],
            "status": "UNKNOWN", "cum": "0", "last_sequence": 0,
            "blockers": ["BROKER_CONTINUITY_UNPROVEN"], "uncertain": True}


def reduce_event(state, event):
    """Sticky conflicts and maximum cumulative exposure; no incremental fill sums."""
    total, cum, _ = validate_event(event)
    result = deepcopy(state)
    def block(code):
        if code not in result["blockers"]:
            result["blockers"].append(code)
        result["uncertain"] = True
    if event["order_key"] != result["order_key"]:
        raise PrimaryError("Order correlation failed; event not recorded.")
    for previous in result["events"]:
        if previous == event:
            return result
        if previous["event_id"] == event["event_id"] or previous["sequence"] == event["sequence"]:
            block("EVENT_ID_OR_SEQUENCE_CONFLICT")
    if result["events"] and amount(result["events"][0]["total"]) != total:
        block("ORDER_TOTAL_CONFLICT")
    previous_cum = amount(result["cum"])
    if event["sequence"] > result["last_sequence"] + 1:
        block("SEQUENCE_GAP_OBSERVED")
    if event["sequence"] > result["last_sequence"] and cum < previous_cum:
        block("CUMULATIVE_FILL_REGRESSION")
    # Even conflicting/later cancellation must never erase previously observed fills.
    result["cum"] = str(max(previous_cum, cum))
    if (result["status"] in TERMINAL and event["status"] in TERMINAL
            and result["status"] != event["status"]):
        block("TERMINAL_STATE_CONFLICT")
    if event["sequence"] > result["last_sequence"]:
        if result["status"] in TERMINAL and event["status"] not in TERMINAL:
            block("TERMINAL_STATE_REGRESSION")
        elif result["status"] != "FILLED":
            result["status"] = event["status"]
        result["last_sequence"] = event["sequence"]
    if cum == total and event["status"] == "FILLED":
        result["status"] = "FILLED"
    result["events"].append(deepcopy(event))
    if len(result["events"]) > 1000:
        raise PrimaryError("Offline event history limit; manual review required.")
    return result


class OfflineLedger:
    """Explicit snapshot writes only. OS lease serializes cooperating writers.

    Failed atomic replacement retains .tmp as a conservative recovery blocker.
    This is not a production event journal or account-wide coordination service.
    """
    def __init__(self, path, order_key):
        self.path = Path(path)
        self.order_key = order_key

    def load(self):
        if self.path.with_suffix(self.path.suffix + ".tmp").exists():
            raise PrimaryError("Interrupted ledger write; preserve evidence and reconcile.")
        if not self.path.exists():
            return initial_state(self.order_key)
        raw = read_journal(self.path)
        if (set(raw) != {"schema_version", "order_key", "events"}
                or type(raw["schema_version"]) is not int or raw["schema_version"] != 1
                or raw["order_key"] != self.order_key or not isinstance(raw["events"], list)
                or len(raw["events"]) > 1000):
            raise PrimaryError("Ledger version/context invalid.")
        state = initial_state(self.order_key)
        for event in raw["events"]:
            state = reduce_event(state, event)
        if raw["events"]:
            state["blockers"].append("RESTART_RECONCILIATION_REQUIRED")
        return state

    def record(self, event):
        with journal_lease(self.path.parent):
            state = reduce_event(self.load(), event)
            durable(self.path, {k: state[k] for k in ("schema_version", "order_key", "events")},
                    exclusive=not self.path.exists())
            return state


def reconnect(state):
    result = deepcopy(state)
    result["uncertain"] = True
    if "RECONNECT_RECONCILIATION_REQUIRED" not in result["blockers"]:
        result["blockers"].append("RECONNECT_RECONCILIATION_REQUIRED")
    return result

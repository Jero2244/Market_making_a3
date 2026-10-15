"""Read-only offline evidence requirements. No endpoint guessing or cancellation.

Only active/history/status order paths exist in the current demo client.
Positions, balances and margin adapters are deliberately absent pending specs.
"""
from decimal import Decimal
import re
from market_making.http_policy import HOST, ORDER_READ_PATHS, PrimaryError
from market_making.execution.demo_execution import identifier

REQUIRED = ("active_orders", "order_history", "positions", "balances", "margin")
DOCUMENTED_ORDER_PATHS = tuple(sorted(ORDER_READ_PATHS))


def validate_envelope(item, kind, expected_context):
    """Normalized fixture envelope only; validation never confers broker authority.

    Every account component must be from the same explicit capture context. The
    `complete` declaration still needs independently verified broker semantics.
    No timestamps, margin units, endpoints or completeness rules are inferred.
    """
    fields = {"schema_version", "kind", "context", "complete", "source_ref", "rows"}
    if (not isinstance(item, dict) or set(item) != fields or kind not in REQUIRED
            or type(item["schema_version"]) is not int or item["schema_version"] != 1
            or item["kind"] != kind or item["context"] != expected_context
            or item["complete"] is not True or not isinstance(item["source_ref"], str)
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", item["source_ref"])
            or not isinstance(item["rows"], list) or len(item["rows"]) > 10000):
        raise PrimaryError("Reconciliation fixture envelope invalid/incomplete.")
    if (not isinstance(expected_context, dict)
            or set(expected_context) != {"environment", "host", "account_tag", "capture_id"}
            or expected_context["environment"] != "REMARKETS" or expected_context["host"] != HOST
            or not isinstance(expected_context["account_tag"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_context["account_tag"])):
        raise PrimaryError("Reconciliation capture context invalid.")
    identifier(expected_context["capture_id"])
    for row in item["rows"]:
        # Minimal normalized records; broker field/unit mappings remain external.
        keys = {"order_key"} if kind in {"active_orders", "order_history"} else {"symbol", "quantity"} if kind == "positions" else {"currency", "amount"}
        if not isinstance(row, dict) or set(row) != keys:
            raise PrimaryError("Reconciliation normalized row invalid.")
        identifier(row["order_key"] if "order_key" in keys else row["symbol"] if "symbol" in keys else row["currency"])
        if kind not in {"active_orders", "order_history"}:
            value = row["quantity"] if kind == "positions" else row["amount"]
            try:
                if not isinstance(value, str) or len(value) > 64:
                    raise ValueError()
                amount = Decimal(value)
                if not amount.is_finite() or abs(amount.adjusted()) > 18:
                    raise ValueError()
            except (ArithmeticError, ValueError):
                raise PrimaryError("Reconciliation exact amount invalid.") from None
    return len(item["rows"])


def assess_evidence(evidence, expected_context=None):
    """Presence is not completeness or authority; fixtures cannot confer readiness."""
    blockers = []
    observations = {}
    for kind in REQUIRED:
        item = evidence.get(kind) if isinstance(evidence, dict) else None
        observations[kind] = "ABSENT" if item is None else "UNVERIFIED"
        if item is not None and expected_context is not None:
            try:
                validate_envelope(item, kind, expected_context)
                observations[kind] = "NORMALIZED_FIXTURE_VALID_NOT_BROKER_AUTHORITY"
            except PrimaryError:
                observations[kind] = "INVALID_OR_INCOMPLETE"
        blockers.append({"code": kind.upper() + "_AUTHORITY_UNVERIFIED",
                         "category": "missing_broker_evidence"})
    for kind in ("positions", "balances", "margin"):
        blockers.append({"code": kind.upper() + "_ADAPTER_MISSING", "category": "missing_code"})
    return {"status": "BLOCKED", "observations": observations, "blockers": blockers,
            "mutations_permitted": False, "unrelated_orders_cancelled": 0}

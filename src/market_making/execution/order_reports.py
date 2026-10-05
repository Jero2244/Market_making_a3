"""Non-authoritative account-scoped observations; never a REST status adapter."""
from decimal import Decimal, DecimalException
import hashlib
import hmac
import json
import secrets as random_secrets


STATES = frozenset({"PENDING_NEW", "NEW", "PARTIALLY_FILLED", "FILLED",
                    "CANCELLED", "REJECTED", "PENDING_CANCEL", "PENDING_REPLACE",
                    "REPLACED", "PENDING_APPROVAL", "EXPIRED"})
QUANTITIES = ("orderQty", "cumQty", "leavesQty", "lastQty")
MAX_FRAME_CHARACTERS = 1024 * 1024


class UnsupportedJSONNumber:
    """Typed, token-free marker for valid JSON numbers Decimal cannot represent.

    Never a string, quantity, identifier or retained server value. Conversion
    failure is local to the number, not a reason to discard correlated fills.
    """
    __slots__ = ()


UNSUPPORTED_JSON_NUMBER = UnsupportedJSONNumber()


def json_number(token):
    try:
        value = Decimal(token)
        # With InvalidOperation traps disabled, conversion can return NaN
        # instead of raising. Keep the same unsupported marker in either case.
        if value.is_finite():
            return value
    except (DecimalException, OverflowError, ValueError):
        pass
    return UNSUPPORTED_JSON_NUMBER


def contains_unsupported_number(value):
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, UnsupportedJSONNumber):
            return True
        if isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return False


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def constant(value):
        raise ValueError("Nonstandard JSON number")

    try:
        text = raw.decode("utf-8", errors="strict") if isinstance(raw, bytes) else raw
        if not isinstance(text, str) or len(text) > MAX_FRAME_CHARACTERS:
            return None
        result = json.loads(text, object_pairs_hook=pairs, parse_constant=constant,
                            parse_float=json_number, parse_int=json_number)
        return result if isinstance(result, dict) else None
    except (ValueError, TypeError, RecursionError):
        return None


def identifier(value):
    return (isinstance(value, str) and 0 < len(value) <= 256
            and value.isascii() and all(c.isalnum() or c in "._:/-" for c in value))


def quantity(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        return None
    # The frame size is the resource limit, not numeric magnitude/precision.
    # Decimal construction is exact and independent of the active context.
    if isinstance(value, str) and len(value) > MAX_FRAME_CHARACTERS:
        return None
    try:
        result = Decimal(value)
        if result.is_finite() and result >= 0:
            return result
    except (DecimalException, OverflowError, ValueError):
        pass
    return None


def sum_exceeds(left, right, total):
    """Exact nonnegative decimal sum comparison, without context rounding.

    Sparse base-ten digits avoid allocating exponent-sized buffers for e.g.
    1E+999999 + 1E-999999. Work/storage depends on supplied coefficient digits,
    bounded by the frame limit, rather than exponent separation.
    """
    def digits(value):
        parts = value.as_tuple()
        return {parts.exponent + index: digit
                for index, digit in enumerate(reversed(parts.digits)) if digit}

    summed = digits(left)
    for position, digit in digits(right).items():
        summed[position] = summed.get(position, 0) + digit
    # A carry can only reach the next position; handle it before the next
    # occupied position, without walking over empty exponent ranges.
    carry_position = None
    for position in sorted(summed):
        if carry_position is not None:
            summed[carry_position] = summed.get(carry_position, 0) + 1
            carry_position = None
        digit = summed[position]
        if digit >= 10:
            summed[position] = digit - 10
            carry_position = position + 1
    if carry_position is not None:
        summed[carry_position] = summed.get(carry_position, 0) + 1
    expected = digits(total)
    for position in sorted(summed.keys() | expected.keys(), reverse=True):
        actual_digit = summed.get(position, 0)
        expected_digit = expected.get(position, 0)
        if actual_digit != expected_digit:
            return actual_digit > expected_digit
    return False


class OrderReports:
    """Sticky evidence, not an order state machine or reconstruction of executions.

    IDs are salted digests. Cumulative quantities are maxima, never sums. Reports
    without usable identity still preserve correlated positive fill observations.
    """

    def __init__(self, account, secrets=(), salt=None):
        if not identifier(account):
            raise ValueError("Invalid account configuration")
        self.account = account
        self.secrets = secrets  # shared registry includes every renewed token
        self.salt = salt if salt is not None else random_secrets.token_bytes(32)
        self.generation = 0
        self.gaps = {"INITIAL_HISTORY_UNVERIFIED"}
        self.seen = set()
        self.orders = {}
        self.aliases = {}
        self.executions = {}
        self.qualifying = 0
        self.fill_observed = False

    def digest(self, value):
        return hmac.new(self.salt, value.encode("utf-8"), hashlib.sha256).hexdigest()

    def gap(self, code):
        self.gaps.add(code)

    def connected(self):
        self.generation += 1
        if self.generation > 1:
            self.gap("CONNECTION_GENERATION_GAP")

    def snapshot(self):
        return {"orders_sent": 0, "readiness": "unverified",
                "continuity": "unverified", "generation": self.generation,
                "gaps": sorted(self.gaps), "qualifying_reports": self.qualifying,
                "fill_observed": self.fill_observed,
                "authoritative_state": False}

    def observe(self, message):
        if not isinstance(message, dict) or message.get("type") != "or":
            return None
        report = message.get("orderReport")
        if (not isinstance(report, dict) or not isinstance(report.get("accountId"), dict)
                or report["accountId"].get("id") != self.account):
            self.gap("UNCORRELATED_REPORT")
            return {"event": "report_withheld", "anomalies": ["UNCORRELATED_REPORT"]}
        anomalies = []
        if contains_unsupported_number(message):
            anomalies.append("UNSUPPORTED_JSON_NUMBER")
        ids = {}
        for key in ("orderId", "clOrdId", "execId", "wsClOrdId"):
            if key in report:
                if identifier(report[key]):
                    ids[key] = self.digest(key + ":" + report[key])
                else:
                    anomalies.append("INVALID_" + key)
        if not any(k in ids for k in ("orderId", "clOrdId")):
            anomalies.append("MISSING_ORDER_IDENTITY")
        state = report.get("status")
        if not isinstance(state, str) or state not in STATES:
            state = None
            anomalies.append("INVALID_STATUS")
        quantities = {}
        numeric = {}
        for key in QUANTITIES:
            if key in report:
                value = quantity(report[key])
                if value is None:
                    anomalies.append("INVALID_" + key)
                    if isinstance(report[key], UnsupportedJSONNumber):
                        anomalies.append("UNSUPPORTED_" + key)
                else:
                    numeric[key] = value
                    text = str(value)
                    if any(s and s in text for s in (self.account, *self.secrets)):
                        anomalies.append("WITHHELD_" + key)
                    else:
                        quantities[key] = text
        fill = state in ("PARTIALLY_FILLED", "FILLED") or any(
            numeric.get(k, Decimal(0)) > 0 for k in ("cumQty", "lastQty"))
        self.fill_observed |= fill
        for key in ("avgPx", "lastPx", "price"):
            if key in report and quantity(report[key]) is None:
                anomalies.append("INVALID_" + key)
                if isinstance(report[key], UnsupportedJSONNumber):
                    anomalies.append("UNSUPPORTED_" + key)
        # Optional text, timestamps, symbols, prices and arbitrary metadata are
        # deliberately not retained, even when well formed.
        for key in ("transactTime", "text", "proprietary"):
            if key in report and not isinstance(report[key], str):
                anomalies.append("INVALID_" + key)
        if ("orderQty" in numeric and any(numeric.get(k, Decimal(0)) > numeric["orderQty"]
                                         for k in ("cumQty", "leavesQty", "lastQty"))):
            anomalies.append("QUANTITY_CONFLICT")
        if (state in ("PARTIALLY_FILLED", "FILLED") and numeric.get("cumQty") == 0):
            anomalies.append("FILL_QUANTITY_CONFLICT")
        if ("orderQty" in numeric and "cumQty" in numeric and "leavesQty" in numeric
                and sum_exceeds(numeric["cumQty"], numeric["leavesQty"], numeric["orderQty"])):
            anomalies.append("QUANTITY_CONFLICT")
        if ("cumQty" in numeric and "lastQty" in numeric
                and numeric["lastQty"] > numeric["cumQty"]):
            anomalies.append("QUANTITY_CONFLICT")
        if state == "FILLED" and (numeric.get("leavesQty", Decimal(0)) != 0
                or ("orderQty" in numeric and "cumQty" in numeric
                    and numeric["cumQty"] != numeric["orderQty"])):
            anomalies.append("FILL_QUANTITY_CONFLICT")
        if state in ("NEW", "PENDING_NEW", "REJECTED") and numeric.get("cumQty", Decimal(0)) > 0:
            anomalies.append("LIFECYCLE_QUANTITY_CONFLICT")
        if state in ("CANCELLED", "EXPIRED") and numeric.get("leavesQty", Decimal(0)) > 0:
            anomalies.append("LIFECYCLE_QUANTITY_CONFLICT")
        identity = ids.get("orderId") or ids.get("clOrdId")
        aliases = [ids[k] for k in ("orderId", "clOrdId") if k in ids]
        known = {self.aliases[a] for a in aliases if a in self.aliases}
        if len(known) > 1:
            anomalies.append("IDENTITY_CONFLICT")
            identity = None
        elif known:
            identity = next(iter(known))
        if identity:
            history = self.orders.setdefault(identity, {"ids": {}, "states": set(), "max_cum": Decimal(0)})
            if any(k in history["ids"] and history["ids"][k] != ids[k]
                   for k in ("orderId", "clOrdId") if k in ids):
                anomalies.append("IDENTITY_CONFLICT")
                identity = None
            else:
                history["ids"].update({k: v for k, v in ids.items() if k in ("orderId", "clOrdId")})
                for alias in aliases:
                    self.aliases[alias] = identity
                if history["states"] and state not in history["states"]:
                    anomalies.append("LIFECYCLE_ORDER_UNVERIFIED")
                if state:
                    history["states"].add(state)
                if numeric.get("cumQty", history["max_cum"]) < history["max_cum"]:
                    anomalies.append("CUMULATIVE_REGRESSION")
                history["max_cum"] = max(history["max_cum"], numeric.get("cumQty", Decimal(0)))
        sanitized = {"ids": ids, "status": state, "quantities": quantities, "fill_observed": fill}
        fingerprint = self.digest(json.dumps(sanitized, sort_keys=True))
        if "execId" in ids:
            previous = self.executions.setdefault(ids["execId"], fingerprint)
            if previous != fingerprint:
                anomalies.append("EXECUTION_CONFLICT")
        duplicate = fingerprint in self.seen
        self.seen.add(fingerprint)
        invalid_core = {"IDENTITY_CONFLICT", "EXECUTION_CONFLICT", "QUANTITY_CONFLICT",
                        "FILL_QUANTITY_CONFLICT", "LIFECYCLE_QUANTITY_CONFLICT",
                        "INVALID_orderId", "INVALID_clOrdId",
                        *("INVALID_" + k for k in QUANTITIES)}
        qualifying = bool(identity and state and not duplicate and not invalid_core.intersection(anomalies))
        if qualifying:
            self.qualifying += 1
        if anomalies:
            self.gap("REPORT_ANOMALY")
        return {"event": "report_observed", "generation": self.generation,
                **sanitized, "report_fill_observed": fill,
                "duplicate": duplicate, "qualifying": qualifying,
                "anomalies": sorted(set(anomalies)),
                "authoritative_state": False}

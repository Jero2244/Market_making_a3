"""Isolated REMARKETS order endpoints. Never used by the read-only clients.

Primary REST documentation: newSingleOrder, cancelById, id, actives, all.
Submission is a documented GET mutation: no HTTP retries or redirects permitted.
An HTTP/API OK is only an acknowledgement, NOT order acceptance.
"""
import json
import os
import re
import time
from pathlib import Path
import requests
from requests.adapters import HTTPAdapter
from check_connection import PrimaryError, safe_payload
from instrument_rules import MARKET, SYMBOL, number

HOST = "https://api.remarkets.primary.com.ar"
PATHS = frozenset({"/rest/order/newSingleOrder", "/rest/order/cancelById",
                   "/rest/order/id", "/rest/order/actives", "/rest/order/all"})
IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")


class MonitoringBlocked(PrimaryError):
    """No tested alternative is configured for accounts without historical IDs."""


class DemoProtocolError(PrimaryError):
    """Static code only; rejected server content must never appear in evidence."""
    def __init__(self, code):
        self.code = code
        super().__init__("Demo response protocol validation failed.")


def quantity_evidence(status, cum_value, leaves_value):
    """Return sanitized values plus consistency, never discard a fill status.

    This harness orders ONE indivisible contract. A partial fill is therefore a
    failure/protocol anomaly even if the broker reports contradictory zero cum.
    """
    try:
        cum, leaves = number(cum_value), number(leaves_value)
        valid = (0 <= cum <= 1 and 0 <= leaves <= 1 and not cum % 1 and not leaves % 1)
        if status in {"PENDING_NEW", "NEW"}:
            valid = valid and cum == 0 and leaves == 1
        elif status == "PENDING_CANCEL":
            valid = valid and cum + leaves == 1
        elif status == "PARTIALLY_FILLED":
            valid = valid and 0 < cum < 1 and leaves > 0 and cum + leaves == 1
        elif status == "FILLED":
            valid = valid and cum == 1 and leaves == 0
        elif status in {"CANCELLED", "EXPIRED"}:
            valid = valid and leaves == 0
        elif status == "REJECTED":
            valid = valid and cum == 0 and leaves == 0
        else:
            valid = False
        return {"cumQty": str(cum), "leavesQty": str(leaves), "quantities_valid": bool(valid)}
    except (PrimaryError, ArithmeticError, ValueError, TypeError):
        return {"cumQty": None, "leavesQty": None, "quantities_valid": False}


def identifier(value):
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise PrimaryError("Invalid order identifier.")
    value = str(value)
    if not IDENTIFIER.fullmatch(value):
        raise PrimaryError("Invalid order identifier.")
    return value


class DemoClient:
    def __init__(self, session, account):
        self.session = session
        self.account = identifier(account)
        # Dedicated CLI session; disable adapter retry machinery explicitly.
        session.mount("https://", HTTPAdapter(max_retries=0))

    def call(self, path, **params):
        if path not in PATHS:
            raise PrimaryError("Execution endpoint not allowed.")
        try:
            response = self.session.request("GET", HOST + path, params=params,
                                            timeout=(5, 10), allow_redirects=False)
        except requests.RequestException:
            raise PrimaryError("Demo request transport failed; outcome may be uncertain.") from None
        if not 200 <= response.status_code < 300:
            raise PrimaryError("Demo request HTTP failed; outcome may be uncertain.")
        try:
            payload = response.json()
        except ValueError:
            raise DemoProtocolError("RESPONSE_NOT_JSON") from None
        if not isinstance(payload, dict) or payload.get("status") != "OK":
            raise DemoProtocolError("RESPONSE_NOT_OK")
        if not safe_payload(payload, getattr(self.session, "_primary_secrets", ())):
            raise DemoProtocolError("SENSITIVE_RESPONSE_WITHHELD")
        return payload

    def orders(self, active=False):
        data = self.call("/rest/order/actives" if active else "/rest/order/all", accountId=self.account)
        orders = data.get("orders")
        if not isinstance(orders, list) or not all(isinstance(o, dict) for o in orders):
            raise PrimaryError("Account orders format unverified.")
        # Validate each returned account; no uncorrelated account results accepted.
        for order in orders:
            if account_id(order) != self.account:
                raise PrimaryError("Account order correlation failed.")
        return orders

    def submit(self, price):
        price = number(price)
        if price <= 0 or price % 100:
            raise PrimaryError("Submission must be positive and on the 100 grid.")
        data = self.call("/rest/order/newSingleOrder", marketId=MARKET, symbol=SYMBOL,
                         side="BUY", orderQty=1, ordType="LIMIT", price=str(price),
                         timeInForce="DAY", account=self.account,
                         cancelPrevious="false", iceberg="false")
        order = data.get("order")
        if not isinstance(order, dict):
            raise PrimaryError("Submission acknowledgement missing; uncertain.")
        return {"clOrdId": identifier(order.get("clientId")),
                "proprietary": identifier(order.get("proprietary"))}

    def cancel(self, ids):
        # Never cancel-all, replace, hedge or resubmit.
        self.call("/rest/order/cancelById", **ids)

    def monitor_ready(self):
        history = self.orders()
        if not history:
            raise MonitoringBlocked("No history: REST status monitoring unverified; no alternative configured.")
        sample = history[-1]
        ids = {"clOrdId": identifier(sample.get("clOrdId")),
               "proprietary": identifier(sample.get("proprietary"))}
        report = self.call("/rest/order/id", **ids).get("order")
        if (not isinstance(report, dict) or account_id(report) != self.account
                or any(str(report.get(k)) != v for k, v in ids.items())):
            raise PrimaryError("Monitoring correlation not proven.")

    def status(self, ids, price):
        payload = self.call("/rest/order/id", **ids)
        report = payload.get("order")
        if not isinstance(report, dict):
            raise DemoProtocolError("ORDER_REPORT_NOT_OBJECT")
        try:
            matched_ids = all(identifier(report.get(k)) == v for k, v in ids.items())
        except (PrimaryError, ArithmeticError, ValueError, TypeError):
            matched_ids = False
        if not matched_ids:
            raise DemoProtocolError("ORDER_ID_CORRELATION_FAILED")
        try:
            matched_account = account_id(report) == self.account
        except (PrimaryError, ArithmeticError, ValueError, TypeError):
            matched_account = False
        if not matched_account:
            raise DemoProtocolError("ORDER_ACCOUNT_CORRELATION_FAILED")
        # Core request IDs/account correlate the observation. Never let later
        # optional metadata or intent validation erase its fill evidence.
        anomalies = []
        raw_state = report.get("status")
        if isinstance(raw_state, str) and raw_state in {
                         "PENDING_NEW", "NEW", "PENDING_CANCEL", "PARTIALLY_FILLED",
                          "FILLED", "CANCELLED", "REJECTED", "EXPIRED"}:
            state = raw_state
        else:
            state = "UNKNOWN"
            anomalies.append("UNKNOWN_LIFECYCLE_STATE")
        if report.get("instrumentId") != {"marketId": MARKET, "symbol": SYMBOL}:
            anomalies.append("INSTRUMENT_MISMATCH")
        if (report.get("side") != "BUY" or report.get("ordType") != "LIMIT"
                or report.get("timeInForce") != "DAY"):
            anomalies.append("ORDER_INTENT_MISMATCH")
        try:
            numeric_match = number(report.get("orderQty")) == 1 and number(report.get("price")) == number(price)
        except (PrimaryError, ArithmeticError, ValueError, TypeError):
            numeric_match = False
        if not numeric_match:
            anomalies.append("ORDER_NUMERIC_INTENT_MISMATCH")
        order_id = None
        if report.get("orderId") is not None:
            try:
                order_id = identifier(report["orderId"])
            except (PrimaryError, ArithmeticError, ValueError, TypeError):
                anomalies.append("OPTIONAL_ORDER_ID_INVALID")
        return {**ids, "status": state,
                **quantity_evidence(state, report.get("cumQty"), report.get("leavesQty")),
                "orderId": order_id, "protocol_anomaly": bool(anomalies), "protocol_codes": anomalies}


def account_id(order):
    account = order.get("accountId", order.get("account"))
    if isinstance(account, dict):
        account = account.get("id")
    return identifier(account)


def durable(path, value, exclusive=False):
    """Atomic replace of flushed intent/state. No raw server reports or account numbers."""
    path = Path(path)
    if exclusive:
        with path.open("x", encoding="utf-8") as output:
            json.dump(value, output, indent=2)
            output.flush()
            os.fsync(output.fileno())
        return
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("x", encoding="utf-8") as output:
        json.dump(value, output, indent=2)
        output.flush()
        os.fsync(output.fileno())
    os.replace(tmp, path)


def cleanup(client, ids, price, journal, state, polls=5, sleep=time.sleep):
    """Cancel immediately, then prove cancelled/zero/absent. Errors never mean success."""
    cancel_error = False
    try:
        client.cancel(ids)
    except (Exception, KeyboardInterrupt):
        cancel_error = True
    if state.get("reconciliation_in_progress"):
        state["protocol_anomaly_observed"] = True
        state.setdefault("first_protocol_anomaly", {"code": "RECOVERY_OBSERVATION_CONTINUITY_UNPROVEN"})
    # Persist a write-ahead observation marker BEFORE reading any report. If a
    # later evidence write fails/crashes, restart cannot pretend no anomalous
    # report was observed. Cancellation above is still attempted on disk error.
    state["reconciliation_in_progress"] = True
    durable(journal, state)
    # Upgrade older journals too: a previously retained fill-status observation
    # must not vanish merely because an old version omitted the sticky flag.
    for previous in (state.get("initial_report"), state.get("last_report")):
        if isinstance(previous, dict) and previous.get("status") in {"PARTIALLY_FILLED", "FILLED"}:
            state["fill_observed"] = True
            observed = state.setdefault("fill_statuses_observed", [])
            if previous["status"] not in observed:
                observed.append(previous["status"])
    for _ in range(polls):
        try:
            report = client.status(ids, price)
            report = {**report, **quantity_evidence(report.get("status"),
                                                   report.get("cumQty"), report.get("leavesQty"))}
            state["last_report"] = report
            if report["status"] in {"PARTIALLY_FILLED", "FILLED"}:
                state["fill_observed"] = True
                state.setdefault("first_fill_report", report)
                observed = state.setdefault("fill_statuses_observed", [])
                if report["status"] not in observed:
                    observed.append(report["status"])
            if report["cumQty"] is not None and number(report["cumQty"]) > 0:
                state["fill_observed"] = True
                state.setdefault("first_fill_report", report)
            if not report["quantities_valid"]:
                state["quantity_anomaly_observed"] = True
                state.setdefault("first_quantity_anomaly", report)
            if report.get("protocol_anomaly"):
                state["protocol_anomaly_observed"] = True
                state.setdefault("first_protocol_anomaly", report)
            # An HTTP acknowledgement or CANCELLED report cannot prove that
            # the submitted order was accepted. Require a correlated, valid
            # NEW observation; never delay the immediate cancellation for it.
            if (report["status"] == "NEW" and report["quantities_valid"]
                    and not report.get("protocol_anomaly")):
                state["acceptance_proven"] = True
                state.setdefault("acceptance_report", report)
            if report["status"] in {"REJECTED", "EXPIRED", "FILLED"}:
                state["terminal_failure_observed"] = report["status"]
            durable(journal, state)
            if state.get("fill_observed"):
                state["result"] = "FAIL_FILL"
            elif state.get("quantity_anomaly_observed"):
                state["result"] = "FAIL_QUANTITY_ANOMALY"
            elif state.get("protocol_anomaly_observed"):
                state["result"] = "FAIL_PROTOCOL_ANOMALY"
            elif state.get("terminal_failure_observed"):
                state["result"] = "FAIL_" + state["terminal_failure_observed"]
            # Continue bounded reconciliation after a failed observation: a
            # later cancellation can prove cleanup, NEVER erase the failure.
            if report["status"] == "CANCELLED" and report["quantities_valid"]:
                active = client.orders(active=True)
                if any(str(o.get("clOrdId")) == ids["clOrdId"] for o in active):
                    raise PrimaryError("Cancelled order still active.")
                state["cleanup_proven"] = True
                state.setdefault("result", "PASS_CANCELLED_ZERO")
                # Old blocked results must not suppress a newly proven clean
                # zero cancellation, but persisted failures always dominate.
                if not (state.get("fill_observed") or state.get("quantity_anomaly_observed")
                        or state.get("protocol_anomaly_observed")
                        or state.get("terminal_failure_observed")
                        or str(state.get("result", "")).startswith("FAIL_")):
                    if state.get("evidence_gap_observed"):
                        state["result"] = "BLOCKED_EVIDENCE_GAP"
                    elif not state.get("acceptance_proven"):
                        state["result"] = "BLOCKED_ACCEPTANCE_UNPROVEN"
                    else:
                        state["result"] = "PASS_CANCELLED_ZERO"
                completed_state = {**state, "reconciliation_in_progress": False}
                durable(journal, completed_state)
                state.update(completed_state)
                return state["result"] == "PASS_CANCELLED_ZERO"
            if (report["status"] in {"REJECTED", "EXPIRED", "FILLED"}
                    and report["quantities_valid"] and not report.get("protocol_anomaly")):
                state["result"] = "FAIL_" + report["status"]
                if state.get("fill_observed"):
                    state["result"] = "FAIL_FILL"
                break
        except DemoProtocolError as exc:
            state["protocol_anomaly_observed"] = True
            state.setdefault("first_protocol_anomaly", {"code": exc.code})
            durable(journal, state)
        except (Exception, KeyboardInterrupt):
            # A lost report/storage/reconciliation observation is sticky. A
            # later zero cancellation proves cleanup, not a successful smoke.
            state["evidence_gap_observed"] = True
        sleep(0.2)
    if state.get("fill_observed"):
        state["result"] = "FAIL_FILL"
    elif state.get("quantity_anomaly_observed"):
        state["result"] = "FAIL_QUANTITY_ANOMALY"
    elif state.get("protocol_anomaly_observed"):
        state["result"] = "FAIL_PROTOCOL_ANOMALY"
    elif not str(state.get("result", "")).startswith("FAIL_"):
        state["result"] = "BLOCKED_UNCERTAIN_CANCEL" if cancel_error else "BLOCKED_UNPROVEN_TERMINAL"
    completed_state = {**state, "reconciliation_in_progress": False}
    durable(journal, completed_state)
    state.update(completed_state)
    return False


def lifecycle(client, price, journal, recheck, final_check, sleep=time.sleep):
    """At most ONE submission. Persist submit intent before touching the network."""
    state = {"phase": "prepared", "symbol": SYMBOL, "side": "BUY", "qty": 1,
             "ordType": "LIMIT", "timeInForce": "DAY", "price": str(price)}
    durable(journal, state, exclusive=True)
    recheck()
    state["phase"] = "submit_intent"
    durable(journal, state)
    # The durable intent write can be slow. The final check must be local,
    # rechecking current session and the SAME recheck snapshot's elapsed age.
    # No network or persistence is permitted between this check and submit.
    try:
        final_check()
    except (Exception, KeyboardInterrupt):
        state.update(phase="aborted_no_send", result="BLOCKED_FINAL_SEND_GATE",
                     orders_sent=0)
        durable(journal, state)
        return False
    ids = None
    completed = False
    try:
        ids = client.submit(price)
        state.update(phase="acknowledged", ids=ids)
        durable(journal, state)
        # Monitoring access was proven by preflight. Do not wait for NEW before
        # cancelling: immediately enter finally; poll only AFTER the cancel.
    except (Exception, KeyboardInterrupt):
        state["result"] = "BLOCKED_UNCERTAIN_SUBMIT" if ids is None else "BLOCKED_REPORT_FAILURE"
        state["evidence_gap_observed"] = True
    finally:
        if ids is not None:
            completed = cleanup(client, ids, price, journal, state, sleep=sleep)
        else:
            durable(journal, state)
    return completed

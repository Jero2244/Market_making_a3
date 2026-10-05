import json
import unittest
from decimal import Decimal, Inexact, InvalidOperation, localcontext
from fractions import Fraction
import random
from market_making.execution.order_reports import (OrderReports, strict_json, quantity,
                                                   sum_exceeds, MAX_FRAME_CHARACTERS,
                                                   UnsupportedJSONNumber, identifier)


def report(**changes):
    body = {"accountId": {"id": "DEMO_ACCOUNT"}, "orderId": "order-1",
            "clOrdId": "client-1", "status": "NEW", "orderQty": 100,
            "cumQty": 0, "leavesQty": 100}
    body.update(changes)
    if isinstance(body["cumQty"], int) and not isinstance(body["cumQty"], bool) and body["cumQty"] >= 0:
        if "leavesQty" not in changes:
            body["leavesQty"] = max(0, body["orderQty"] - body["cumQty"])
        if "status" not in changes and body["cumQty"] > 0:
            body["status"] = "PARTIALLY_FILLED"
    if body["status"] in ("CANCELLED", "FILLED", "EXPIRED") and "leavesQty" not in changes:
        body["leavesQty"] = 0
    return {"type": "or", "orderReport": body}


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.reports = OrderReports("DEMO_ACCOUNT", ["USER_SECRET", "PASS_SECRET", "TOKEN_SECRET"], salt=b"offline")
        self.reports.connected()

    def test_strict_invalid_frames(self):
        for raw in (b"\xff", '{"type":"or","type":"or"}',
                    '{"x":{"a":1,"a":2}}', '{"x":NaN}', '{"x":Infinity}',
                    '{"x":-Infinity}', '[]', 'null', None, '{'):
            with self.subTest(raw=raw):
                self.assertIsNone(strict_json(raw))

    def test_strict_decimal_and_large_account_quantity(self):
        self.assertEqual(strict_json('{"x":1.25}')['x'], Decimal("1.25"))
        event = self.reports.observe(report(cumQty=50, lastQty=10, status="PARTIALLY_FILLED"))
        self.assertEqual(event["quantities"]["cumQty"], "50")
        self.assertTrue(event["fill_observed"])
        self.assertEqual(self.reports.qualifying, 1)

    def test_account_requires_exact_correlation(self):
        for account in ({"id": "OTHER"}, "DEMO_ACCOUNT", {"id": 1}, None):
            event = self.reports.observe(report(accountId=account, cumQty=10))
            self.assertEqual(event["event"], "report_withheld")
        self.assertFalse(self.reports.fill_observed)
        self.assertEqual(self.reports.qualifying, 0)
        self.assertIn("UNCORRELATED_REPORT", self.reports.gaps)

    def test_optional_malformed_fields_cannot_erase_fill(self):
        event = self.reports.observe(report(cumQty=25, lastQty=5, avgPx="bad", text={},
                                            transactTime=[], proprietary=None))
        self.assertTrue(event["fill_observed"])
        self.assertIn("INVALID_avgPx", event["anomalies"])
        self.assertIn("INVALID_text", event["anomalies"])
        self.assertTrue(self.reports.fill_observed)

    def test_bad_status_and_identity_still_preserve_correlated_fill(self):
        event = self.reports.observe(report(orderId={}, clOrdId=None, status="secret unknown", cumQty=25))
        self.assertTrue(event["fill_observed"])
        self.assertIsNone(event["status"])
        self.assertEqual(event["ids"], {})
        self.assertEqual(self.reports.qualifying, 0)
        self.assertIn("MISSING_ORDER_IDENTITY", event["anomalies"])

    def test_quantities_reject_bool_negative_nonfinite_invalid(self):
        for value in (True, False, -1, "-2", "nan", "Infinity", {}, [], None, "bad"):
            self.assertIsNone(quantity(value), str(value))
            event = self.reports.observe(report(cumQty=value, lastQty=2))
            self.assertTrue(event["fill_observed"])
            self.assertIn("INVALID_cumQty", event["anomalies"])

    def test_duplicates_and_reordered_cancel_never_sum_or_erase(self):
        first = report(status="PARTIALLY_FILLED", cumQty=25, lastQty=5)
        self.reports.observe(first)
        event = self.reports.observe(first)
        self.assertTrue(event["duplicate"])
        self.assertEqual(self.reports.qualifying, 1)
        cancelled = self.reports.observe(report(status="CANCELLED", cumQty=25))
        self.assertIn("LIFECYCLE_ORDER_UNVERIFIED", cancelled["anomalies"])
        event = self.reports.observe(report(cumQty=0))
        self.assertIn("CUMULATIVE_REGRESSION", event["anomalies"])
        history = next(iter(self.reports.orders.values()))
        self.assertEqual(history["max_cum"], 25)
        self.assertTrue(self.reports.snapshot()["fill_observed"])
        self.assertFalse(self.reports.snapshot()["authoritative_state"])

    def test_conflicting_identity_and_execution(self):
        self.reports.observe(report(execId="exec-1", cumQty=10))
        event = self.reports.observe(report(execId="exec-1", cumQty=20))
        self.assertIn("EXECUTION_CONFLICT", event["anomalies"])
        event = self.reports.observe(report(clOrdId="another-client", cumQty=25))
        self.assertIn("IDENTITY_CONFLICT", event["anomalies"])
        self.assertTrue(self.reports.fill_observed)

    def test_secret_safe_allowlist(self):
        event = self.reports.observe(report(orderId="TOKEN_SECRET", clOrdId="PASS_SECRET",
            execId="USER_SECRET", wsClOrdId="DEMO_ACCOUNT", text="TOKEN_SECRET",
            instrumentId={"symbol": "PASS_SECRET"}, transactTime="USER_SECRET",
            arbitrary={"password": "PASS_SECRET"}))
        encoded = json.dumps(event)
        for secret in ("TOKEN_SECRET", "PASS_SECRET", "USER_SECRET", "DEMO_ACCOUNT"):
            self.assertNotIn(secret, encoded)
        self.assertNotIn("raw", event)
        self.assertNotIn("text", event)
        self.assertTrue(all(len(v) == 64 for v in event["ids"].values()))

    def test_generations_and_gaps_sticky(self):
        self.reports.observe(report(status="FILLED", cumQty=100))
        self.reports.gap("INVALID_JSON")
        self.reports.connected()
        self.reports.observe(report())
        summary = self.reports.snapshot()
        self.assertEqual(summary["generation"], 2)
        self.assertIn("INVALID_JSON", summary["gaps"])
        self.assertIn("CONNECTION_GENERATION_GAP", summary["gaps"])
        self.assertTrue(summary["fill_observed"])
        self.assertEqual(summary["readiness"], "unverified")

    def test_inconsistent_quantities_do_not_manufacture_fill(self):
        event = self.reports.observe(report(orderQty=5, leavesQty=100))
        self.assertIn("QUANTITY_CONFLICT", event["anomalies"])
        self.assertFalse(event["fill_observed"])
        event = self.reports.observe(report(status="FILLED", cumQty=0))
        self.assertTrue(event["fill_observed"])
        self.assertEqual(event["quantities"]["cumQty"], "0")
        self.assertIn("FILL_QUANTITY_CONFLICT", event["anomalies"])

    def test_control_and_market_data_not_reports(self):
        for msg in ({"type": "heartbeat"}, {"type": "Md"}, None):
            self.assertIsNone(self.reports.observe(msg))
        self.assertEqual(self.reports.qualifying, 0)

    def test_invalid_quantities_not_qualifying_and_numeric_secret_withheld(self):
        event = self.reports.observe(report(cumQty=-1, lastQty=5))
        self.assertFalse(event["qualifying"])
        self.assertTrue(event["fill_observed"])
        self.assertEqual(self.reports.qualifying, 0)
        reports = OrderReports("DEMO_ACCOUNT", ["12345"])
        event = reports.observe(report(cumQty=12345, orderQty=20000))
        self.assertNotIn("12345", json.dumps(event))
        self.assertTrue(event["fill_observed"])
        self.assertIn("WITHHELD_cumQty", event["anomalies"])

    def test_disjoint_order_aliases_cannot_be_merged(self):
        self.reports.observe(report())
        self.reports.observe(report(orderId="order-2", clOrdId="client-2"))
        event = self.reports.observe(report(orderId="order-1", clOrdId="client-2", cumQty=5))
        self.assertIn("IDENTITY_CONFLICT", event["anomalies"])
        self.assertTrue(event["fill_observed"])
        self.assertFalse(event["qualifying"])

    def test_large_and_tiny_positive_quantities_preserve_fill_with_new_status(self):
        for value in ("1e101", "1e-101", "9" * 129, "1e999999", "1e-999999"):
            with self.subTest(value=value):
                observer = OrderReports("DEMO_ACCOUNT")
                event = observer.observe(report(status="NEW", orderQty=value, cumQty=value,
                                                lastQty=value, leavesQty="0"))
                self.assertEqual(quantity(value), Decimal(value))
                self.assertTrue(event["report_fill_observed"])
                self.assertTrue(observer.snapshot()["fill_observed"])
                self.assertEqual(Decimal(event["quantities"]["cumQty"]), Decimal(value))
                self.assertIn("LIFECYCLE_QUANTITY_CONFLICT", event["anomalies"])
                self.assertNotIn("INVALID_cumQty", event["anomalies"])

    def test_large_unquoted_json_integer_within_payload_limit(self):
        integer = "9" * 5000  # Above Python's default int-string conversion limit.
        raw = ('{"type":"or","orderReport":{"accountId":{"id":"DEMO_ACCOUNT"},'
               '"orderId":"one","status":"NEW","cumQty":' + integer + '}}')
        message = strict_json(raw)
        self.assertIsNotNone(message)
        event = self.reports.observe(message)
        self.assertTrue(event["report_fill_observed"])
        self.assertEqual(event["quantities"]["cumQty"], integer)
        self.assertIsNone(strict_json(" " * (MAX_FRAME_CHARACTERS + 1)))
        self.assertIsNone(quantity("1" * (MAX_FRAME_CHARACTERS + 1)))

    def test_high_precision_consistency_without_decimal_context_rounding(self):
        base = "1" + "0" * 128
        for cum, leaves, total, conflict in (
                (base, "1", base, True),
                ("1e101", "1e-101", "1e101", True),
                ("1e999999", "1e-999999", "1e999999", True),
                ("9" * 129, "1", "1e129", False),
                ("0.00000000000000000000000000001", "0", "1e-29", False)):
            with self.subTest(cum=cum, leaves=leaves, total=total), localcontext() as context:
                context.prec = 2
                context.traps[Inexact] = True
                context.clear_flags()
                observer = OrderReports("DEMO_ACCOUNT")
                event = observer.observe(report(status="PARTIALLY_FILLED", cumQty=cum,
                                                leavesQty=leaves, orderQty=total))
                self.assertEqual("QUANTITY_CONFLICT" in event["anomalies"], conflict)
                self.assertTrue(event["fill_observed"])
                self.assertEqual(sum_exceeds(Decimal(cum), Decimal(leaves), Decimal(total)), conflict)
                self.assertFalse(context.flags[Inexact])

    def test_sparse_sum_comparison_matches_exact_rationals(self):
        rng = random.Random(42)
        for _ in range(500):
            values = [Decimal(f"{rng.randrange(100000)}e{rng.randrange(-50, 51)}") for _ in range(3)]
            left, right, total = values
            self.assertEqual(sum_exceeds(left, right, total),
                             Fraction(left) + Fraction(right) > Fraction(total))

    def test_extreme_optional_numeric_tokens_preserve_fill(self):
        tokens = ("1e9999999999999999999", "1e-9999999999999999999",
                  "-1e9999999999999999999")
        for token in tokens:
            with self.subTest(token=token):
                raw = json.dumps(report(cumQty=20, avgPx="EXTREME_TOKEN"))
                raw = raw.replace('"EXTREME_TOKEN"', token)
                message = strict_json(raw)
                self.assertIsNotNone(message)
                self.assertIsInstance(message["orderReport"]["avgPx"], UnsupportedJSONNumber)
                event = OrderReports("DEMO_ACCOUNT").observe(message)
                self.assertTrue(event["report_fill_observed"])
                self.assertEqual(event["quantities"]["cumQty"], "20")
                self.assertTrue(event["qualifying"])
                self.assertIn("UNSUPPORTED_avgPx", event["anomalies"])
                self.assertIn("UNSUPPORTED_JSON_NUMBER", event["anomalies"])
                self.assertNotIn(token, json.dumps(event))

    def test_extreme_nested_and_envelope_numbers_are_optional_anomalies(self):
        for where in ("report", "envelope"):
            with self.subTest(where=where):
                message = report(cumQty=20)
                target = message["orderReport"] if where == "report" else message
                target["unknown_metadata"] = {"secret_server_key": ["EXTREME_TOKEN", {"another": "EXTREME_TOKEN"}]}
                raw = json.dumps(message).replace('"EXTREME_TOKEN"', '1e9999999999999999999')
                event = OrderReports("DEMO_ACCOUNT").observe(strict_json(raw))
                self.assertTrue(event["fill_observed"])
                self.assertTrue(event["qualifying"])
                self.assertIn("UNSUPPORTED_JSON_NUMBER", event["anomalies"])
                for withheld in ("secret_server_key", "another", "9999999999999999999"):
                    self.assertNotIn(withheld, json.dumps(event))

    def test_unsupported_core_quantity_is_distinct_and_never_invents_fill(self):
        raw = json.dumps(report(status="NEW", cumQty="EXTREME_TOKEN", leavesQty=100))
        event = OrderReports("DEMO_ACCOUNT").observe(strict_json(
            raw.replace('"EXTREME_TOKEN"', '1e9999999999999999999')))
        self.assertFalse(event["fill_observed"])
        self.assertFalse(event["qualifying"])
        self.assertNotIn("cumQty", event["quantities"])
        self.assertIn("UNSUPPORTED_cumQty", event["anomalies"])
        invalid = OrderReports("DEMO_ACCOUNT").observe(report(status="NEW", cumQty="not-a-number"))
        self.assertIn("INVALID_cumQty", invalid["anomalies"])
        self.assertNotIn("UNSUPPORTED_cumQty", invalid["anomalies"])
        raw = json.dumps(report(cumQty=20, lastQty="EXTREME_TOKEN"))
        observer = OrderReports("DEMO_ACCOUNT")
        event = observer.observe(strict_json(raw.replace('"EXTREME_TOKEN"', '1e9999999999999999999')))
        self.assertTrue(event["fill_observed"])
        self.assertIn("UNSUPPORTED_lastQty", event["anomalies"])
        self.assertNotIn("lastQty", event["quantities"])
        self.assertFalse(event["qualifying"])
        observer.observe(report(status="CANCELLED", cumQty=0))
        self.assertTrue(observer.snapshot()["fill_observed"])

    def test_numeric_identifiers_are_not_coerced_to_strings(self):
        for token in ("20", "1e101", "1e9999999999999999999"):
            with self.subTest(token=token):
                raw = json.dumps(report(orderId="NUMBER_TOKEN", clOrdId="NUMBER_TOKEN",
                                        execId="NUMBER_TOKEN", wsClOrdId="NUMBER_TOKEN", cumQty=20))
                message = strict_json(raw.replace('"NUMBER_TOKEN"', token))
                value = message["orderReport"]["orderId"]
                self.assertFalse(identifier(value))
                event = OrderReports("DEMO_ACCOUNT").observe(message)
                self.assertEqual(event["ids"], {})
                self.assertFalse(event["qualifying"])
                self.assertTrue(event["fill_observed"])
                for key in ("orderId", "clOrdId", "execId", "wsClOrdId"):
                    self.assertIn("INVALID_" + key, event["anomalies"])
        raw = json.dumps(report(accountId={"id": "NUMBER_TOKEN"}, cumQty=20))
        observer = OrderReports("DEMO_ACCOUNT")
        event = observer.observe(strict_json(raw.replace('"NUMBER_TOKEN"', '1e9999999999999999999')))
        self.assertEqual(event["event"], "report_withheld")
        self.assertFalse(observer.fill_observed)

    def test_unsupported_conversion_with_decimal_traps_disabled(self):
        with localcontext() as context:
            context.traps[InvalidOperation] = False
            raw = json.dumps(report(cumQty=20, price="EXTREME_TOKEN"))
            message = strict_json(raw.replace('"EXTREME_TOKEN"', '1e9999999999999999999'))
            self.assertIsInstance(message["orderReport"]["price"], UnsupportedJSONNumber)
            event = self.reports.observe(message)
            self.assertTrue(event["fill_observed"])
            self.assertIn("UNSUPPORTED_price", event["anomalies"])
            self.assertIsNone(quantity("1e9999999999999999999"))


if __name__ == "__main__":
    unittest.main()

"""All transports offline; synthetic ages exercise mechanics, never provenance."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from market_making.market_data.check_connection import PrimaryError
from market_making.market_data.order_book import parse_snapshot
from market_making.execution.smoke_demo_order import final_send_gate, session_gate, timestamp_gate

NOW = datetime(2026, 10, 2, 15, tzinfo=timezone.utc)


def payload(stamp):
    return {"status": "OK", "instrumentId": {"marketId": "ROFX", "symbol": "RFX20/OCT26"},
            "timestamp": stamp, "marketData": {"BI": [{"price": 440000, "size": 1}],
                                              "OF": [{"price": 441000, "size": 1}]}}


class FreshnessTests(unittest.TestCase):
    def test_all_unverified_timestamps_block_even_with_attestation(self):
        stamps = [None, NOW.isoformat(), (NOW-timedelta(seconds=30)).isoformat(),
                  (NOW+timedelta(seconds=1)).isoformat(), "malformed", "2026-10-02T15:00:00",
                  float("nan"), float("inf"), float("-inf"), NOW.timestamp()*1000, True, {}, []]
        for stamp in stamps:
            with self.subTest(stamp=stamp):
                book = parse_snapshot(payload(stamp), NOW, received_monotonic=0)
                self.assertIsNone(book.quote_age)
                self.assertFalse(book.summary()["timestamp_authoritative"])
                with self.assertRaises(PrimaryError):
                    replace(book, clock_uncertainty=0).gate(NOW, monotonic_now=0)
                with self.assertRaises(PrimaryError):
                    final_send_gate({"independently_reviewed": True,
                                     "server_snapshot_timestamp_verified": True}, book)

    def test_diagnostic_age_not_authoritative_and_no_numeric_unit_guess(self):
        self.assertEqual(parse_snapshot(payload(NOW.isoformat()), NOW).timestamp_age, 0)
        self.assertIsNone(parse_snapshot(payload(NOW.timestamp()*1000), NOW).timestamp_age)
        p = payload(None)
        p["marketData"]["LA"] = {"date": NOW.isoformat()}
        p["Date"] = NOW.isoformat()
        p["heartbeat"] = NOW.isoformat()
        self.assertIsNone(parse_snapshot(p, NOW).quote_age)

    def test_synthetic_clock_age_boundaries(self):
        base = replace(parse_snapshot(payload(NOW.isoformat()), NOW, received_monotonic=0),
                       quote_age=0, clock_uncertainty=0, timestamp_authoritative=True)
        base.gate(NOW+timedelta(seconds=5), monotonic_now=5)
        for age, elapsed, uncertainty, wall in [(0, 5.000001, 0, 5.000001),
                (0, 5, .01, 5), (-1, 0, 0, 0), (float("nan"), 0, 0, 0),
                (float("inf"), 0, 0, 0), (0, 0, None, 0), (0, 0, float("nan"), 0),
                (0, -1, 0, 0), (0, 2, 0, 1), (0, 1, 0, -1)]:
            with self.subTest(age=age, elapsed=elapsed, uncertainty=uncertainty, wall=wall), self.assertRaises(PrimaryError):
                replace(base, quote_age=age, clock_uncertainty=uncertainty).gate(
                    NOW+timedelta(seconds=wall), monotonic_now=elapsed)

    def test_naive_receipt_rejected(self):
        with self.assertRaises(PrimaryError):
            parse_snapshot(payload(None), NOW.replace(tzinfo=None))

    def test_synthetic_nonzero_age_elapsed_and_uncertainty_share_budget(self):
        # Test mechanics only: no production adapter sets these fields.
        book = replace(parse_snapshot(payload(NOW.isoformat()), NOW, received_monotonic=0),
                       quote_age=2, clock_uncertainty=1, timestamp_authoritative=True)
        book.gate(NOW+timedelta(seconds=2), monotonic_now=2)
        with self.assertRaises(PrimaryError):
            book.gate(NOW+timedelta(seconds=2.000001), monotonic_now=2.000001)
        for uncertainty in (-1, float("inf"), float("-inf")):
            with self.subTest(uncertainty=uncertainty), self.assertRaises(PrimaryError):
                replace(book, clock_uncertainty=uncertainty).gate(NOW, monotonic_now=0)

    def test_either_missing_side_blocks_even_synthetic_authority(self):
        for side in ("BI", "OF"):
            for explicit_empty in (True, False):
                p = payload(NOW.isoformat())
                if explicit_empty:
                    p["marketData"][side] = []
                else:
                    del p["marketData"][side]
                book = replace(parse_snapshot(p, NOW, received_monotonic=0),
                               quote_age=0, clock_uncertainty=0, timestamp_authoritative=True)
                with self.subTest(side=side, explicit_empty=explicit_empty), self.assertRaises(PrimaryError):
                    book.gate(NOW, monotonic_now=0)

    def test_hard_timestamp_block_survives_synthetic_book_and_review_flags(self):
        book = replace(parse_snapshot(payload(NOW.isoformat()), NOW, received_monotonic=0),
                       quote_age=0, clock_uncertainty=0, timestamp_authoritative=True)
        evidence = {"independently_reviewed": True, "server_snapshot_timestamp_verified": True,
                    "trading_day": True, "remarkets_session_confirmed": True}
        with self.assertRaisesRegex(PrimaryError, "No supported authoritative"):
            final_send_gate(evidence, book)

    def test_final_cached_evidence_has_no_filesystem_io(self):
        book = replace(parse_snapshot(payload(NOW.isoformat()), NOW, received_monotonic=0),
                       quote_age=0, clock_uncertainty=0, timestamp_authoritative=True)
        with patch("market_making.execution.smoke_demo_order.timestamp_gate"), patch("pathlib.Path.read_text", side_effect=AssertionError("IO")), \
                patch("market_making.market_data.order_book.time.monotonic", return_value=0), patch("market_making.market_data.order_book.datetime") as clock:
            clock.now.return_value = NOW
            final_send_gate({"independently_reviewed": True}, book)
            with self.assertRaises(PrimaryError):
                final_send_gate({}, book)

    def test_demo_247_calendar_decoupled_review_and_freshness_still_block(self):
        for now in [datetime(2026, 10, 3, 3, tzinfo=timezone.utc),
                    datetime(2026, 10, 4, 7, tzinfo=timezone.utc),
                    datetime(2026, 12, 25, 3, tzinfo=timezone.utc),
                    datetime(2026, 10, 2, 20, tzinfo=timezone.utc)]:
            with self.subTest(now=now):
                session_gate({"independently_reviewed": True, "date": "old",
                              "trading_day": False, "remarkets_session_confirmed": False}, now)
                with self.assertRaises(PrimaryError):
                    session_gate({}, now)
                with self.assertRaises(PrimaryError):
                    timestamp_gate({"server_snapshot_timestamp_verified": True})


if __name__ == "__main__":
    unittest.main()

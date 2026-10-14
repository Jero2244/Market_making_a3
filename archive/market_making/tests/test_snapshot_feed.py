import copy
from contextlib import redirect_stderr
from io import StringIO
from decimal import Decimal
import unittest
from unittest.mock import patch

from market_making.market_data.check_connection import PrimaryError
from market_making.market_data.order_book import parse_snapshot
from market_making.market_data.snapshot_feed import Contract, SnapshotFeed, normalize_book, main


CONTRACT = Contract("ROFX", "DLR/DIC26", Decimal("0.5"), Decimal("1"))


def payload():
    return {"status": "OK", "marketData": {
        "BI": [{"price": 100, "size": 2}, {"price": 101, "size": 1}],
        "OF": [{"price": 103, "size": 3}, {"price": 102, "size": 2}]}}


class SnapshotTests(unittest.TestCase):
    def test_exact_detail_grid_and_future_required(self):
        detail = {"instrument": {"instrumentId": {"marketId": "ROFX", "symbol": "DLR/DIC26"},
                  "cficode": "FXXXSX", "minPriceIncrement": 0.5, "roundLot": 1}}
        self.assertEqual(Contract.from_detail(detail, "ROFX", "DLR/DIC26"), CONTRACT)
        for key, value in (("cficode", "OCAFXS"), ("minPriceIncrement", None),
                           ("roundLot", 0), ("tickSize", [{"tick": 1}])):
            bad = copy.deepcopy(detail)
            bad["instrument"][key] = value
            with self.subTest(key=key), self.assertRaises(PrimaryError):
                Contract.from_detail(bad, "ROFX", "DLR/DIC26")
        with self.assertRaises(PrimaryError):
            Contract.from_detail(detail, "ROFX", "DLR/OCT26")

    def test_sorted_complete_books_and_two_contract_grids(self):
        bids, asks, state = normalize_book(payload(), CONTRACT)
        self.assertEqual(state, "valid")
        self.assertEqual(bids[0][0], 101)
        self.assertEqual(asks[0][0], 102)
        with self.assertRaises(PrimaryError):
            normalize_book(payload(), Contract("ROFX", "RFX20/OCT26", Decimal(100), Decimal(1)))

    def test_invalid_missing_duplicate_crossed_and_empty_sides(self):
        for value in (None, {}, "OK", []):
            with self.subTest(value=value), self.assertRaises(PrimaryError):
                normalize_book(value, CONTRACT)
        for bad in (None, {}, [{"price": True, "size": 1}], [{"price": 100, "size": 0}],
                    [{"price": "NaN", "size": 1}], [{"price": 100.1, "size": 1}],
                    [{"price": 100, "size": 0.5}], [{"price": 100, "size": 1}] * 2):
            p = payload()
            p["marketData"]["BI"] = bad
            with self.subTest(bad=bad), self.assertRaises(PrimaryError):
                normalize_book(p, CONTRACT)
        p = payload()
        del p["marketData"]["BI"]
        with self.assertRaises(PrimaryError):
            normalize_book(p, CONTRACT)
        p["marketData"]["BI"] = []
        self.assertEqual(normalize_book(p, CONTRACT)[2], "one_sided")
        p["marketData"]["OF"] = []
        self.assertEqual(normalize_book(p, CONTRACT)[2], "empty")
        p = payload()
        p["marketData"]["BI"][0]["price"] = 102
        self.assertEqual(normalize_book(p, CONTRACT)[2], "crossed_or_locked")
        p["instrumentId"] = {"marketId": "ROFX", "symbol": "OTHER"}
        with self.assertRaises(PrimaryError):
            normalize_book(p, CONTRACT)

    def test_refresh_failure_discards_book_and_recovery_changes_generation(self):
        values = iter([payload(), PrimaryError("failure"), payload()])
        feed = None

        def fetch(contract, timeout):
            self.assertEqual(feed.snapshot()["bids"], [])
            value = next(values)
            if isinstance(value, Exception):
                raise value
            return value

        feed = SnapshotFeed(CONTRACT, fetch, clock=lambda: 0)
        self.assertFalse(feed.snapshot()["usable_for_shadow"])
        self.assertTrue(feed.refresh()["usable_for_shadow"])
        self.assertEqual(feed.generation, 1)
        with self.assertRaises(PrimaryError):
            feed.refresh()
        self.assertEqual(feed.snapshot()["bids"], [])
        self.assertFalse(feed.snapshot()["usable_for_shadow"])
        self.assertEqual(feed.refresh()["generation"], 2)
        feed.disconnect()
        self.assertEqual(feed.snapshot()["asks"], [])

    def test_age_includes_acquisition_and_no_exchange_age_fabricated(self):
        now = [0.0]

        def fetch(contract, timeout):
            now[0] += 2
            return payload()

        feed = SnapshotFeed(CONTRACT, fetch, clock=lambda: now[0], max_request=2, max_age=5)
        result = feed.refresh()
        self.assertEqual(result["request_seconds"], 2)
        self.assertIsNone(result["exchange_quote_age_seconds"])
        now[0] = 5
        self.assertTrue(feed.snapshot()["usable_for_shadow"])
        now[0] = 5.01
        self.assertEqual(feed.snapshot()["state"], "stale")
        now[0] = -1
        self.assertFalse(feed.snapshot()["usable_for_shadow"])

    def test_delayed_or_invalid_new_snapshot_cannot_leave_old_book(self):
        now = [0.0]
        values = iter([payload(), payload(), {"status": "OK", "marketData": {"BI": []}}])

        def fetch(contract, timeout):
            now[0] += 3 if now[0] else 1
            return next(values)

        feed = SnapshotFeed(CONTRACT, fetch, clock=lambda: now[0], max_request=2)
        feed.refresh()
        for _ in range(2):
            with self.assertRaises(PrimaryError):
                feed.refresh()
            self.assertEqual(feed.snapshot()["bids"], [])
            self.assertFalse(feed.connected)

    def test_old_rfx_parser_rejects_duplicates(self):
        p = {"status": "OK", "marketData": {"BI": [{"price": 10000, "size": 1}] * 2,
                                               "OF": [{"price": 10200, "size": 1}]}}
        with self.assertRaisesRegex(PrimaryError, "Duplicate"):
            parse_snapshot(p, requested_identity=("ROFX", "RFX20/OCT26"))

    def test_cli_without_live_never_reads_credentials_or_network(self):
        with patch("market_making.market_data.snapshot_feed.load_dotenv") as env, \
             patch("market_making.market_data.snapshot_feed.requests.Session") as session:
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                main(["--symbol", CONTRACT.symbol])
            env.assert_not_called()
            session.assert_not_called()


if __name__ == "__main__":
    unittest.main()

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import replay_readiness as replay
import validate_live as live


ROOT = Path(__file__).resolve().parents[1]
ORIGINAL = ROOT / "data/live_exploratory_20261002_01"
IDENTITY = ("ROFX", "RFX20/DIC26")
POLICY = json.loads((ROOT / "replay_acceptance_policy.json").read_text())
SPEC = json.loads((ORIGINAL / "contract.json").read_text())["instrument"]


def message(data, second=0, timestamp=None):
    raw = {"type": "Md", "instrumentId": dict(zip(("marketId", "symbol"), IDENTITY)), "marketData": data}
    if timestamp is not None:
        raw["timestamp"] = timestamp
    return {"event": "message", "received_at_utc": f"2026-10-02T16:00:{second:02d}+00:00",
            "raw": json.dumps(raw)}


FULL = {"BI": [{"price": 439600, "size": 10}], "OF": [{"price": 444300, "size": 10}], "LA": None, "TV": 0}


class ReplayTests(unittest.TestCase):
    def test_saved_raw_observations_and_no_exchange_freshness_claim(self):
        events = [json.loads(s) for s in (ORIGINAL / "stream.jsonl").read_text().splitlines()]
        result = replay.quality(events, IDENTITY, SPEC, POLICY)
        self.assertEqual(result["selected_message_count"], 18)
        self.assertEqual(result["availability"]["LA"]["null"], 18)
        self.assertEqual(result["availability"]["TV"]["numeric_zero"], 18)
        self.assertEqual(result["timestamp_message_count"], 18)
        self.assertEqual(result["crossed_observation_lines"], [])
        self.assertEqual(result["spec_violations"], [])
        self.assertIsNone(result["exchange_order_errors"])
        self.assertIsNone(result["historical_clock_uncertainty_seconds"])
        self.assertTrue(all(not r["book_usable"] for r in result["observations"]))
        self.assertIsNone(result["recovery"][0]["seconds_to_valid_market_view"])
        self.assertGreater(len(result["additional_gap_exclusions"]), 0)

    def test_absent_null_empty_zero_distinguished(self):
        for data, kind in (({}, "absent"), ({"TV": None}, "null"), ({"TV": []}, "empty"),
                           ({"TV": {}}, "empty"), ({"TV": 0}, "numeric_zero"), ({"TV": False}, "present")):
            self.assertEqual(replay.entry_kind(data, "TV"), kind)

    def test_no_carry_forward_between_partial_messages(self):
        rows = [message({"BI": FULL["BI"]}), message({"OF": FULL["OF"]}, 1)]
        result = replay.quality(rows, IDENTITY, SPEC, POLICY)
        self.assertTrue(all(r["spread"] is None for r in result["observations"]))
        self.assertEqual(result["availability"]["BI"]["absent"], 1)

    def test_zero_size_not_assumed_deletion_and_spec_violations(self):
        issues = replay.level_issues({"price": 439650, "size": 0}, SPEC)
        self.assertIn("price_off_increment", issues)
        self.assertIn("nonpositive_quantity_deletion_semantics_unknown", issues)
        self.assertIn("quantity_off_increment", replay.level_issues({"price": 439600, "size": 1.5}, SPEC))
        for value in (float("nan"), float("inf"), True, "100"):
            self.assertIn("invalid_price", replay.level_issues({"price": value, "size": 1}, SPEC))

    def test_crossed_duplicate_payload_and_receipt_regression(self):
        data = {**FULL, "OF": [{"price": 439500, "size": 10}]}
        first = message(data, 2, 1)
        result = replay.quality([first, first, message(data, 1, 2)], IDENTITY, SPEC, POLICY)
        self.assertEqual(result["crossed_observation_lines"], [1, 2, 3])
        self.assertEqual(result["duplicate_frame_lines"], [2])
        self.assertEqual(result["repeated_market_payload_lines"], [2, 3])
        self.assertEqual(result["receipt_order_error_lines"], [3])

    def test_stale_missing_timestamp_and_boundary_gaps(self):
        events = [message(FULL), message(FULL, 25),
                  {"event": "ended", "received_at_utc": "2026-10-02T16:00:50+00:00"}]
        result = replay.quality(events, IDENTITY, SPEC, POLICY)
        self.assertEqual(result["max_receipt_gap_seconds"], 25)
        self.assertEqual(len(result["stale_receipt_intervals"]["BI"]), 2)
        self.assertEqual(result["timestamp_message_count"], 0)
        self.assertIsNone(replay.receipt("2026-10-02T16:00:00"))

    def test_disconnect_gap_stale_terminal_and_claimed_snapshot_never_enable(self):
        guard = replay.UnverifiedBookGuard()
        self.assertFalse(guard.observe(FULL))
        for reason in ("disconnect", "gap", "stale"):
            guard.invalidate(reason)
            self.assertFalse(guard.observe({"BI": FULL["BI"]}))
            self.assertFalse(guard.observe({**FULL, "snapshot": True}))
        guard.invalidate("terminal", terminal=True)
        self.assertFalse(guard.observe(FULL))
        self.assertIn("Terminal", guard.reason)
        self.assertEqual(guard.generation, 4)

    def test_exclusive_offline_report_preserves_sources_and_blocks_all_replay(self):
        before = replay.hashes(ORIGINAL)
        with tempfile.TemporaryDirectory() as folder, patch.object(live, "authenticate") as auth:
            output = Path(folder) / "audit"
            result = replay.audit(ORIGINAL, output, ROOT / "replay_acceptance_policy.json")
            auth.assert_not_called()
            self.assertEqual(len(result["capture_matrix"]), 12)
            self.assertEqual(result["new_credentialed_runs"], 0)
            self.assertEqual(result["overall_replay_status"], "BLOCKED")
            self.assertEqual(result["uses"]["raw_observation_exploration"]["status"], "READY")
            for use in ("order_book_reconstruction", "quote_strategy_replay", "time_dependent_replay", "trade_dependent_replay"):
                self.assertEqual(result["uses"][use]["status"], "BLOCKED")
                self.assertTrue(result["uses"][use]["fill_assumptions"])
            with self.assertRaises(FileExistsError):
                replay.audit(ORIGINAL, output, ROOT / "replay_acceptance_policy.json")
            with self.assertRaises(ValueError):
                replay.audit(ORIGINAL, ORIGINAL / "forbidden", ROOT / "replay_acceptance_policy.json")
        self.assertEqual(before, replay.hashes(ORIGINAL))

    def test_recovery_rejects_extra_fault_before_data_and_interrupted_attempt(self):
        prefix = [message(FULL), {"event": "disconnected", "reason": "InducedLocalDisconnect"},
                  {"event": "reconnecting"}, {"event": "connected"}]
        suffix = [{"event": "subscription_sent"}, message(FULL, 1)]
        self.assertTrue(live.summarize(prefix + suffix, IDENTITY)["recovered"])
        extra = [{"event": "disconnected", "reason": "InducedLocalDisconnect"}] + prefix + suffix
        self.assertFalse(live.summarize(extra, IDENTITY)["recovered"])
        self.assertEqual(live.summarize(extra, IDENTITY)["induced_local_disconnects"], 2)
        interrupted = prefix + [{"event": "disconnected"}, {"event": "connected"}] + suffix
        self.assertFalse(live.summarize(interrupted, IDENTITY)["recovered"])

    def test_completion_needs_unique_consistent_session(self):
        session = {"event": "session", "marketId": IDENTITY[0], "symbol": IDENTITY[1], "duration_seconds": 300}
        end = {"event": "ended", "reason": "duration_deadline", "duration_seconds": 300,
               "elapsed_seconds": 300.048, "deadline_reached": True}
        rows = [session, message(FULL), end]
        self.assertTrue(live.summarize(rows, IDENTITY, expected_duration=300)["duration_completed"])
        for invalid in (rows[1:], [session, session] + rows[1:], [{**session, "duration_seconds": 12}] + rows[1:],
                        [{**session, "symbol": "OTHER"}] + rows[1:], rows + [end]):
            self.assertFalse(live.summarize(invalid, IDENTITY, expected_duration=300)["duration_completed"])

    def test_reanalysis_does_not_invent_process_exit_status(self):
        from reanalyze_demo import reanalyze
        with tempfile.TemporaryDirectory() as folder:
            result = reanalyze(ORIGINAL, Path(folder) / "analysis")
            self.assertIsNone(result["original_exit_code"])
            self.assertEqual(result["original_inferred_exit_equivalent"], 2)
            self.assertEqual(result["retrospective_observational_stages"]["recovery"]["status"], "PASS")

    def test_terminal_error_cannot_pass_offline_observational_acceptance(self):
        rows = [json.loads(s) for s in (ORIGINAL / "stream.jsonl").read_text().splitlines()]
        rows.insert(-1, {"event": "error"})
        summary = live.summarize(rows, IDENTITY, expected_duration=300)
        result = live.observation_acceptance(summary)
        self.assertTrue(summary["terminal_recorder_error"])
        self.assertFalse(result["stream_pass"])
        self.assertFalse(result["recovery_pass"])
        self.assertEqual(result["recorder_error_class"], "RecordedTerminalError")

    def test_raw_auth_and_subscription_rejections_are_terminal_evidence(self):
        for raw in ({"type": "error", "status": "REJECTED"}, {"code": 401},
                    {"type": "Md", "status": "403", "marketData": FULL}):
            result = replay.quality([{"event": "message", "raw": json.dumps(raw)}], IDENTITY, SPEC, POLICY)
            self.assertEqual(result["terminal_error_lines"], [1])
            self.assertEqual(result["withheld_frame_lines"], [1])

    def test_receipt_threshold_needs_valid_complete_boundaries_and_selected_data(self):
        start = {"event": "session", "received_at_utc": "2026-10-02T16:00:00+00:00"}
        end = {"event": "ended", "received_at_utc": "2026-10-02T16:00:10+00:00"}
        rows = [start, message(FULL, 1), message(FULL, 2), end]
        self.assertTrue(replay.quality(rows, IDENTITY, SPEC, POLICY)["receipt_coverage_complete"])
        for invalid in ([{"event": "session"}] + rows[1:], rows[:-1] + [{"event": "ended"}],
                        [start, end], rows[:-1] + [{**end, "received_at_utc": "2026-10-02T15:00:00+00:00"}],
                        [{**start, "received_at_utc": "2026-10-02T16:01:00+00:00"}] + rows[1:]):
            comparison = replay.quality(invalid, IDENTITY, SPEC, POLICY)["retrospective_threshold_comparisons"]["receipt_gap"]
            self.assertFalse(comparison["evidence_complete"])
            self.assertFalse(comparison["within_limit"])

    def test_long_initial_and_final_gaps_are_excluded_with_endpoints(self):
        rows = [{"event": "session", "received_at_utc": "2026-10-02T15:59:00+00:00"},
                message(FULL), {"event": "ended", "received_at_utc": "2026-10-02T16:01:00+00:00"}]
        result = replay.quality(rows, IDENTITY, SPEC, POLICY)
        exclusions = result["additional_gap_exclusions"]
        self.assertEqual([e["kind"] for e in exclusions], ["start_to_first", "last_to_end"])
        self.assertEqual([(e["from_line"], e["to_line"]) for e in exclusions], [(1, 2), (2, 3)])
        self.assertTrue(all(e["seconds"] == 60 and e["start"] and e["end"] for e in exclusions))


if __name__ == "__main__":
    unittest.main()

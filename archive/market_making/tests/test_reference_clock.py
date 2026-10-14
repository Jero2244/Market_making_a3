import struct
import unittest

from market_making.market_data.check_connection import PrimaryError
from market_making.market_data.reference_clock import (NTP_EPOCH, ReferenceClock, Sample,
                                                       RenewingReferenceClock, calibrate,
                                                       decode_sample, diagnose_timestamp)


def sample(server="one", offset=1.5, uncertainty=0.01):
    return Sample(server, offset, uncertainty, 1700000000, 100, 0.01, 2)


def calibration(**kwargs):
    return ReferenceClock.from_samples([sample(), sample("two")], **kwargs)


def packet_stamp(value):
    seconds = value+NTP_EPOCH
    return struct.pack("!II", int(seconds) % 2**32, int((seconds % 1)*2**32))


def packets():
    request = bytearray(48)
    request[0] = 0x23
    request[40:48] = packet_stamp(1700000000)
    reply = bytearray(48)
    reply[0], reply[1], reply[3] = 0x24, 2, 236  # v4 server, stratum 2, precision -20
    reply[24:32] = request[40:48]
    reply[32:40] = packet_stamp(1700000001.515)
    reply[40:48] = packet_stamp(1700000001.525)
    return request, reply


class ClockTests(unittest.TestCase):
    def test_renewal_failure_does_not_extend_old_calibration(self):
        clock = RenewingReferenceClock(calibration(ttl=10),
                                      refresh=lambda: (_ for _ in ()).throw(PrimaryError("unavailable")))
        self.assertFalse(clock.renew())
        self.assertEqual(clock.reading(105, 1700000005)["state"], "estimated")
        self.assertEqual(clock.reading(111, 1700000011)["state"], "expired")
        self.assertEqual(clock.reading(111, 1700000011)["renewal_state"], "failed")
        clock.close()

    def test_successful_renewal_uses_new_anchor_and_shutdown_closes_worker(self):
        newer = ReferenceClock.from_samples([
            Sample("one", 1.5, 0.01, 1700000010, 110, 0.01, 2),
            Sample("two", 1.5, 0.01, 1700000010, 110, 0.01, 2)])
        clock = RenewingReferenceClock(calibration(ttl=10), refresh=lambda: newer,
                                      monotonic=lambda: 110)
        self.assertTrue(clock.renew())
        self.assertEqual(clock.reading(111, 1700000011)["state"], "estimated")
        clock.start()
        clock.close()
        self.assertFalse(clock.worker.is_alive())
        self.assertFalse(clock.renew())

    def test_offset_decodes_and_monotonic_anchor_advances(self):
        request, reply = packets()
        result = decode_sample("test", request, reply, 1700000000, 1700000000.04, 100, 100.04)
        self.assertAlmostEqual(result.offset, 1.5, places=5)
        self.assertAlmostEqual(result.roundtrip, 0.03, places=5)
        c = calibration()
        reading = c.reading(102, 1700000002)
        self.assertAlmostEqual(reading["reference_unix_seconds"], 1700000003.5)
        self.assertGreater(reading["estimated_uncertainty_seconds"], c.uncertainty)
        self.assertFalse(reading["source_clock_authority_verified"])

    def test_packets_with_wrong_origin_unsynchronized_or_zero_time_rejected(self):
        request, valid = packets()
        for index, value in ((24, 1), (0, 0xE4), (0, 0x23), (1, 0), (1, 16)):
            bad = bytearray(valid)
            bad[index] = value
            with self.subTest(index=index, value=value), self.assertRaises(PrimaryError):
                decode_sample("test", request, bad, 1700000000, 1700000000.04, 100, 100.04)
        with self.assertRaises(PrimaryError):
            decode_sample("test", request, b"short", 0, 1, 0, 1)
        bad = bytearray(valid)
        bad[32:40] = bytes(8)
        with self.assertRaises(PrimaryError):
            decode_sample("test", request, bad, 1700000000, 1700000000.04, 100, 100.04)

    def test_mid_request_clock_jump_and_slow_request_rejected(self):
        request, reply = packets()
        for t4, m4 in ((1700000001, 100.04), (1700000002, 102), (1700000000.04, 99)):
            with self.subTest(t4=t4, m4=m4), self.assertRaises(PrimaryError):
                decode_sample("test", request, reply, 1700000000, t4, 100, m4)

    def test_disagreement_and_single_source_cannot_calibrate(self):
        for samples in ([sample()], [sample(), sample()],
                        [sample(), sample("two", offset=3)],
                        [sample(), sample("two", uncertainty=-1)]):
            with self.subTest(samples=samples), self.assertRaises(PrimaryError):
                ReferenceClock.from_samples(samples)
        with self.assertRaises(PrimaryError):
            calibrate(query=lambda server: (_ for _ in ()).throw(OSError()))

    def test_expiry_rollback_and_wall_jump_make_reference_unavailable(self):
        c = calibration(ttl=10)
        for mono, wall, expected in ((111, 1700000011, "expired"),
                                      (99, 1699999999, "clock_rollback"),
                                      (101, 1700000002.5, "wall_clock_changed")):
            with self.subTest(expected=expected):
                reading = c.reading(mono, wall)
                self.assertEqual(reading["state"], expected)
                self.assertIsNone(reading["reference_unix_seconds"])
                self.assertIsNone(reading["estimated_uncertainty_seconds"])

    def test_future_due_to_slow_local_clock_is_resolved_without_forging_authority(self):
        message = {"timestamp": 1700000001480}
        raw = diagnose_timestamp(message, 1700000000, 100)
        self.assertEqual(raw["state"], "clock_unverified")
        corrected = diagnose_timestamp(message, 1700000000, 100, calibration())
        self.assertEqual(corrected["state"], "candidate_only")
        self.assertAlmostEqual(corrected["estimated_message_age_seconds"], 0.02, places=5)
        self.assertFalse(corrected["timestamp_authoritative"])
        self.assertIsNone(corrected["exchange_quote_age_seconds"])

    def test_true_future_not_clamped_and_expired_calibration_not_reused(self):
        future = diagnose_timestamp({"timestamp": 1700000002000}, 1700000000, 100, calibration())
        self.assertEqual(future["state"], "candidate_future")
        self.assertAlmostEqual(future["estimated_message_age_seconds"], -0.5)
        expired = diagnose_timestamp({"timestamp": 1700000002000}, 1700000400, 500, calibration())
        self.assertEqual(expired["state"], "expired")
        self.assertIsNone(expired["estimated_message_age_seconds"])

    def test_nonfinite_wrong_type_and_missing_candidate(self):
        for value in (True, "1700000000000", 1700000000000.0, float("nan"), 10**1000):
            with self.subTest(value=str(value)[:30]):
                result = diagnose_timestamp({"timestamp": value}, 1700000000, 100, calibration())
                self.assertEqual(result["state"], "invalid")
        self.assertEqual(diagnose_timestamp({}, 1700000000, 100)["state"], "missing")


if __name__ == "__main__":
    unittest.main()

"""Measured UTC estimates anchored to a monotonic clock, without changing Windows.

SNTP measurements are unauthenticated estimates, not provider timestamp authority.
Packet checks and agreement of independent sources prevent silent offset guessing.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import socket
import struct
import threading
import time

from .check_connection import PrimaryError

NTP_EPOCH = 2208988800
DEFAULT_SERVERS = ("time.cloudflare.com", "time.windows.com")


@dataclass(frozen=True)
class Sample:
    server: str
    offset: float
    uncertainty: float
    received_wall: float
    received_monotonic: float
    roundtrip: float
    stratum: int


def decode_sample(server, request, reply, t1, t4, m1, m4):
    if (len(reply) < 48 or len(request) != 48 or reply[24:32] != request[40:48]
            or (reply[0] & 7) != 4 or ((reply[0] >> 3) & 7) not in (3, 4)
            or reply[0] >> 6 == 3 or not 1 <= reply[1] <= 15):
        raise PrimaryError("Invalid or unsynchronized time-server response.")
    if not all(math.isfinite(v) for v in (t1, t4, m1, m4)):
        raise PrimaryError("Invalid local time sample.")
    elapsed = m4 - m1
    if elapsed < 0 or elapsed > 1 or abs((t4-t1)-elapsed) > 0.02:
        raise PrimaryError("Clock changed or time-server request too slow.")

    def stamp(index):
        seconds, fraction = struct.unpack("!II", reply[index:index+8])
        if not seconds and not fraction:
            raise PrimaryError("Time-server event timestamp missing.")
        value = seconds - NTP_EPOCH + fraction / 2**32
        # Choose the NTP era closest to the local calendar (2036 rollover).
        return value + round((t4-value) / 2**32) * 2**32

    t2, t3 = stamp(32), stamp(40)
    processing = t3 - t2
    roundtrip = elapsed - processing
    if processing < 0 or roundtrip < -0.001:
        raise PrimaryError("Invalid time-server event ordering.")
    root_delay = struct.unpack("!i", reply[4:8])[0] / 65536
    dispersion = struct.unpack("!I", reply[8:12])[0] / 65536
    precision = 2 ** struct.unpack("!b", reply[3:4])[0]
    uncertainty = (max(0, roundtrip)/2 + max(0, root_delay)/2 + dispersion
                   + precision + 0.002)
    if uncertainty > 0.25:
        raise PrimaryError("Time-server uncertainty too large.")
    return Sample(server, ((t2-t1)+(t3-t4))/2, uncertainty, t4, m4,
                  max(0, roundtrip), reply[1])


def query_server(server, *, timeout=2, wall=time.time, monotonic=time.monotonic):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.connect((server, 123))
        packet = bytearray(48)
        packet[0] = 0x23
        m1, t1 = monotonic(), wall()
        stamp = t1 + NTP_EPOCH
        packet[40:48] = struct.pack("!II", int(stamp) % 2**32,
                                   int((stamp % 1) * 2**32))
        sock.send(packet)
        reply = sock.recv(512)
        t4, m4 = wall(), monotonic()
    return decode_sample(server, packet, reply, t1, t4, m1, m4)


@dataclass(frozen=True)
class ReferenceClock:
    utc_anchor: float
    monotonic_anchor: float
    wall_anchor: float
    offset: float
    uncertainty: float
    samples: tuple
    ttl: float = 300
    drift_ppm: float = 100

    @classmethod
    def from_samples(cls, samples, *, ttl=300, drift_ppm=100):
        if (len({s.server for s in samples}) < 2 or not math.isfinite(ttl) or ttl <= 0
                or not math.isfinite(drift_ppm) or drift_ppm < 0):
            raise PrimaryError("Two independent clock sources and valid bounds are required.")
        if any(not all(math.isfinite(v) for v in
                       (s.offset, s.uncertainty, s.received_wall, s.received_monotonic))
               or s.uncertainty < 0 for s in samples):
            raise PrimaryError("Invalid clock-source estimate.")
        low = max(s.offset-s.uncertainty for s in samples)
        high = min(s.offset+s.uncertainty for s in samples)
        if low > high:
            raise PrimaryError("Time servers disagree; calibration unavailable.")
        best = min(samples, key=lambda s: s.uncertainty)
        # Keep the best source's entire uncertainty; agreement does not shrink it.
        return cls(best.received_wall+best.offset, best.received_monotonic,
                   best.received_wall, best.offset, best.uncertainty,
                   tuple(samples), ttl, drift_ppm)

    def reading(self, monotonic_now, wall_now):
        elapsed = monotonic_now-self.monotonic_anchor
        state = "estimated"
        if not math.isfinite(elapsed) or elapsed < 0:
            state = "clock_rollback"
        elif elapsed > self.ttl:
            state = "expired"
        elif not math.isfinite(wall_now) or abs((wall_now-self.wall_anchor)-elapsed) > 0.05:
            state = "wall_clock_changed"
        valid = state == "estimated"
        return {"state": state, "reference_unix_seconds": self.utc_anchor+elapsed if valid else None,
                "local_offset_seconds": self.offset,
                "estimated_uncertainty_seconds": self.uncertainty+elapsed*self.drift_ppm/1e6 if valid else None,
                "calibration_age_seconds": elapsed if math.isfinite(elapsed) else None,
                "sources": [s.server for s in self.samples], "authentication": "none",
                "source_clock_authority_verified": False}


def calibrate(*, query=query_server):
    try:
        samples = [query(server) for server in DEFAULT_SERVERS]
        return ReferenceClock.from_samples(samples)
    except (OSError, ValueError, OverflowError) as exc:
        raise PrimaryError("Time reference unavailable; calibration stopped.") from None


class RenewingReferenceClock:
    """Refresh measured UTC away from the market-data receive path.

    A failed renewal never extends an old estimate's expiry. Worker requests
    have socket timeouts; OS DNS delays may outlive close's bounded join.
    """
    def __init__(self, initial, *, refresh=calibrate, monotonic=time.monotonic):
        self.reference = initial
        self.refresh = refresh
        self.monotonic = monotonic
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.worker = None
        self.renewal_state = "initial"

    def reading(self, monotonic_now, wall_now):
        with self.lock:
            result = self.reference.reading(monotonic_now, wall_now)
            result["renewal_state"] = self.renewal_state
            return result

    def renew(self):
        try:
            reference = self.refresh()
        except (PrimaryError, OSError, ValueError, OverflowError):
            with self.lock:
                self.renewal_state = "failed"
            return False
        with self.lock:
            if self.stop.is_set():
                return False
            self.reference = reference
            self.renewal_state = "renewed"
        return True

    def start(self):
        if self.worker is not None:
            raise PrimaryError("Clock renewal already started.")

        def work():
            with self.lock:
                next_refresh = self.reference.monotonic_anchor+self.reference.ttl*0.75
            while not self.stop.wait(max(0, next_refresh-self.monotonic())):
                succeeded = self.renew()
                with self.lock:
                    next_refresh = (self.reference.monotonic_anchor+self.reference.ttl*0.75
                                    if succeeded else self.monotonic()+30)

        self.worker = threading.Thread(target=work, name="market-data-clock-reference", daemon=True)
        self.worker.start()
        return self

    def close(self):
        self.stop.set()
        if self.worker is not None:
            self.worker.join(timeout=5)


def diagnose_timestamp(message, received_wall, received_monotonic, reference=None):
    """Interpret only the observed Md path as a candidate; no freshness promotion."""
    result = {"mapping": "Md.timestamp_as_epoch_milliseconds (candidate)",
              "timestamp_authoritative": False, "exchange_quote_age_seconds": None,
              "state": "missing", "candidate_utc": None,
              "estimated_message_age_seconds": None,
              "estimated_age_interval_seconds": None}
    value = message.get("timestamp")
    if value is None:
        return result
    if isinstance(value, bool) or not isinstance(value, int):
        result["state"] = "invalid"
        return result
    try:
        candidate = value/1000
        result["candidate_utc"] = datetime.fromtimestamp(candidate, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        result["state"] = "invalid"
        return result
    if reference is None:
        result["state"] = "clock_unverified"
        return result
    reading = reference.reading(received_monotonic, received_wall)
    result["clock"] = reading
    if reading["state"] != "estimated":
        result["state"] = reading["state"]
        return result
    age = reading["reference_unix_seconds"]-candidate
    uncertainty = reading["estimated_uncertainty_seconds"] + 0.001
    result.update(state="candidate_only", estimated_message_age_seconds=age,
                  estimated_age_interval_seconds=[age-uncertainty, age+uncertainty])
    if age+uncertainty < 0:
        result["state"] = "candidate_future"
    return result

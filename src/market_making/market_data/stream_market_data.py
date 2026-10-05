"""Read-only REMARKETS RFX 20 market-data recorder (no order API)."""
import argparse
import json
import os
from pathlib import Path
import sys
import time
import requests
import websocket
from market_making.market_data.check_connection import (
    PrimaryError, authenticate, get_json, load_dotenv, require_field,
    instrument_id, normalize, safe_payload)
from market_making.market_data.websocket_session import (
    WS_URL, ENTRIES, TIMESTAMP_KEYS, utc_now, server_timestamps, validate_symbol,
    Freshness, auth_rejected, subscription_rejected, contains_secret,
    parse_market_frame, FixedHostWebSocket, connect, run_session)


class Recorder:
    def __init__(self, output):
        # Exclusive create: never append to or overwrite a previous session.
        self.file = output.open("x", encoding="utf-8")

    def write(self, event):
        try:
            self.file.write(json.dumps(event, ensure_ascii=True, separators=(",", ":")) + "\n")
            self.file.flush()
            os.fsync(self.file.fileno())
        except (OSError, ValueError):
            raise PrimaryError("Recording write failed; session stopped.") from None

    def close(self):
        self.file.close()


def run(symbol, market_id, output, username, password, *, duration=300, stale_after=20,
        reconnects=5, refresh_after=23 * 3600, interval=5, connect_fn=connect,
        clock=time.monotonic, sleep=time.sleep, shared_secrets=None):
    def factory():
        output.parent.mkdir(parents=True, exist_ok=True)
        return Recorder(output)
    return run_session(symbol, market_id, factory, username, password,
        duration=duration, stale_after=stale_after, reconnects=reconnects,
        refresh_after=refresh_after, interval=interval, connect_fn=connect_fn,
        clock=clock, sleep=sleep, shared_secrets=shared_secrets,
        session_factory=requests.Session, authenticate_fn=authenticate,
        get_json_fn=get_json, validate_fn=validate_symbol, utc_fn=utc_now,
        parse_fn=parse_market_frame, timestamps_fn=server_timestamps,
        freshness_factory=Freshness)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True, help="Exact RFX 20 future symbol from catalog")
    parser.add_argument("--market-id", default="ROFX")
    parser.add_argument("--output", type=Path, required=True, help="NEW JSONL path")
    parser.add_argument("--duration", type=float, default=300, help="Session seconds (default: 300)")
    parser.add_argument("--stale-after", type=float, default=20)
    parser.add_argument("--reconnects", type=int, default=5)
    parser.add_argument("--refresh-after", type=float, default=23 * 3600)
    parser.add_argument("--interval", type=float, default=5, help="Heartbeat/status seconds")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--no-env", action="store_true")
    args = parser.parse_args(argv)
    if not args.no_env:
        load_dotenv(args.env_file)
    username, password = os.getenv("PRIMARY_USER", ""), os.getenv("PRIMARY_PASSWORD", "")
    if not username or not password:
        parser.error("Set PRIMARY_USER and PRIMARY_PASSWORD in environment or dotenv file.")
    try:
        run(args.symbol, args.market_id, args.output, username, password,
            duration=args.duration, stale_after=args.stale_after,
            reconnects=args.reconnects, refresh_after=args.refresh_after, interval=args.interval)
        print("Recording finished (new JSONL output).")
        return 0
    except KeyboardInterrupt:
        print("Interrupted; recording closed.", file=sys.stderr)
        return 130
    except (PrimaryError, OSError, ValueError) as exc:
        print(f"Recording stopped: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

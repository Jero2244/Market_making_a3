"""Read-only DEMO account order reports. Default: NOT_RUN, no credential access."""
import argparse
import json
import os
from pathlib import Path
from market_making.execution.order_reports import identifier
from market_making.execution.websocket_order_session import run_session, validate_bounds


class Evidence:
    """Exclusive destination; append-only flushed evidence, not an order journal."""
    def __init__(self, path):
        self.file = Path(path).open("x", encoding="utf-8")

    def write(self, event):
        self.file.write(json.dumps(event, sort_keys=True, allow_nan=False) + "\n")
        self.file.flush()
        os.fsync(self.file.fileno())

    def close(self):
        self.file.close()


def main(argv=None, *, runner=run_session, environ=None, evidence_factory=Evidence):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Explicit read-only REMARKETS opt-in (never production)")
    parser.add_argument("--output", help="Exclusive NEW JSONL evidence file (parent must exist)")
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--interval", type=float, default=1)
    parser.add_argument("--reconnects", type=int, default=2)
    parser.add_argument("--refresh-after", type=float, default=23 * 3600)
    args = parser.parse_args(argv)
    if not args.live:
        print("NOT_RUN: read-only DEMO opt-in required; orders_sent=0; readiness unverified")
        return 2
    sink = None
    exit_code = 2
    try:
        validate_bounds(args.duration, args.interval, args.reconnects, args.refresh_after)
        if not args.output:
            raise ValueError("New evidence destination required")
        # Reserve destination before credential access or authentication. Never
        # overwrite prior evidence, never create directories or use existing locks.
        sink = evidence_factory(args.output)
        env = os.environ if environ is None else environ
        user = env.get("PRIMARY_USER", "")
        password = env.get("PRIMARY_PASSWORD", "")
        account = env.get("PRIMARY_ACCOUNT", "")
        if not user or not password or not identifier(account):
            sink.write({"event": "blocked", "orders_sent": 0, "readiness": "unverified",
                        "continuity": "unverified", "gaps": ["CONFIGURATION_UNAVAILABLE"]})
        else:
            result = runner(account, sink, user, password, duration=args.duration,
                            interval=args.interval, reconnects=args.reconnects,
                            refresh_after=args.refresh_after)
            print("Observational collection ended; orders_sent=0; readiness unverified")
            exit_code = 0 if result.get("qualifying_reports", 0) > 0 else 2
    except KeyboardInterrupt:
        exit_code = 130
    except Exception:
        print("BLOCKED/FAILED: read-only collection unavailable; orders_sent=0; readiness unverified")
        exit_code = 2
    finally:
        if sink is not None:
            try:
                sink.close()
            except Exception:
                # A failed evidence close must not turn a collection into success.
                exit_code = 2
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

"""Read-only REMARKETS 24/7 test view; receipt age is NOT exchange quote age."""
import argparse
import json
import os
import math
from pathlib import Path
import requests
from market_making.market_data.check_connection import PrimaryError, authenticate, load_dotenv
from market_making.market_data.instrument_rules import resolve
from market_making.market_data.order_book import fetch_book
from market_making.market_data.websocket_session import run_session
from market_making.market_data.websocket_observation import Observation
from market_making.market_data.instrument_rules import MARKET, SYMBOL


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--depth", type=int, choices=range(1, 6), default=5)
    parser.add_argument("--transport", choices=("rest", "websocket"), default="rest")
    parser.add_argument("--duration", type=float, default=30, help="Bounded WebSocket observation seconds")
    parser.add_argument("--stale-after", type=float, default=20)
    args = parser.parse_args(argv)
    if not args.live:
        print("NOT_RUN: use --live for read-only demo market data.")
        return 2
    if any(not math.isfinite(v) or v <= 0 for v in (args.duration, args.stale_after)):
        parser.error("Timing must be positive and finite.")
    load_dotenv(Path(".env"))
    try:
        user, password = os.getenv("PRIMARY_USER", ""), os.getenv("PRIMARY_PASSWORD", "")
        if not user or not password:
            raise PrimaryError("Missing environment credentials.")
        if args.transport == "websocket":
            secrets = []
            observation = Observation(MARKET, SYMBOL, depth=args.depth,
                stale_after=args.stale_after, secrets=secrets,
                display=lambda value: print(json.dumps(value, indent=2)))
            run_session(SYMBOL, MARKET, lambda: observation, user, password,
                        depth=args.depth, duration=args.duration,
                        stale_after=args.stale_after, shared_secrets=secrets)
            if not observation.eligible:
                print("NOT_OBSERVED: no eligible selected-instrument observation by deadline.")
                return 2
            return 0
        with requests.Session() as session:
            authenticate(session, user, password)
            resolve(session)
            print(json.dumps(fetch_book(session, args.depth).summary(), indent=2))
        return 0
    except KeyboardInterrupt:
        print("Interrupted; observation closed.")
        return 130
    except (PrimaryError, OSError, ValueError):
        print("BLOCKED: request/rules/snapshot validation failed (no server body logged).")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

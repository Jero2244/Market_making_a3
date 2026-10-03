"""Read-only REMARKETS 24/7 test view; receipt age is NOT exchange quote age."""
import argparse
import json
import os
from pathlib import Path
import requests
from market_making.market_data.check_connection import PrimaryError, authenticate, load_dotenv
from market_making.market_data.instrument_rules import resolve
from market_making.market_data.order_book import fetch_book


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--depth", type=int, choices=range(1, 6), default=5)
    args = parser.parse_args(argv)
    if not args.live:
        print("NOT_RUN: use --live for read-only demo REST.")
        return 2
    load_dotenv(Path(".env"))
    try:
        user, password = os.getenv("PRIMARY_USER", ""), os.getenv("PRIMARY_PASSWORD", "")
        if not user or not password:
            raise PrimaryError("Missing environment credentials.")
        with requests.Session() as session:
            authenticate(session, user, password)
            resolve(session)
            print(json.dumps(fetch_book(session, args.depth).summary(), indent=2))
        return 0
    except PrimaryError:
        print("BLOCKED: request/rules/snapshot validation failed (no server body logged).")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

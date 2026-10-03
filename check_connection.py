"""One-shot REMARKETS authentication, instrument discovery and market data check."""

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import getpass
import json
import os
from pathlib import Path
import sys

import requests


BASE_URL = "https://api.remarkets.primary.com.ar"
ENTRIES = "BI,OF,LA,OP,CL,SE,OI,TV"
ALLOWED_OPERATIONS = frozenset({("POST", "/auth/getToken"),
    ("GET", "/rest/segment/all"), ("GET", "/rest/instruments/all"),
    ("GET", "/rest/instruments/detail"), ("GET", "/rest/marketdata/get")})


def safe_payload(value, secrets=()):
    """Reject secret echoes (including JSON keys), never attempt partial redaction."""
    pending = [value]
    sensitive = {"xauthtoken", "xusername", "xpassword", "authorization",
                 "username", "password", "token", "accesstoken", "refreshtoken"}
    sensitive = {s.upper() for s in sensitive}
    while pending:
        item = pending.pop()
        if isinstance(item, str) and any(s and s in item for s in secrets):
            return False
        if isinstance(item, dict):
            if any(normalize(str(k)) in sensitive for k in item):
                return False
            for key, child in item.items():
                pending.extend((key, child))
        elif isinstance(item, list):
            pending.extend(item)
    return True


class PrimaryError(RuntimeError):
    """Connection, authentication or API response failure."""


def request(session, method, path, **kwargs):
    """Keep credentials on the configured host and bound network waits."""
    if (method, path) not in ALLOWED_OPERATIONS:
        raise PrimaryError("Operation is not on the read-only validation allowlist.")
    try:
        response = session.request(
            method, BASE_URL + path, timeout=(10, 30),
            allow_redirects=False, **kwargs
        )
    except requests.RequestException as exc:
        # Do not log request objects, headers or credentials.
        raise PrimaryError(f"{path}: network failure ({type(exc).__name__}).") from None
    if not 200 <= response.status_code < 300:
        hints = {
            401: "Authentication failed or token expired; check credentials and rerun.",
            403: "Access denied; check REMARKETS API permissions.",
            429: "Rate limit reached; wait before rerunning.",
        }
        hint = hints.get(response.status_code, "Check endpoint availability and retry later.")
        raise PrimaryError(f"{path}: HTTP {response.status_code}. {hint}")
    return response


def authenticate(session, username, password):
    session._primary_secrets = [username, password]
    response = request(session, "POST", "/auth/getToken", headers={
        "X-Username": username, "X-Password": password,
    })
    token = response.headers.get("X-Auth-Token")
    if not token:
        raise PrimaryError("Authentication response did not include X-Auth-Token.")
    session.headers["X-Auth-Token"] = token
    session._primary_secrets.append(token)


def get_json(session, path, **params):
    response = request(session, "GET", path, params=params)
    try:
        payload = response.json()
    except ValueError:
        raise PrimaryError(f"{path}: expected JSON, received a non-JSON response.") from None
    if not isinstance(payload, dict) or payload.get("status") != "OK":
        # Avoid dumping arbitrary server responses that may contain sensitive data.
        raise PrimaryError(f"{path}: API did not return status OK; check permissions and parameters.")
    if not safe_payload(payload, getattr(session, "_primary_secrets", ())):
        raise PrimaryError("API payload withheld: sensitive content detected.")
    return payload


def require_field(payload, key, expected_type):
    value = payload.get(key)
    if not isinstance(value, expected_type):
        raise PrimaryError(f"Unexpected API response: {key} must be {expected_type.__name__}.")
    return value


def instrument_id(instrument):
    identity = instrument.get("instrumentId") or instrument
    if not isinstance(identity, dict):
        raise PrimaryError("Unexpected instrument identifier format.")
    return identity.get("marketId", ""), identity.get("symbol", "")


def normalize(text):
    return "".join(c for c in text.upper() if c.isalnum())


def best_price(levels, side):
    prices = []
    if not isinstance(levels, list):
        return None
    for level in levels:
        if not isinstance(level, dict):
            continue
        try:
            price = Decimal(str(level.get("price")))
            size = Decimal(str(level.get("size")))
            if price.is_finite() and size.is_finite() and size > 0:
                prices.append(price)
        except InvalidOperation:
            continue
    return (max(prices) if side == "BI" else min(prices)) if prices else None


def print_snapshot(data):
    print("Market data (raw entries; supplied dates have unverified scope, not authoritative book freshness):")
    print(json.dumps(data, indent=2, ensure_ascii=True))
    bid = best_price(data.get("BI"), "BI")
    ask = best_price(data.get("OF"), "OF")
    if bid is None or ask is None:
        print("Two-sided book unavailable. The session may be closed or the contract inactive.")
        return
    print(f"Best bid: {bid} | Best ask: {ask} | Spread: {ask - bid}")
    if ask > bid:
        print(f"Midpoint: {(ask + bid) / 2}")
    else:
        print("Locked/crossed snapshot: inspect freshness and session state before interpreting it.")


def load_dotenv(path):
    """Minimal .env loader: KEY=VALUE lines, no dependency, real env wins."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--search", default="RFX20", help="Catalog symbol substring (default: RFX20)")
    parser.add_argument("--symbol", help="Exact symbol copied from discovery; fetch its details and snapshot")
    parser.add_argument("--market-id", default="ROFX")
    parser.add_argument("--depth", type=int, choices=range(1, 6), default=3)
    parser.add_argument("--output", type=Path, help="Write retrieved data to a NEW JSON file")
    parser.add_argument("--no-prompt", action="store_true", help="Require credentials in environment")
    parser.add_argument("--env-file", type=Path, default=Path(".env"),
                        help="Dotenv file to load (default: .env; use --no-env to disable)")
    parser.add_argument("--no-env", action="store_true", help="Do not load a .env file")
    args = parser.parse_args(argv)

    if not args.no_env:
        load_dotenv(args.env_file)
    username = os.getenv("PRIMARY_USER", "")
    password = os.getenv("PRIMARY_PASSWORD", "")
    try:
        if not args.no_prompt:
            username = username or input("REMARKETS username: ").strip()
            password = password or getpass.getpass("REMARKETS password: ")
        if not username or not password:
            raise PrimaryError("Set PRIMARY_USER and PRIMARY_PASSWORD, or run without --no-prompt.")

        print(f"Environment: REMARKETS | {BASE_URL}")
        with requests.Session() as session:
            authenticate(session, username, password)
            print("Authentication OK (token kept in memory).")
            segments = get_json(session, "/rest/segment/all")
            print("Segments:", json.dumps(require_field(segments, "segments", list)))
            catalog = get_json(session, "/rest/instruments/all")
            instruments = require_field(catalog, "instruments", list)
            if not all(isinstance(item, dict) for item in instruments):
                raise PrimaryError("Unexpected instrument catalog format.")
            matches = [item for item in instruments
                       if normalize(args.search) in normalize(instrument_id(item)[1])
                       and instrument_id(item)[0] == args.market_id]
            print(f"Catalog: {len(instruments)} instruments; {len(matches)} search matches.")
            for item in matches:
                market, symbol = instrument_id(item)
                print(f"  {market} | {symbol} | CFI: {item.get('cficode', 'unknown')}")

            report = {"environment": "REMARKETS", "base_url": BASE_URL,
                      "segments": segments, "catalog": catalog, "matches": matches}
            if args.symbol:
                if not any(instrument_id(item) == (args.market_id, args.symbol) for item in instruments):
                    raise PrimaryError("Exact symbol/market pair not found in catalog; run discovery first.")
                params = {"marketId": args.market_id, "symbol": args.symbol}
                detail = get_json(session, "/rest/instruments/detail", **params)
                print("Instrument details:")
                print(json.dumps(require_field(detail, "instrument", dict), indent=2))
                snapshot = get_json(session, "/rest/marketdata/get", **params,
                                    entries=ENTRIES, depth=args.depth)
                received_at = datetime.now(timezone.utc).isoformat()
                print(f"Snapshot received at {received_at} (local receipt time, not quote age).")
                print_snapshot(require_field(snapshot, "marketData", dict))
                report.update(instrument=detail, snapshot=snapshot, received_at_utc=received_at)
            else:
                print('Discovery complete. Rerun with --symbol "EXACT_SYMBOL" to retrieve a snapshot.')
                if not matches:
                    print('Try --search "RFX" or --search "" to inspect the catalog.')
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                with args.output.open("x", encoding="utf-8") as output:
                    json.dump(report, output, indent=2, ensure_ascii=True)
                print(f"Saved data to {args.output}")
        return 0
    except (PrimaryError, OSError, EOFError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())

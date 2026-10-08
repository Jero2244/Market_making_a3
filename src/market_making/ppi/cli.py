"""Network is opt-in. Only sanitized summaries leave this command."""
import argparse
import json
import math
from .config import load_credentials
from .client import Client, PPIError, safe_error_code
from .discovery import discover
from .arbitrage import fair_future, period_rate


def positive_integer(value):
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return result


def interval_seconds(value):
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise argparse.ArgumentTypeError("must be finite and nonnegative")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="PPI read-only GGAL discovery and carry research")
    parser.add_argument("command", choices=("credentials-status", "discover", "assess", "watch"))
    parser.add_argument("--env-file", default=".env")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--live", action="store_true", help="Authorize bounded PPI production authentication and market-data reads")
    modes.add_argument("--demo", action="store_true", help="Synthetic offline watch; no credentials or network")
    parser.add_argument("--watch-config", help="PPI production spot + REMARKETS simulated futures assumptions JSON; PRIMARY_USER/PRIMARY_PASSWORD required")
    parser.add_argument("--interval", type=interval_seconds, default=30, help="Seconds between batches (live minimum/default 30)")
    parser.add_argument("--iterations", type=positive_integer, help="Positive cycle count; omitted means until Ctrl+C")
    parser.add_argument("--json", action="store_true", help="Watch NDJSON: one record per expiry per cycle")
    parser.add_argument("--manual-check", action="store_true", help="Watch only: compare quoted prices despite timestamp/last-trade diagnostics; never fresh or executable")
    parser.add_argument("--spot", type=float, help="Explicit hypothetical ARS/share scenario price, NOT live book")
    parser.add_argument("--tna", type=float, help="Explicit hypothetical percent annual nominal rate")
    parser.add_argument("--days", type=float, help="Explicit scenario horizon; NOT inferred future maturity")
    parser.add_argument("--basis", type=int, choices=(360, 365))
    parser.add_argument("--caucion-ticker", help="Discover/assess only: exact broker-reviewed ARS caucion identifier; no tenor or rate inference")
    args = parser.parse_args(argv)
    if args.command == "watch":
        if any(value is not None for value in (args.spot, args.tna, args.days, args.basis, args.caucion_ticker)):
            parser.error("assessment scenario options are not accepted by watch")
        if not (args.demo or args.live):
            parser.error("watch requires explicit --demo or --live")
        from .monitor import watch
        return watch(args)
    if args.demo or args.watch_config or args.json or args.manual_check or args.iterations is not None or args.interval != 30:
        parser.error("watch options require watch command")
    if args.caucion_ticker is not None:
        from .discovery import label
        if args.command == "credentials-status" or not label(args.caucion_ticker) or not args.caucion_ticker.strip():
            parser.error("caucion ticker requires discover/assess and a bounded nonempty identifier")
    client = None
    try:
        credentials = load_credentials(args.env_file)
        if args.command == "credentials-status":
            print(json.dumps(credentials.statuses(), indent=2, allow_nan=False))
            return 0
        report = {"executable": False, "blockers": []}
        code = 0
        if args.live:
            if not credentials.ready:
                report["blockers"].append("credentials_unavailable_check_PPI_key_secret_and_client_overrides")
                code = 2
            else:
                client = Client(credentials, live=True)
                client.login()
                report = discover(client, caucion_ticker=args.caucion_ticker)
        else:
            report["blockers"].append("network_disabled_use_live_for_read_only_discovery")
        if args.command == "assess":
            supplied = (args.spot, args.tna, args.days, args.basis)
            if all(x is not None for x in supplied):
                try:
                    report["hypothetical_scenario"] = {
                        "formula": "F=S(1+r)", "r": period_rate(args.tna, args.days, args.basis),
                        "F": fair_future(args.spot, args.tna, args.days, args.basis),
                        "days": args.days, "basis": args.basis,
                        "overnight_rollover": "hypothetical constant TNA; no guaranteed future financing",
                        "not_live_or_executable": True,
                    }
                except (ValueError, OverflowError):
                    report["blockers"].append("invalid_hypothetical_scenario")
                    code = 2
            elif any(x is not None for x in supplied):
                report["blockers"].append("scenario_requires_spot_tna_days_and_basis")
                code = 2
            report["blockers"].append("executable_edges_require_verified_contract_rate_sides_books_and_all_in_costs")
        print(json.dumps(report, indent=2, default=lambda x: x.isoformat(), allow_nan=False))
        return code
    except PPIError as exc:
        print(json.dumps({"executable": False, "blockers": [safe_error_code(exc)]}, allow_nan=False))
        return 2
    except Exception:
        print(json.dumps({"executable": False, "blockers": ["local_configuration_or_processing_failure"]}, allow_nan=False))
        return 2
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())

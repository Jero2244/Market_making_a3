"""Recurring non-executable carry proxy. No discovery or trading requests."""
import json
import math
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .arbitrage import fair_future, finite, period_rate, price_diagnostics, MANUAL_FRESHNESS_CAVEAT
from .client import Client, PPIError
from .config import load_credentials
from .models import Book, number, parse_book, timestamp
from .monitor_config import TARGETS, SYMBOLS, load_watch_config, default_watch_config
from .remarkets import Client as RemarketsClient, load_credentials as load_primary_credentials, parse_snapshot

NO_EDGE = "SIN ARBITRAJE TEORICO"
EDGE = "ARBITRAJE TEORICO DISPONIBLE"
UNAVAILABLE = "NO EVALUABLE"
UNVERIFIED = "source_timestamp_scope_clock_and_session_unverified"
TRANSIENT_FAILURES = frozenset(("http_failure_429", "http_failure_500", "http_failure_502",
                                "http_failure_503", "http_failure_504", "transport_or_json_failure"))


def quote_summary(spot, future, config, contract, now):
    """Gross cash-carry TNA from quotes; independent of freshness eligibility."""
    summary = {"implied_yield_tna": None, "caucion_tna": None,
               "caucion_source": "configured_borrow_tna", "yield_direction": "cash_carry"}
    try:
        summary["caucion_tna"] = number(config["rates"]["borrow_tna"], positive=False)
    except (ValueError, TypeError, KeyError):
        pass
    try:
        # Timestamp diagnostics remain visible in the assessment. Structural
        # book/identity errors must never produce a numerical yield.
        if any(price_diagnostics(book.blockers, True)[0] for book in (spot, future)):
            return summary
        for book in (spot, future):
            if number(book.bid.price) >= number(book.ask.price):
                return summary
        days = number((timestamp(contract["maturity"]) - now).total_seconds() / 86400)
        basis = config["rates"]["basis"]
        if basis not in (360, 365):
            return summary
        observed = finite(number(future.bid.price) * number(contract["price_scale"]))
        summary["implied_yield_tna"] = finite((observed / number(spot.ask.price) - 1) * basis / days * 100)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        pass
    return summary


def book_record(book, *, symbol, market, provider, environment, identity_basis=None, now=None, max_age=30):
    """Normalized display data; receipt never certifies source freshness."""
    def rows(levels):
        return [{"price": level.price, "quantity": level.quantity} for level in levels]
    def age(value):
        if now is None or value is None or value.tzinfo is None or now.tzinfo is None:
            return None
        return finite((now - value).total_seconds())
    source_age = age(book.source_time)
    warnings = [x for x in book.blockers if x.startswith("source_timestamp_")]
    if source_age is None:
        warnings.append("source_timestamp_missing_or_ambiguous")
    elif not -2 <= source_age <= max_age:
        warnings.append("source_timestamp_stale_or_future")
    return {"symbol": symbol, "market": market, "provider": provider,
            "environment": environment, "simulated": environment != "production",
            "bids": rows(book.bids), "offers": rows(book.offers),
            "bid_count": len(book.bids), "offer_count": len(book.offers),
            "requested_depth": book.requested_depth,
            "receipt_timestamp": book.receipt_time.isoformat(),
             "source_timestamp": book.source_time.isoformat() if book.source_time else None,
             "source_age_seconds": source_age, "receipt_age_seconds": age(book.receipt_time),
             "timestamp_warnings": list(dict.fromkeys(warnings)),
            "source_timestamp_verified": False,
            "identity_basis": book.identity_basis or identity_basis,
            "diagnostics": list(book.blockers)}


def empty_result(expiry, mode, cycle, now, blockers, *, manual_check=False):
    return {"cycle": cycle, "expiry": expiry, "as_of": now.isoformat(), "mode": mode,
            "status": UNAVAILABLE, "executable": False, "live_freshness_established": False,
            "provenance": {"spot": "synthetic" if mode == "DEMO" else "PPI production",
                           "futures": "synthetic" if mode == "DEMO" else "REMARKETS simulated"},
             "selected_direction": None, "manual_check": manual_check,
              "books": None,
              "quote_summary": {"implied_yield_tna": None, "caucion_tna": None,
                                "caucion_source": "configured_borrow_tna", "yield_direction": "cash_carry"},
              "timing": {"source_skew_seconds": None, "receipt_skew_seconds": None,
                         "policy": "advisory_manual" if manual_check else "strict_source_age",
                         "receipt_is_source_freshness": False},
            "directions": {d: {"theoretical": None, "observed": None, "difference": None,
                               "net_edge": None, "eligible": False, "blockers": ["inputs_unavailable"]}
                           for d in ("cash_carry", "reverse")}, "blockers": list(blockers),
            "caveats": ([MANUAL_FRESHNESS_CAVEAT] if manual_check else []) + ["synthetic_fixture" if mode == "DEMO" else UNVERIFIED,
                        "constant_rate_rollover_proxy_not_locked_funding",
                        "metadata_costs_rates_are_user_assumptions_not_verified",
                        "short_dividends_margin_entitlement_and_simultaneous_fills_unverified"] +
                        ([] if mode == "DEMO" else ["mixed_production_spot_and_simulated_futures"])}


def evaluate(spot, future, config, contract, *, now, cycle=1, mode="LIVE-PROXY", manual_check=False):
    """Both signed theoretical-minus-observed differences in ARS/share.

    Strict proxy evaluation permits only the unverified-scope caveat. Explicit
    manual checks also make timestamp and last-trade diagnostics advisory.
    """
    result = empty_result(contract["expiry"], mode, cycle, now, [], manual_check=manual_check)
    result["quote_summary"] = quote_summary(spot, future, config, contract, now)
    result["books"] = {
        "spot": book_record(spot, symbol="GGAL", market="BYMA", provider="synthetic" if mode == "DEMO" else "ppi",
                             environment="synthetic" if mode == "DEMO" else "production", identity_basis="request-bound",
                             now=now, max_age=config.get("max_age_seconds", 30)),
        "future": book_record(future, symbol=contract.get("symbol"), market=contract.get("market_id"),
                              provider="synthetic" if mode == "DEMO" else "remarkets",
                               environment="synthetic" if mode == "DEMO" else "REMARKETS simulated",
                               now=now, max_age=config.get("max_age_seconds", 30))}
    for field, attribute in (("source_skew_seconds", "source_time"), ("receipt_skew_seconds", "receipt_time")):
        times = [getattr(book, attribute) for book in (spot, future)]
        if all(t is not None and t.tzinfo is not None for t in times):
            result["timing"][field] = finite(abs((times[0] - times[1]).total_seconds()))
    if config.get("quote_only"):
        result["quote_only"] = True
        result["assumptions"] = {"maturity": contract["maturity"], "maturity_rule": "last_weekday_month_end_Buenos_Aires",
                                 "price_unit": "ARS", "price_scale": 1, "basis": config["rates"]["basis"]}
        result["timing"]["policy"] = "advisory_price_comparison"
        result["caveats"].append("user_assumed_maturity_and_peso_units")
        for book in (spot, future):
            blocking, advisory = price_diagnostics(book.blockers, True)
            result["blockers"].extend(blocking)
            result["caveats"].extend(x for x in advisory if x not in result["caveats"])
        if result["quote_summary"]["implied_yield_tna"] is None and not result["blockers"]:
            result["blockers"].append("price_yield_unavailable")
        result["status"] = UNAVAILABLE if result["blockers"] else "PRICE CHECK"
        return result
    blockers = result["blockers"]
    try:
        if now.tzinfo is None:
            raise ValueError
        days = number((timestamp(contract["maturity"]) - now).total_seconds() / 86400)
        scale = number(contract["price_scale"])
        multiplier = number(contract["multiplier"])
        requested = number(config["contracts"])
        if requested != int(requested):
            raise ValueError
        threshold = number(config["threshold"], positive=False)
        max_age = number(config["max_age_seconds"])
        for book in (spot, future):
            diagnostics = list(book.blockers)
            if book.source_time is None or book.source_time.tzinfo is None:
                diagnostics.append("source_timestamp_missing_or_ambiguous")
            elif not -2 <= (now - book.source_time).total_seconds() <= max_age:
                diagnostics.append("source_timestamp_stale_or_future")
            blocking, advisory = price_diagnostics(diagnostics, manual_check)
            blockers.extend(x for x in blocking if x != UNVERIFIED)
            result["caveats"].extend(x for x in advisory if x not in result["caveats"])
            for level in (book.bid, book.ask):
                number(level.price)
                number(level.quantity)
            if book.bid.price >= book.ask.price:
                blockers.append("crossed_or_locked_book")
        if blockers:
            return result
        result["days_to_maturity"] = days
        rates = config["rates"]
        for direction, rate_key, spot_side, future_side, sign in (
                ("cash_carry", "borrow_tna", spot.ask, future.bid, -1),
                ("reverse", "lend_tna", spot.bid, future.ask, 1)):
            item = {"theoretical": None, "observed": None, "difference": None,
                    "net_edge": None, "eligible": False, "blockers": []}
            result["directions"][direction] = item
            try:
                theoretical = fair_future(spot_side.price, rates[rate_key], days, rates["basis"])
                observed = finite(future_side.price * scale)
                difference = finite(theoretical - observed)
                capacity = math.floor(min(finite(spot_side.quantity / multiplier), future_side.quantity))
                costs = number(contract["costs"][direction], positive=False)
                edge = finite(sign * difference - costs)
                item.update(theoretical=theoretical, observed=observed, difference=difference,
                            net_edge=edge, costs=costs, capacity_contracts=capacity,
                            period_rate=period_rate(rates[rate_key], days, rates["basis"]))
                if capacity < requested:
                    item["blockers"].append("insufficient_depth")
                item["eligible"] = not item["blockers"] and edge > threshold
            except (ValueError, TypeError, KeyError, OverflowError):
                item["blockers"].append("rate_cost_or_calculation_unavailable")
        eligible = [d for d, item in result["directions"].items() if item["eligible"]]
        if eligible:
            # Insertion order cash_carry then reverse breaks equal-edge ties.
            result["selected_direction"] = max(eligible, key=lambda d: result["directions"][d]["net_edge"])
            result["status"] = EDGE
        elif any(item["blockers"] for item in result["directions"].values()):
            blockers.append("one_or_more_directions_unavailable")
        else:
            result["status"] = NO_EDGE
            result["selected_direction"] = max(result["directions"], key=lambda d: result["directions"][d]["net_edge"])
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        blockers.append("invalid_or_missing_book_horizon_units_or_inputs")
    return result


def demo_cycle(cycle, *, manual_check=False):
    """Repeat four deterministic scenarios; never load credentials or network."""
    now = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
    scenario = (cycle - 1) % 4
    config = {"contracts": 1, "threshold": 0, "max_age_seconds": 30,
              "rates": {"borrow_tna": 36, "lend_tna": 30, "basis": 365}}

    def book(bid, ask):
        return parse_book({"bids": [{"price": bid, "quantity": 1000}],
                           "offers": [{"price": ask, "quantity": 1000}],
                           "date": now.isoformat()}, receipt=now)

    spot = book(100, 101)
    reports = []
    for expiry in TARGETS:
        maturity = expiry + "-30T12:00:00+00:00"
        contract = {"expiry": expiry, "maturity": maturity, "price_scale": 1,
                    "symbol": SYMBOLS[expiry], "market_id": "ROFX",
                    "multiplier": 10, "costs": {"cash_carry": 0.5, "reverse": 0.5}}
        days = (timestamp(maturity) - now).total_seconds() / 86400
        lower = fair_future(100, 30, days, 365)
        upper = fair_future(101, 36, days, 365)
        if scenario == 0:
            future = book(lower, upper)
        elif scenario == 1:
            future = book(upper + 5, upper + 6)
        elif scenario == 2:
            future = book(lower - 6, lower - 5)
        else:
            future = parse_book({}, receipt=now)
        report = evaluate(spot, future, config, contract, now=now, cycle=cycle, mode="DEMO", manual_check=manual_check)
        report["scenario"] = ("no_edge", "cash_carry", "reverse", "unavailable")[scenario]
        reports.append(report)
    return reports


def emit(reports, json_output=False, verbose=False):
    for report in reports:
        if json_output:
            print(json.dumps(report, allow_nan=False), flush=True)
        else:
            books = report.get("books") or {}
            summary = report.get("quote_summary") or {}
            def price(leg, side):
                rows = books.get(leg, {}).get(side, [])
                return f'{rows[0]["price"]:.2f}' if rows else "n/a"
            def percentage(key):
                value = summary.get(key)
                return "n/a" if value is None else f"{value:.2f}%"
            print(f'{report["mode"]} #{report["cycle"]} GGAL {report["expiry"]} '
                  + ('MANUAL-CHECK ' if report["manual_check"] else '') +
                  f'| remarkets book: bid {price("future", "bids")} | ask {price("future", "offers")}, '
                  f'ppi ggal price: bid {price("spot", "bids")} | ask {price("spot", "offers")} '
                  f'implied yield {percentage("implied_yield_tna")} caucion {percentage("caucion_tna")} '
                  f'(configured borrow TNA) | {report["status"]} | NONEXECUTABLE'
                  + (" | PPI production + REMARKETS simulated" if report["mode"] != "DEMO" else "")
                  + (" | " + ",".join(report["blockers"]) if report["blockers"] else ""), flush=True)
            if not verbose:
                continue
            direction = report["selected_direction"]
            item = report["directions"].get(direction, {})
            def fmt(key):
                value = item.get(key)
                return "n/a" if value is None else f"{value:+.4f}"
            print(f'{report["mode"]} #{report["cycle"]} GGAL {report["expiry"]} '
                  + ('MANUAL-CHECK ' if report["manual_check"] else '') +
                  f'{report["status"]} | {direction or "n/a"} '
                  f'Fteo={fmt("theoretical")} Fobs={fmt("observed")} '
                  f'Fteo-Fobs={fmt("difference")} net={fmt("net_edge")} ARS/share '
                  f'| NONEXECUTABLE' + (" | " + ",".join(report["blockers"]) if report["blockers"] else "")
                  + (" | PPI production spot + REMARKETS simulated futures" if report["mode"] != "DEMO" else "")
                   + (" | caveats=" + ",".join(report["caveats"]) if report["manual_check"] else ""), flush=True)
            for name, item in report["directions"].items():
                print(f'  {name}: Fteo={item["theoretical"]} Fobs={item["observed"]} '
                      f'Fteo-Fobs={item["difference"]} net={item["net_edge"]} ARS/share '
                      f'eligible={item["eligible"]} capacity={item.get("capacity_contracts")} '
                      f'blockers={",".join(item["blockers"])}', flush=True)
            if report.get("books"):
                for leg, book in report["books"].items():
                    def levels(side):
                        return ",".join(f'{row["price"]:g}x{row["quantity"]:g}' for row in book[side]) or "n/a"
                    print(f'  BOOK {leg} {book["symbol"]} {book["market"]} {book["environment"]} '
                          f'BI[{book["bid_count"]}]={levels("bids")} OF[{book["offer_count"]}]={levels("offers")} '
                          f'depth={book["requested_depth"]} identity={book["identity_basis"]} '
                          f'receipt={book["receipt_timestamp"]} source={book["source_timestamp"] or "null"} (unverified) '
                          f'source_age_s={book["source_age_seconds"]} receipt_age_s={book["receipt_age_seconds"]} '
                          f'warnings={",".join(book["timestamp_warnings"])}', flush=True)
            print(f'  TIMING policy={report["timing"]["policy"]} '
                  f'source_skew_s={report["timing"]["source_skew_seconds"]} '
                  f'receipt_skew_s={report["timing"]["receipt_skew_seconds"]} '
                  'live_freshness_established=false', flush=True)


class WatchSession:
    """One session per provider; independent provider reads overlap safely.

    Clients are constructed lazily, each used by only one task at a time.
    Tokens stay in memory. No former-cycle book is cached.
    """

    def __init__(self, credentials, primary_credentials):
        self.credentials = credentials
        self.primary_credentials = primary_credentials
        self.client = self.primary = self.executor = None
        self.cancelled = threading.Event()

    def cancel(self):
        self.cancelled.set()
        for client in (self.client, self.primary):
            if client is not None:
                client.cancel()

    def _check_cancelled(self):
        if self.cancelled.is_set():
            raise PPIError("response_limit_exceeded")

    def close(self):
        if self.executor is not None:
            self.executor.shutdown(wait=True, cancel_futures=True)
            self.executor = None
        try:
            if self.primary is not None:
                self.primary.close()
                self.primary = None
        finally:
            if self.client is not None:
                self.client.close()
                self.client = None

    @staticmethod
    def _retry_401(client, read, recovery):
        try:
            return read()
        except PPIError as exc:
            if str(exc) != "http_failure_401" or recovery[0]:
                raise
            recovery[0] = True
            client.renew_authentication()
            return read()  # One retry only; 403, bad credentials and unknown errors stop.

    def read(self, config, cycle, manual_check=False):
        self._check_cancelled()
        if not self.credentials.ready or not self.primary_credentials.ready:
            raise PPIError("credentials_unavailable")
        if self.client is None:
            self.client = Client(self.credentials, live=True, max_requests=2)
        if self.primary is None:
            self.primary = RemarketsClient(self.primary_credentials, live=True, max_requests=3)
        if self.executor is None:
            self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ggal-provider")
        started = time.monotonic()

        def spot_read():
            self._check_cancelled()
            self.client.begin_watch_cycle()
            self.client.ensure_authenticated()
            params = {key.capitalize(): config["spot"][key] for key in ("ticker", "type", "settlement")}
            raw = self._retry_401(self.client, lambda: self.client.get("MarketData/Book", params), [False])
            return parse_book(raw, receipt=datetime.now(timezone.utc), max_age=config["max_age_seconds"])

        def futures_read():
            self._check_cancelled()
            self.primary.begin_watch_cycle()
            self.primary.ensure_authenticated()
            recovery = [False]
            books = []
            for instrument in config["futures"]:
                self._check_cancelled()
                try:
                    snapshot = self._retry_401(self.primary, lambda: self.primary.snapshot(instrument), recovery)
                except PPIError as exc:
                    if str(exc) not in TRANSIENT_FAILURES:
                        raise
                    books.append(Book(None, None, None, None, None, datetime.now(timezone.utc),
                                      ["transient_market_data_failure"]))
                    if str(exc) == "http_failure_429":
                        # Do not make another quote request to a throttled provider.
                        books.extend(Book(None, None, None, None, None, datetime.now(timezone.utc),
                                          ["transient_market_data_failure"])
                                     for _ in range(len(config["futures"]) - len(books)))
                        break
                else:
                    books.append(parse_snapshot(snapshot, instrument, receipt=datetime.now(timezone.utc),
                                                max_age=config["max_age_seconds"]))
            return books

        spot_job = self.executor.submit(spot_read)
        futures_job = self.executor.submit(futures_read)
        # Await both providers before returning or propagating a failure. Thus a
        # later cycle cannot overlap a still-running earlier provider request.
        spot_error = future_error = None
        try:
            spot = spot_job.result()
        except BaseException as exc:
            spot_error = exc
        try:
            futures = futures_job.result()
        except BaseException as exc:
            future_error = exc
        if spot_error is not None or future_error is not None:
            # Authentication/security failures outrank a simultaneous transient
            # failure so that the watch cannot keep retrying revoked access.
            errors = [exc for exc in (spot_error, future_error) if exc is not None]
            raise next((exc for exc in errors if not isinstance(exc, PPIError)
                        or str(exc) not in TRANSIENT_FAILURES), errors[0])
        now = datetime.now(timezone.utc)
        reports = [evaluate(spot, futures[i], config, contract, now=now, cycle=cycle, manual_check=manual_check)
                   for i, contract in enumerate(config["futures"])]
        for report in reports:
            report["acquisition_seconds"] = round(time.monotonic() - started, 3)
        return reports


def refresh_delay(reports, interval, failures):
    """Back off unavailable transports, not valid empty/unchanged market books."""
    transient = any("transient_market_data_failure" in report["blockers"] for report in reports)
    failures = failures + 1 if transient else 0
    elapsed = reports[0].get("acquisition_seconds", 0) if reports else 0
    delay = (max(interval, min(300, 30 * 2 ** min(failures - 1, 4))) if transient else
             max(0, interval - elapsed))
    return delay, failures


def live_cycle(config, credentials, cycle, *, primary_credentials, manual_check=False, session=None):
    """One PPI spot and two REMARKETS futures, with no fallback."""
    if session is not None:
        return session.read(config, cycle, manual_check)
    if not credentials.ready or not primary_credentials.ready:
        raise PPIError("credentials_unavailable")
    client = primary = None
    try:
        client = Client(credentials, live=True, max_requests=2)
        primary = RemarketsClient(primary_credentials, live=True, max_requests=3)
        client.login()
        primary.login()
        params = {key.capitalize(): config["spot"][key] for key in ("ticker", "type", "settlement")}
        books = [parse_book(client.get("MarketData/Book", params), receipt=datetime.now(timezone.utc),
                            max_age=config["max_age_seconds"])]
        for instrument in config["futures"]:
            try:
                snapshot = primary.snapshot(instrument)
            except PPIError as exc:
                if str(exc) not in TRANSIENT_FAILURES:
                    # Auth/security/unknown failures terminate the cycle, sanitized
                    # by watch. Only explicitly recoverable reads are leg-local.
                    raise
                books.append(Book(None, None, None, None, None, datetime.now(timezone.utc),
                                  ["transient_market_data_failure"]))
            else:
                books.append(parse_snapshot(snapshot, instrument,
                                            receipt=datetime.now(timezone.utc), max_age=config["max_age_seconds"]))
        # Evaluate each expiry independently. Structural diagnostics remain
        # blockers on that pair, without discarding the other valid book.
        now = datetime.now(timezone.utc)
        return [evaluate(books[0], books[i + 1], config, contract, now=now, cycle=cycle, manual_check=manual_check)
                for i, contract in enumerate(config["futures"])]
    finally:
        try:
            if primary is not None:
                primary.close()
        finally:
            if client is not None:
                client.close()


def watch(args):
    mode = "DEMO" if args.demo else "LIVE-PROXY"
    manual_check = args.manual_check
    cycle = 0
    session = None
    failures = 0
    try:
        if args.demo:
            if args.watch_config:
                raise ValueError("demo_has_no_external_inputs")
            config = credentials = None
        else:
            if not args.live or args.interval < 1:
                raise ValueError("live_requires_interval_at_least_1_second")
            config = (load_watch_config(args.watch_config) if args.watch_config else
                      default_watch_config(getattr(args, "caucion_tna", None)))
            now = datetime.now(timezone.utc)
            if any(timestamp(c["maturity"]) <= now for c in config["futures"]):
                raise ValueError("expired_watch_maturity")
            credentials = load_credentials(args.env_file)
            primary_credentials = load_primary_credentials(args.env_file)
            if not credentials.ready or not primary_credentials.ready:
                raise ValueError("credentials_unavailable")
            session = WatchSession(credentials, primary_credentials)
        while args.iterations is None or cycle < args.iterations:
            cycle += 1
            stop = False
            try:
                reports = (demo_cycle(cycle, manual_check=manual_check) if args.demo else
                           live_cycle(config, credentials, cycle, primary_credentials=primary_credentials,
                                      manual_check=manual_check, session=session))
            except PPIError as exc:
                # Never interpolate arbitrary exception text, even from a fake transport.
                code = str(exc)
                auth = code in ("http_failure_401", "http_failure_403", "authentication_missing_or_expired",
                                "invalid_authentication_response", "credentials_unavailable")
                stop = auth or code not in TRANSIENT_FAILURES
                reason = "authentication_failure_stop" if auth else ("transient_market_data_failure" if not stop else "safe_market_data_failure_stop")
                reports = [empty_result(e, mode, cycle, datetime.now(timezone.utc), [reason], manual_check=manual_check) for e in TARGETS]
            emit(reports, args.json, getattr(args, "verbose", False))
            if stop:
                return 2
            if args.iterations is None or cycle < args.iterations:
                delay, failures = refresh_delay(reports, args.interval, failures)
                time.sleep(delay)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        reason = "local_watch_configuration_or_processing_failure"
        if isinstance(exc, ValueError) and str(exc) == "legacy_PPI_futures_not_supported_use_remarkets_symbol_and_market_id":
            reason = "legacy_PPI_futures_not_supported_use_remarkets_symbol_and_market_id"
        emit([empty_result(e, mode, cycle, datetime.now(timezone.utc),
                           [reason], manual_check=manual_check) for e in TARGETS], args.json,
             getattr(args, "verbose", False))
        return 2
    finally:
        if session is not None:
            session.close()

# PPI GGAL read-only carry research

This document uses simplified technical English in the style of ASD-STE100.
It does not claim certified or full compliance. No dictionary audit was done.

The adapter is separate from Primary. It never imports the order harness.
It has no order endpoints or account-balance endpoints.
This adapter is for research. It does not authorize trading.

## Current checkpoint — 2026-10-09

The full project offline suite passes **249 tests**. It covers PPI discovery,
carry calculations, bounded transports, recurring watch output, strict/manual
checks and request-bound REMARKETS depth, plus existing RFX20 regressions.
It also covers persistent authentication, refresh tokens, bounded recovery,
provider overlap, polling backoff, pooled-connection deadlines and cancellation,
and desktop lifecycle behavior. Source and packaged GUI widget checks passed.
The synthetic watch works without credentials. The live-input path is implemented,
but offline success is not broker entitlement or live acceptance evidence.
No credentialed refresh benchmark or order was made during the desktop/session
update. The separately recorded 2026-10-09 PPI caucion inspection below does not
establish live refresh performance or futures freshness.
Contract, funding and freshness blockers below remain unresolved.

The desktop checker is documented in [DESKTOP.md](DESKTOP.md). It provides the
simple price/yield comparison and optional full assessment. Recurring GUI/CLI
monitoring uses persistent sessions, a 5-second default and 1-second application
minimum, with bounded renewal and automatic provider backoff. Details and
provider sources: [REFRESH_PERFORMANCE.md](REFRESH_PERFORMANCE.md).

## Credentials and setup

### Warnings

- Keep secrets in the ignored local `.env` file.
- Do not commit `.env`. Do not add it with `git add -f`.
- Do not paste keys, tokens, raw responses or tracebacks into reports.
- Do not search unrelated files or credential stores.
- Do not generate credentials or borrow them from Primary.
- Production SDK application identifiers are defaults, not account credentials.
- Credentials alone are not sufficient. Contract metadata, funding and freshness
  requirements can still block assessment or executability.

### Setup procedure

1. Use Python 3.10+.
2. Install the project with the existing installation command:
   `python -m pip install -e .`.
3. Get production credentials from PPI Gestiones > Gestión de servicio API.
4. Check that git excludes the local `.env` file.
5. On Windows, review the local `.env` ACLs.
6. Set `PPI_API_KEY` locally in the ignored `.env` file.
7. Set `PPI_API_SECRET` locally in the ignored `.env` file.
8. Optionally set `PPI_AUTHORIZED_CLIENT` to override the official SDK production default.
9. Optionally set `PPI_CLIENT_KEY` to override the official SDK production default.
10. Check credential status:
    `python -m market_making.ppi.cli credentials-status`.
11. Run offline discovery:
    `python -m market_making.ppi.cli discover`.
12. To opt in to bounded production reads, run live discovery:
    `python -m market_making.ppi.cli discover --live`.
13. To request reads and explicit blockers, run live assessment:
    `python -m market_making.ppi.cli assess --live`.

### Notes

- Named environment variables take precedence over `.env` values.
- Blank or placeholder API key/secret values block login. Absent or blank client
  identifiers use the official `ppi-client` 1.3.0 production defaults; explicit
  placeholder overrides still block login. Defaults are not written to `.env`.
- POSIX mode bits do not establish Windows ACL isolation.
- Offline discovery makes no requests.
- The installation also exposes `ppi-readonly`.
- No output evidence file is created.
- `config.ensure_env` appends absent names. It does not rewrite existing bytes.
  The caller must first check git exclusion. The helper reuses only the four
  named process environment credentials. Otherwise, it adds blank values.

### Hypothetical calculation

**Warning:** These inputs are explicit hypothetical inputs. They are NOT observed
market values.

```powershell
python -m market_making.ppi.cli assess --spot 100 --tna 30 --days 30 --basis 365
```

## Protocol sources inspected (2026-10-07)

- [Official REST docs](https://itatppi.github.io/ppi-official-api-docs/api/documentacionRest/)
  specify API **1.0** and POST `/Account/LoginApi`.
  The headers are `ApiKey`, `ApiSecret`, `AuthorizedClient` and `ClientKey`.
  Authenticated reads use Bearer authorization.
- [Official Python examples](https://github.com/itAtPPI/ppi-connector-python)
  use the `ppi-client` dependency. PyPI `ppi-client` **1.3.0** source was inspected.
  Its trading client was not installed or imported for that inspection.
  `ppi_restclient.py` verifies the production URL:
  `https://clientapi.portfoliopersonal.com/api/`.
  `api/constants.py` uses **1.0** paths.
  The SDK library version is not the API version.
  `ppi_api_client.py` verifies the header names and Bearer scheme.
  The SDK bundles production application identifiers. This adapter uses the same
  defaults, with optional explicit overrides, so key/secret-only setup works.
- The docs show a singleton-array login response. The SDK uses an object.
  The adapter accepts either form.
  It requires an `expirationDate` with timezone information and a future value.
  It fails closed for malformed authentication. Recurring monitoring renews
  ahead of the returned expiry and uses `Account/RefreshToken` when supplied.
  A quote HTTP 401 permits one renewal/retry per provider per cycle; 403,
  repeated rejection or failed renewal stops. Other one-shot commands retain
  their bounded, explicit-login behavior.

### Transport safeguards

The transport permits only login/refresh-token POST and these GET reads:
InstrumentTypes, Markets, Settlements, SearchInstrument, Book and Current.

- The HTTPS production host is fixed.
- Certificate validation is enabled.
- Redirects are not permitted.
- Environment proxy/netrc settings are not used.
- Connect/read inactivity timeouts are at most 5/10 seconds, capped by remaining budget.
- One-shot discovery has a maximum of 32 requests; recurring monitoring starts
  a new budget of at most four PPI requests per cycle, including auth/recovery.
- The checked deadline is 120 seconds.
- The decompressed response ceiling is 2 MB.

Deadline checks occur between requests/chunks, and a separate monotonic watchdog
shuts down active transport sockets at the deadline. It owns a duplicate raw
socket handle before TLS wrapping, so cancellation remains possible throughout
the TLS handshake even after wrapping detaches the original handle. This interrupts
stalled handshakes, blocked header reads and streamed/decompressed body reads, including continuously dripping
bytes. Read timeouts alone are inactivity limits, not total elapsed-time limits.
The request thread closes responses/connections; the watchdog is joined on exit.
Client cleanup also closes all owned cancellation handles. Certificate verification
remains enabled. OS DNS resolution before a socket exists is not interruptible
by this socket watchdog.
Credentials are not put in query parameters, server error text or logs.
Tokens are not cached on disk.
Public documentation retrieval is separate from the CLI market-data network opt-in.
If decoded responses or query parameters echo credentials or tokens, the adapter
withholds them with a static error. This includes nested or escaped JSON strings.

**Warning:** A trusted production host does not guarantee freshness.

## Discovery and limits

Configuration identifies **available** instrument types, markets and settlements.
It does not establish settlement compatibility for each instrument.

Discovery searches for GGAL in ACCIONES.
It filters for exact GGAL/BYMA/ARS matches to exclude USA ADRs and CEDEARs.
The production currency label `Pesos` is normalized to `ARS` before this filter.
It searches returned FUTUROS broadly.
It retains only GGAL name/ticker **candidates**.
CAUCIONES enumeration with an empty ticker is not established by the official
sources. Default discovery therefore reports
`CAUCIONES:broad_search_not_documented_explicit_ticker_required` and does not send
that request. With `--caucion-ticker`, it searches that explicit identifier and
retains only exact returned CAUCIONES/ARS matches.
It does **not** infer a one-day tenor from the ticker.
It reports missing or unsupported categories and request failures.
It does not fabricate tickers or contracts.

At most two candidates per category and three discovered settlement strings
receive Book/Current requests. The overall budget still applies.
Truncation is explicit.

**Warning:** No matching returned future does not prove that none exists.
Naming conventions, broker coverage, search semantics and incomplete scans can
hide a contract. Candidate books are diagnostic.
They are not certified GGAL future books or one-day caucion books.

### Caucion HTTP 400 diagnosis and limitation (2026-10-08)

The repository implementation previously sent `Ticker=""`, `Type="CAUCIONES"`
to SearchInstrument. It then sent returned identifiers to Book and Current for
up to three configuration settlements. No captured failing live response or
original user command was supplied for this change. The exact failing stage and
the server's reason are **not established**. An HTTP 400 alone does not establish
a wrong type, ticker, settlement or account entitlement.

The official REST documentation marks SearchInstrument `Ticker` as required.
`Name`, `Market` and `Type` are optional. Configuration examples include
`CAUCIONES`, and settlements include `INMEDIATA`, `A-24HS`, `A-48HS`, `A-72HS`.
The official Python documentation and public PyPI `ppi-client` 1.3.0 source
(`api/marketdata.py`, inspected without installation or import) expose the same
search fields and ticker/type/settlement Book and Current calls. They do not
document a one-day caucion ticker, blank-ticker caucion enumeration, rate units,
borrow/lend mapping or caucion-specific settlement compatibility.

This is a conservative capability restriction, **not a proven live HTTP 400
root-cause repair**. It removes the undocumented empty-ticker caucion request.
Existing stock and futures discovery remains separate; the broad futures search
is unchanged and its empty-ticker semantics also remain unverified.

If PPI supplies an exact identifier, pass it as `--caucion-ticker` to `discover`
or `assess`, together with `--live` only after authorizing production reads.
There is no default identifier. This option is rejected by `watch` and
`credentials-status`. It neither authenticates by itself nor certifies tenor,
funding or metadata. Do not put credentials in this option.

`request_failures` records static `category`, `stage` and sanitized `code` values.
Book/Current failures include zero-based `candidate_index` and `settlement_index`
to identify the bounded attempted request without echoing a URL, body, token or
arbitrary exception text. Candidate blockers also name Book versus Current and
the settlement. Search failures preserve unrelated stock/futures results;
Current failure preserves the returned Book. Partial discovery can exit 0:
inspect blockers and request_failures, not only the exit status.

There are no HTTP 400 retries or alternate-ticker/settlement guesses. Missing
caucion data never becomes a zero funding rate. No credentialed live API call
was made for this change. Live acceptance, endpoint support and the original
failure remain unverified. Obtain PPI confirmation of the exact identifier and
settlement before a separately authorized bounded check. Do not share raw
responses or credentials when reporting its result.

### Required contract metadata

The official SearchInstrument schema exposes ticker, description, currency,
type and market. It does **not** establish these fields:

- Ordinary-share identity.
- Futures underlying, maturity and multiplier.
- Quote scaling.
- Cash or physical delivery.
- Margin rules.
- Caucion tenor.
- TNA units, day basis and rate-side mapping.

Missing fields are **blocking**. The adapter does not guess them.
Obtain reviewed exchange/broker specifications for the exact contracts and
one-calendar-day ARS caucion. These must include holiday/weekend treatment.
No CLI flag can waive these requirements.
Live assessment deliberately remains partial.

### Books and freshness

Books normalize the best bid/ask and quantity.
The last trade comes separately from Current and has its own timestamp.
Source timestamps and UTC receipt timestamps are separate.
All levels must have finite positive numeric price and quantity.

These conditions block assessment:

- Missing sides.
- Duplicate prices, which make depth ambiguous.
- Crossed or locked books.
- Missing timestamps or timestamps without timezone information.
- Stale (>30 seconds) or future (>2 seconds) source dates.

Even a recent `date` with timezone information does not prove book-level scope,
exchange clock accuracy, trading session or non-delayed entitlement.
Official docs do not establish those meanings.
The discovery parser always marks freshness **unverified**.
It never uses receipt time alone to label data as live.

**Warning:** The last trade is not an executable price or funding rate.

## Carry model and executable-side formulas

Use these formulas only for an explicitly verified horizon and day basis:

`r = (TNA_percent / 100) * days / basis` and **`F = S * (1+r)`**.

Use calendar days from financed cash settlement to future maturity.
API assessment currently computes fractional calendar days for verified immediate
settlement. Delayed settlements block assessment until dated cash-settlement
adjustments are available.
The basis must be explicit 360 or 365.
The actual funding convention must support it. Never assume it silently.
Expired maturity and maturity without timezone information are rejected.
Nonfinite or nonpositive horizons and units are rejected.

**Warning:** A one-day rate applied over a longer maturity is a **hypothetical
constant-rate overnight rollover scenario**.
It is not a maturity-locked funding rate.
Simple annualization here does not claim compounded overnight reinvestment.

### Normalized per-share ARS edges

- Cash carry: `F_bid - S_ask * (1+r_borrow) - all_in_costs`.
- Reverse: `S_bid * (1+r_lend) - F_ask - all_in_costs`.

Funding borrow/lend mapping must come from authoritative caucion conventions.
Never assume that rate quote ask/bid maps to borrowing/lending like a stock book.
An unknown rate leaves the corresponding edge absent.
Unknown costs are not zero.
Costs must include commissions, taxes, clearing, slippage and other
direction-specific costs. Call the model separately if costs differ.
Verified inputs or costs must cover dividends and stock-borrow charges before
you interpret an edge.

Normalize the future quote to ARS/share with verified `price_scale`.
Normalize quantity with verified shares/contract `multiplier`.
Capacity in whole contracts is the minimum of future-side contracts and
spot-side shares divided by multiplier.
Insufficient depth blocks that direction.
Currency, ordinary-share/underlying identity, delivery compatibility and settlement
mismatches block numerical comparison.

### Offline assessment safeguards

`arbitrage.assess` is an offline library interface for reviewed metadata and valid
books. It does not automatically certify metadata.
The CLI does not accept a JSON file of arbitrary fields claimed to be authoritative.

If `now` is omitted, it defaults to **current aware UTC**.
It does not default to either saved receipt timestamp.
Old books and expired contracts cannot regain freshness through an old receipt.
An explicit `now` with timezone information is only for documented historical
replay against that instant. It is never evidence of current freshness or current
contract validity.

Computed rates, fair prices, normalized/financed prices, depth divisions and
intermediate edge calculations must all remain finite.
Overflow blocks numerical edges or returns a sanitized nonzero CLI result.
JSON serialization rejects NaN and Infinity.
It does not emit nonstandard numeric tokens.

These unknown conditions block executability:

- Funding lock.
- Short availability.
- Dividends.
- Margin/variation-margin financing.
- All-in costs.
- Freshness.
- Account feasibility.

**Warning:** Even complete offline inputs do not establish simultaneous fills.
The output **always** says `executable: false`.
A positive modeled edge is not a tradable opportunity.
Primary demo readiness is unchanged.

## Historical local implementation evidence

The following statements report the local inspection at implementation time.
They are not a new credential check or a claim about the current local state.

The ignored local `.env` was inspected by key/status only.
Its existing bytes were preserved.
All four PPI credentials were missing.
Blank named entries were appended.
No credentialed live attempt was made because authentication prerequisites were
unavailable at that time.
No actual GGAL future, one-day caucion, rate or current book is claimed here.

To use the bounded read-only procedure, supply the credentials locally.
Credentials alone do not remove the contract metadata, funding or freshness
blockers described above.

**Note:** Calculation fixtures are synthetic. The small REMARKETS book fixtures
described below are historical simulated observations, not current/live prices.

Run the offline tests with `python -m unittest discover -s tests -v`.
Independent review is still needed.

## Recurring GGAL October/December 2026 watch

`watch` is isolated proxy research. It does not change discovery or `assess`
requirements. It does not import Primary, order or account code. It never certifies
metadata or real-time entitlement from a JSON file. A `verified` field is rejected.
The default polling interval is 5 seconds (application minimum 1 second).
Authenticated sessions persist across cycles; see [refresh findings](REFRESH_PERFORMANCE.md).
Network opt-in is mandatory:
`--demo` and `--live` are mutually exclusive. There is no implicit mode.

### Offline procedure: works without credentials

```powershell
python -m market_making.ppi.cli watch --demo --interval 1 --iterations 8
python -m market_making.ppi.cli watch --demo --interval 0 --iterations 4 --json
```

The synthetic clock is 2026-10-07 12:00 UTC. Synthetic maturities are October 30
and December 30 at 12:00 UTC, **not actual contract specifications**. Cycles repeat
four fixtures for both expiries: no edge, cash carry, reverse, unavailable book.
No credentials/config file is read and no HTTP client is constructed. This is
an offline demonstration, even if the actual calendar is past maturity.
Do not treat synthetic prices, rates, costs or capacities as observations.

### Status and signed difference

Each cycle flushes one record per target expiry, even for failures. Default console
output includes `DEMO` or `LIVE-PROXY`, expiry, status, `NONEXECUTABLE` and:

```text
remarkets book: bid xxxx | ask xxxx, ppi ggal price: bid xxxx | ask xxxx implied yield XX% caucion XX%
```

Implied yield is gross annual nominal cash-carry yield:
`(future.bid * price_scale / spot.ask - 1) * basis / days_to_maturity * 100`.
It always uses cash-carry sides, even when the assessment selects reverse.
It excludes costs and uses fractional calendar days and the configured 360/365
basis. Quote prices display in their original provider units; only the yield
calculation applies `price_scale`. `caucion` is explicitly labeled configured
borrow TNA, not a fetched/live rate. Negative yields and zero rates are retained.
Missing/invalid books, identity errors and invalid/expired horizons produce `n/a`.
Timestamp diagnostics alone do not suppress this price-derived display value;
the assessment's strict/manual freshness rules and status remain unchanged.
JSON stores these values under `quote_summary`, with null for unknown numbers.

Add `--verbose` for the previous full calculations, books and timing diagnostics.
`--json` emits strict NDJSON (one JSON object per
line; no progress text). Both directions, blockers and caveats are retained.
NaN and Infinity are not valid output. Every record states `executable: false`
and `live_freshness_established: false`.

For fractional calendar days to an explicit aware maturity:

- Cash carry theoretical = `spot.ask * (1 + borrow_TNA/100 * days/basis)`.
  Observed = `future.bid * price_scale`.
  Difference = theoretical minus observed. Net edge = **negative difference**
  minus cash-carry all-in costs.
- Reverse theoretical = `spot.bid * (1 + lend_TNA/100 * days/basis)`.
  Observed = `future.ask * price_scale`.
  Difference = theoretical minus observed. Net edge = difference minus reverse
  all-in costs.
- Capacity = floor(min(spot-side shares / shares-per-contract multiplier,
  future-side contracts)). Requested contracts must fit the applicable sides.
- `threshold` is nonnegative ARS/share. An eligible edge must be **strictly greater**
  than threshold after costs, with sufficient depth. Equality is not an alert.
- Among eligible directions, select the highest net edge. Equal edges select
  cash carry deterministically. JSON preserves both calculations.

`SIN ARBITRAJE TEORICO` means **no theoretical arbitrage**: both directions were
evaluable and neither passed threshold. The console shows the better direction's
difference for diagnosis; it does not recommend a trade.
`ARBITRAJE TEORICO DISPONIBLE` means **theoretical arbitrage available** in at least
one eligible modeled direction, not an executable opportunity. A blocked opposite
direction is still disclosed in JSON.
`NO EVALUABLE` means **not evaluable**: missing/invalid inputs, expired horizon,
invalid book, insufficient depth, unknown costs/rates or request failure prevent
a no-edge conclusion, unless another fully eligible modeled direction exists.
Unknown costs are never silently zero. An explicit zero cost is a user assumption.

### Live-input proxy procedure: prerequisites for the next session

#### Simple price/yield checker without a JSON file

```powershell
python -m market_making.ppi.cli watch --live --interval 30
```

This reads the existing `.env` for both providers. The user-assumed defaults are
ARS prices, conversion scale 1, annual basis 365 and maturity at 23:59:59
Buenos Aires time on the last Monday-Friday of the target month: October 30
and December 31, 2026. This calendar proxy does not exclude holidays and is not
the exchange's contract expiry specification. JSON reports the assumptions.
Prices and gross implied yield are displayed independently of a funding rate,
costs or contract multiplier. Status is `PRICE CHECK` for valid quote comparisons;
structural/identity errors and unavailable prices still yield `NO EVALUABLE`.
Timestamp diagnostics remain advisory in this price-only mode, which never
reports freshness, execution eligibility or a theoretical arbitrage status.

Set `CAUCION_TNA` in `src/market_making/ppi/monitor_config.py` to your manual
percentage or override it with `--caucion-tna` (e.g. `--caucion-tna 30` for an
explicit assumed 30% TNA). The default is None and displays `n/a`. This option
only applies to live price-only mode, not demo or a full `--watch-config` run.

On 2026-10-09 a bounded, credentialed PPI read confirmed successful authentication
and the `CAUCIONES` instrument type. `SearchInstrument` filters for `PESOS` and
`CAUCION` with `Type=CAUCIONES` both returned empty lists. These two searches do
not prove that all caucion quotes are unsupported. Official REST documentation
lists the category and generic search/book/current endpoints but does not specify
a universal one-day ARS ticker or funding-side rate convention. No reliable
caucion quote was established, so this checker retains an explicit manual rate
instead of inventing an identifier, tenor or rate value.

#### Full arbitrage assessment with an explicit JSON file

1. Review PPI production spot access and REMARKETS **simulated** futures availability
   separately. Mixed-environment prices do not establish a real arbitrage.
2. Fill the PPI key and secret locally as described above. Client identifiers are
   optional overrides. Set `PRIMARY_USER` and `PRIMARY_PASSWORD` for REMARKETS;
   no account is required. Both providers use the selected `--env-file` (default
   `.env`), with environment variables taking precedence. No PPI credential reuse.
3. Copy `config/ppi_ggal_watch.example.json` to a local input file. The example is
   intentionally invalid; exact symbols are fixed, but prerequisites still contain
   placeholders/nulls, not guessed maturity instants, rates or costs.
4. Spot retains PPI `ticker=GGAL`, `type=ACCIONES`, `settlement=INMEDIATA`.
    Each future requires `provider=remarkets`, `market_id=ROFX`, and the exact mapping
    `2026-10 -> GGAL/OCT26`, `2026-12 -> GGAL/DIC26`. Swapped or unrelated symbols
    are rejected, even if their user-supplied underlying says GGAL. Supply
   GGAL underlying and ARS currency for **2026-10 and 2026-12**. Legacy PPI futures
   ticker/type/settlement configs are rejected; there is no fallback. Do not guess
   symbols or contract specifications. Existing PPI discovery is not REMARKETS
   discovery; any separate discovery needs separate authorization.
5. Supply each future's exact timezone-aware maturity (including year), positive
   quote-to-ARS/share `price_scale`, shares/contract `multiplier`, and specification
    provenance. The saved simulated instrument details report multiplier **100**,
    `priceConvertionFactor=1`, and maturity dates **2026-10-29 / 2026-12-29**.
    These calendar dates do **not** establish an exact time or timezone. Review
    authoritative contract semantics; do not manufacture a midnight/UTC maturity.
    User maturity instants, rates and costs are not replaced by these observations.
    Futures quantity must mean contracts. Spot units are ARS/share and
   shares (scale/multiplier 1). No instrument discovery runs inside watch.
6. Supply explicit nonnegative borrow/lend TNA percentages, basis 360/365 and rate
   provenance including observation time, tenor and side conventions. These are
   **static user inputs**; watch does not observe caucion/rates or refresh them.
   Constant-rate overnight rollover is not locked maturity funding.
7. Supply separate nonnegative all-in costs in ARS/share for each direction and
   their provenance. Include commissions, taxes, clearing, slippage, dividends,
   stock borrow and margin funding assumptions. No generic free-cost default exists.
8. Set positive integer `contracts`, nonnegative `threshold`, and
   `max_age_seconds` in (0, 30]. Config keys are strict; duplicate keys, nonfinite
   numbers, absent/placeholder fields, other/duplicate expiry targets, ambiguous
   dates and `verified` fields are rejected before authentication. Provenance
   records assumptions only; its presence is not independent verification.
9. Start with a short explicit run, after separate network authorization:

```powershell
python -m market_making.ppi.cli watch --live --manual-check --watch-config config/ppi_ggal_watch.local.json --interval 30 --iterations 2
python -m market_making.ppi.cli watch --live --manual-check --watch-config config/ppi_ggal_watch.local.json --interval 30 --iterations 2 --json
```

Do not include secrets in the input JSON. It is not a credentials file. Review its
contents before sharing. The app does not save raw responses, keys or tokens.

### Bounded polling and limits

Every live cycle creates and closes **two fresh bounded clients**: PPI gets one
login and one spot Book GET (two-request limit, 120-second deadline); REMARKETS
gets one login and exactly two futures snapshot GETs (three-request limit,
60-second deadline). Its fixed host is `https://api.remarkets.primary.com.ar`,
auth is POST `/auth/getToken`, and data is GET `/rest/marketdata/get` with
`entries=BI,OF`, bounded depth 5. TLS verification is on, redirects, environment
proxies/netrc and retries are off. Both credentials are checked before network.
The decompressed response ceiling is 2 MB; request inactivity timeouts are at most
5/10 seconds, capped by remaining budget. Both transports also shut down active
sockets at the elapsed deadline, including during headers or compressed-body reads.
Each recurring cycle starts a new bounded budget (at most four PPI and five
Primary requests, including renewal/recovery) and elapsed deadline using the
adapters' explicit watch-cycle methods. HTTP sessions, pools and valid tokens
remain open. No full discovery, Current, rate, account or order endpoint is
polled. Spot and futures provider reads overlap, with each client used
sequentially. Batch completion rechecks all books' ages. Acquisition time is
subtracted from the selected interval; slow cycles never overlap. Live
intervals below 1 second are rejected. The cycle count is positive, or
unlimited if omitted. Broker/account rate allowances remain unverified.

All clients close on normal completion, errors and Ctrl+C (exit code 130).
PPI renews before its returned expiry, using `Account/RefreshToken` when the
server supplied a refresh token, otherwise login. Primary renews one minute
before its documented 24-hour lifetime. A quote HTTP 401 permits at most one
renewal and one retry per provider per cycle. A second 401, 403 or invalid
authentication stops with a sanitized alert (exit code 2). Selected
transport/HTTP 429/5xx errors produce `NO EVALUABLE` and trigger exponential
backoff from 30 to 300 seconds. A recoverable futures
snapshot failure is isolated to that leg: current-cycle spot and the other
future remain visible and the unaffected pair is evaluated. Login or spot
transport failures make both pairs unavailable. Unknown
failures stop with static codes, never arbitrary exception/server text. A bounded
run containing only transient unavailable cycles can exit 0: inspect statuses,
not just process exit code. Ctrl+C during sleep also stops.

Outputs explicitly identify **PPI production spot + REMARKETS simulated futures**.
No documented book-wide REMARKETS timestamp is assumed: numeric epochs and
`LA.date` are not promoted to BI/OF freshness. Strict mode therefore blocks these
unknown timestamps; manual-check only applies the existing advisory relaxations.
Both modes keep `executable=false` and `live_freshness_established=false`.

### Request identity and full book display

Real saved REST snapshots omit `instrumentId`. `Client.snapshot` therefore returns
the unchanged decoded response with a separate trusted request-identity context.
Normalization accepts absent response identity only with that matching context:
`identity_basis=request-bound`. A matching response identity is
`response-confirmed`; present malformed or mismatched identity always blocks, even
with request context. Direct parsing without identity or trusted context blocks.
No `instrumentId` is inserted into the raw response. Request binding is weaker
than server confirmation and never proves freshness or contract semantics.

All returned BI/OF levels are retained and sorted best first (bids descending,
offers ascending). Each side must contain 1–5 finite positive prices and positive
integer contract quantities, without duplicate prices or crossed/locked best
prices. A request for depth 5 can legitimately return fewer levels.
Verbose console output includes a `BOOK` line with `pricexquantity` levels, counts,
symbol, market, simulated environment, depth, identity basis and receipt/source
timestamps. NDJSON includes `books.spot` and `books.future`, with complete levels
and quantities, counts, requested depth, provider/environment and diagnostics.
PPI does not request explicit depth, so its `requested_depth` is null.
`source_timestamp_verified` is always false. REMARKETS source timestamp remains
null; receipt is local acquisition time, not a freshness certificate. Valid books
remain visible when strict evaluation is `NO EVALUABLE` for missing freshness.
Missing sides, malformed books and identity errors block the affected expiry
explicitly, without suppressing a valid other expiry. Unavailable spot blocks both
pairs; normalized valid levels remain visible. Unrecoverable authentication stops the run;
recoverable futures transport failures retain the unaffected current-cycle books
with sanitized leg-local diagnostics. No previous book is reused.
Empty books do not prove market closure. Calculations and capacity still use **best levels only**, with no
VWAP, cumulative-depth fill, calendar spread or trading path.

Small offline fixtures in `tests/fixtures/remarkets_ggal_{oct26,dic26}.json` retain
the actual identity-free snapshots extracted from the saved 2026-10-08 data files,
without catalogs or credentials. Historical simulated levels:

- OCT bids: 6125x1, 6120x15, 6112x2, 6000x1; offers: 6130x30, 6176x1, 6207x5.
- DIC bids: 6339x6, 6204x6; offers: 6405x6, 6486x2, 6498x1.

These are test evidence of response shape, not current market observations.

Recurring checks no longer re-authenticate every cycle. Provider throttling or
transport failure slows polling automatically; a futures HTTP 429 suppresses
remaining futures reads in that cycle. Faster polling does not establish
source freshness or resolve unavailable books. Use a finite run when assessing
the behavior of a shorter interval.
This implementation has not made live requests to validate entitlement, schemas,
specific contracts, timestamp meanings or login-rate policy. No real GGAL
October/December edge is established by implementation tests.

By default, invalid depth, missing sides, duplicate/crossed/locked prices, nonfinite values,
missing/naive timestamps, age above configured limit, or future timestamp above
two seconds block proxy signals. A valid timestamp permits **numerical proxy**
evaluation only. The parser's unverified source scope/clock/session caveat is
retained; it is not converted into a verified freshness claim. Outside trading
hours, delayed entitlement, stale rate assumptions, metadata mismatch, unavailable
shorts, dividends, margin and unsynchronized fills can invalidate a positive
model result.

### Explicit manual price checks

Add `--manual-check` to **watch only** when inspecting prices yourself. The CLI
`assess` command is discovery/scenario research, not a book-edge evaluator, and
does not accept this flag. Example with your own reviewed local configuration:

```powershell
ppi-readonly watch --live --manual-check --watch-config config/ppi_ggal_watch.local.json --iterations 2
```

This opt-in moves only `source_timestamp_missing_or_ambiguous`,
`source_timestamp_stale_or_future`, `source_timestamp_scope_clock_and_session_unverified`
and `invalid_last_trade` to visible caveats. Batch-completion timestamp checks
still run but become advisory. Books and their parser diagnostics are not changed;
receipt time never substitutes for source time. Zero traded volume is not quoted
depth and does not block either mode; positive quoted quantities remain required.

**Seconds of skew never veto the manual comparison.** Offline checks cover
0/1/5/29/31/120-second skew with identical price edges and eligibility. Both
spot and futures verbose `BOOK` lines show source and receipt timestamps, source/receipt
ages in seconds and leg-specific timestamp warnings. JSON stores these as
`source_age_seconds`, `receipt_age_seconds` and `timestamp_warnings` under each
book. `timing.source_skew_seconds` is the absolute difference of aware source
times; it is null if either is unknown/naive. `timing.receipt_skew_seconds` is
local acquisition skew only, not exchange synchronization. Ages can be negative
for future timestamps. Missing, naive, stale and future source times are all
advisory in manual mode. `timing.policy=advisory_manual` makes this explicit;
omitting the flag preserves the existing strict source-age gates.

Console records carry `MANUAL-CHECK`; verbose output adds `Fteo`, `Fobs` and `Fteo-Fobs`. NDJSON records
include `manual_check: true` and per-direction theoretical/observed/difference
values, including on unsuccessful cycles (unavailable values are null).
`executable` and `live_freshness_established` remain false. The freshness caveat
means these are potentially stale price comparisons, not actionable arbitrage.
Quote validation, depth, metadata/settlement, scaling/multiplier, rates/costs,
aware clocks, unexpired maturity, authentication and token expiry are not relaxed.
The example watch JSON remains deliberately invalid; its exact target symbols do
not supply maturity instants, costs, funding or verified contract semantics.
HTTP 400 is not repaired by guessing inputs.

Offline callers of `arbitrage.assess(..., manual_check=True)` can request the same
narrow diagnostic partition; its default remains strict and its other metadata,
funding and feasibility blockers remain present.

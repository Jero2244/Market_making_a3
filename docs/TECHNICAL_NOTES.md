# Technical notes & validation history

Detailed operational notes and historical observations for the RFX 20 project.
For setup and a concise overview, see the [README](../README.md).
All command examples and `data/` paths below are relative to the repository root.
Historical results are not current trading or capture approval.

## Shared read-only WebSocket transport (2026-10-05)

`websocket_session.run_session` uses the existing synchronous websocket-client;
connector, monotonic clock, sleep, authentication/catalog dependencies, UTC clock,
and consumer factory are injectable. `stream_market_data.run` retains its defaults
and explicitly forwards its monkeypatch points, including the exclusive/fsync
Recorder. Existing JSONL event order, raw frame text, and validation FaultConnector
evidence remain compatible. Consumer failures are terminal, not retried as network
errors. Invalid UTF-8 is withheld just like malformed/duplicate JSON. The shared
retention predicate filters plain/escaped/nested credential echoes, sharing the
mutable registry across renewal so old and new tokens remain protected.

The viewer opts in with `--transport websocket --live`, with finite positive
`--duration` (default 30 seconds), `--stale-after` (20 seconds), and depth 1..5.
It uses exact ROFX RFX20/OCT26 catalog validation, never `/rest/marketdata/get`.
Authentication and catalog remain HTTP; existing REST detail, diagnostics and
execution snapshot calls are unchanged exceptions to the transport migration.
Only smd application messages are emitted; ping/pong are transport controls.
Fixed demo host, redirect refusal, terminal 401/403/subscription rejection,
bounded backoff, proactive token renewal (separate from reconnect budget),
resubscription and finally-close behavior are shared with the recorder.

`websocket_observation.Observation` displays the current retained selected frame
only, capped to requested display depth. Every selected frame replaces the prior
observation; omitted entries never inherit earlier values. Generation resets and
disconnects clear all current values. Empty/null values retain their raw distinction
in output; omitted, partial, stale receipt, disconnected and generation-reset
states are explicit. Eligibility means a security-retained exact-instrument frame
contains at least one requested entry, including an explicitly empty entry. It
does not mean valid executable levels or market liquidity. No such frame by
deadline returns 2; failures return 2 and interrupts 130. Exchange freshness and
snapshot/delta semantics remain unverified. No Book.gate, fetch_book, or execution
preflight consumes WebSocket observations; integrated execution is deferred.

Offline baseline in this temporary worktree: 123 tests, one resource-root failure
because an older editable install pointed elsewhere. Tests run with this worktree's
`src` on PYTHONPATH resolve that environment mismatch without changing safeguards.
Implementation test run: 140 tests reported OK with `$env:PYTHONPATH = "$PWD\src"`
and `python -m unittest discover -s tests -v` (fake/mock network only). The suite
loads all eight installed entry-point mappings and runs all eight module `--help`
commands from outside the repository. Native console launchers are unavailable
in this environment. This is not independent review or live acceptance evidence.

## Exact October REST book and isolated demo smoke

REMARKETS is a **24/7 test environment**: you can submit order requests outside
production trading hours. Testing may work better during trading hours.
Availability does not guarantee acceptance, execution, liquidity, fresh quotes
or uninterrupted service. The demo smoke no longer uses production hours,
weekdays or a date-specific session attestation as a send gate. Independent
safety review and all other risk gates remain mandatory.

**Freshness remains BLOCKED**: no authoritative whole-book timestamp mapping
(units, timezone, scope) was established. Supplied timestamps and diagnostic
timestamp-derived ages are not exchange quote ages. Receipt/HTTP Date/heartbeat
cannot substitute. Findings and source citations: [FRESHNESS_FINDINGS.md](FRESHNESS_FINDINGS.md).
`--review-evidence` is the preferred alias for legacy `--session-evidence`.
Strict `validate_live.py` and replay acceptance remain separate and unchanged.

**2026-10-03 offline follow-up:** runtime gates are unchanged; fresh October
capture, authoritative specification, bounded clock evidence and independent
review remain external blockers. [FRESHNESS_SPEC_REQUEST.md](FRESHNESS_SPEC_REQUEST.md)
lists the exact requirements and conditional read-only capture process (not
authorization). Artifacts: `data/freshness_followup_20261003T171340082973Z/`.
Historical test counts/review verdicts below apply only to their labeled runs;
they do not approve current freshness or order sending.

`view-order-book --live` resolves only ROFX `RFX20/OCT26`, validates
the exact 100-point price tick and displays available REST depth with honest
freshness (unknown exchange age is not fresh). No WebSocket book is inferred.

`smoke-demo-order` defaults to **no network/no order**. Separate
read-only preflight and explicitly authorized single-order/cancel commands,
gates, recovery limitations and independent review requirements are in
[DEMO_ORDER_TEST.md](DEMO_ORDER_TEST.md). **No order has been sent by this
implementation.** Existing read-only clients and replay guards are unchanged.

Read-only diagnostics do not need an account or session attestation to discover
the current exact contract/book:

```powershell
smoke-demo-order --preflight --evidence-dir data/demo_diagnostic_NEW
```

All readiness blockers are recorded, rather than stopping discovery at the
first missing gate. The actual review-cycle diagnostic found a valid October
contract but **no exchange snapshot timestamp and no configured account**;
send remains blocked. See [DEMO_ORDER_TEST.md](DEMO_ORDER_TEST.md) for evidence and review fixes.

## Quick start (PowerShell, Python 3.10+)

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\Activate.ps1
check-connection
```

Enter your REMARKETS username and password locally when prompted. The password is
hidden in a terminal. A REMARKETS account is available through
https://remarkets.primary.ventures/. No account number is needed for these market-data calls.

## Credentials via `.env` (recommended)

1. Copy the template once: `copy .env.example .env` (Windows) or `cp .env.example .env`.
2. Edit `.env` and fill in `PRIMARY_USER` and `PRIMARY_PASSWORD`.
3. Run `check-connection` — the command auto-loads `.env`.

Rules:

- `.env` is git-ignored and never committed; `.env.example` is the safe template to commit.
- Real environment variables always win over `.env` values.
- Use `--env-file path\to\custom.env` for another file, or `--no-env` to disable loading.
- For automation, set `PRIMARY_USER` and `PRIMARY_PASSWORD` in the process environment
and run with `--no-prompt --no-env`.

The script authenticates, lists segments, retrieves the catalog, and prints symbols
matching `RFX20` (ignoring punctuation, spaces and case). These are discovery
matches, not a guarantee that each is an outright future: inspect CFI and details.

Copy an exact futures symbol from that output:

```powershell
check-connection --symbol "EXACT_SYMBOL_FROM_OUTPUT" --depth 3 --output data/first_snapshot.json
```

This prints contract specifications, bids, offers, last trade, settlement, volume,
open interest and a spread/midpoint when both book sides are present. The output
file contains the catalog and retrieved data, but no authentication headers. Use
a new filename each run; existing files are not overwritten.

Broader discovery:

```powershell
check-connection --search "RFX"
check-connection --search "" --output data/catalog.json
```

For automation, set `PRIMARY_USER` and `PRIMARY_PASSWORD` in the process environment
and run with `--no-prompt`. Tokens stay in
memory; every run obtains a fresh token (documented lifetime: 24 hours).

## Interpreting results

- Authentication OK confirms login; catalog retrieval confirms authenticated data access.
- Discovery without `--symbol` retrieves reference data only.
- Empty bids/offers can occur outside the session or without resting liquidity.
- Receipt time is not exchange quote time. Last-trade timestamps may be old.
- HTTP 401/403: check credentials/permissions; 429: wait before retrying.
- Network failures have bounded connect/read timeouts; API `status` is also checked.
- Exit code 0 means the requested calls completed; 1 means failure; 130 means interruption.

The endpoint is fixed to REMARKETS. REST discovery remains one-shot.

## Read-only WebSocket recorder (PLAN.md point 2)

Install the updated requirements and first run discovery above to obtain an **exact**
RFX 20 futures symbol. Then run (the recorder reads environment/dotenv credentials;
it does not prompt or accept credentials on the command line):

```powershell
stream-market-data --symbol "EXACT_RFX20_FUTURE" --market-id ROFX --output data/session.jsonl --duration 300
```

The recorder authenticates through REST, checks the exact symbol/market in the
catalog and requires a futures CFI code. Only then does it create a **new** JSONL
file (existing files are never overwritten). It subscribes to `BI,OF,LA,TV` on
`wss://api.remarkets.primary.com.ar/` with `smd` and the token in a WebSocket
header. Redirects are not followed and the connection requires HTTP 101. No order
endpoints or order subscription are used. Market-data (`Md`) text frames are stored
in the `raw` field of a `message` event, with UTC local receipt time and any
supplied server timestamp fields preserved separately. **Security exception:**
market frames containing known credentials/tokens anywhere in decoded JSON
(including escaped strings, nested keys and arrays) are withheld entirely;
error/control/malformed JSON and duplicate-key payloads are also withheld (`control_message`
metadata only), as they may echo credentials or other unknown secrets. Thus
`raw` is exact for retained market frames only; this is not a full-fidelity
control-frame log.
Other event lines include connection, `subscription_sent` (request sent, **not**
accepted), `selected_instrument_observed` (actual selected `Md` received),
freshness, reconnect and end markers. Explicit subscription errors stop with a
nonzero exit. Flush + fsync is performed on every line; a disk error stops
recording immediately rather than triggering a reconnect.
Treat recordings as sensitive and do not share them without reviewing their contents.

`--stale-after` (20 seconds), `--interval` (5 seconds for status and ping),
`--reconnects` (5 total retries), `--refresh-after` (23 hours, ahead of the
documented 24-hour token lifetime), and `--duration` (300 seconds) are tunable.
All timing values must be finite and strictly positive; invalid values are
rejected before authentication or creating output files.
Disconnected sessions (including transient HTTP 503 handshake failures) retry
with capped exponential backoff and resubscribe; HTTP 401/403 and explicit
WebSocket auth rejection stop immediately. A proactive token renewal reconnects and
resubscribes without spending the failure retry budget. No token is written to
the recording or printed. A recording ends nonzero when retries are exhausted
or it cannot safely write. After interruption, use a **different** output path.

`freshness` status distinguishes transport, selected instrument, and each of
BI/OF/LA/TV separately. `missing` means no entry seen since connection;
`empty` means an explicitly empty current observation; `stale` means no recent
observation. Instrument traffic alone does not refresh missing sides, and a
reconnect clears all observations. Receiving a bid does not imply an offer.
Server `date`/`timestamp` fields are not treated as receipt times. The API's
per-entry replace/delta behavior is **not verified**: no book is reconstructed,
and neither a `fresh` entry nor a live socket guarantees a usable two-sided book.
Strict market-hours acceptance and exchange-side semantics require verified-session
evidence. An explicitly opted-in demo experiment can separately observe transport
recovery without claiming market-hours acceptance; deterministic offline coverage:

```powershell
python -m unittest discover -s tests -v
```

## Explicit live validation (read-only demo, no orders)

```powershell
validate-live --live --output-dir data/validation_NEW
```

Without `--live`, no network calls occur. The directory and all JSON/JSONL files
are exclusively created; choose a new directory for each attempt. Credentials are
read locally from environment/`.env`, never accepted as CLI arguments or reported.
REST requests are allowlisted to authentication, segments, catalog, exact detail
and snapshot only; the fixed demo host and disabled redirects cannot be configured
from CLI. Discovery/report payloads containing known secrets or sensitive keys
are withheld, not partially redacted. Treat all local artifacts as sensitive.

Stages report `PASS`, `FAIL`, `BLOCKED`, or `NOT_RUN`. Missing credentials, contract,
session evidence or selected market data cannot yield a mock pass. `snapshot: PASS`
means the real REST call succeeded, **not** that a fresh two-sided book exists.
An exact ROFX RFX20 future is validated against the authenticated catalog and detail
CFI/identity/maturity. Supply `--symbol "EXACT_FUTURE"` or let the validator choose
the lexicographically first confirmed nonexpired future (at most five detail probes).

The official [A3 schedule](https://a3mercados.com.ar/info-de-mercado/datos-de-mercado),
reviewed 2026-10-02, lists RFX20 trading 10:30–17:00 Buenos Aires (UTC−03:00).
This is **not** a holiday calendar or proof that REMARKETS follows that schedule.
To enable streaming, supply `--session-evidence path/to/reviewed_session.json` with:

```json
{
  "date": "YYYY-MM-DD",
  "trading_day": true,
  "remarkets_session_confirmed": true,
  "calendar_source": "https://OFFICIAL_DATE_SPECIFIC_CALENDAR_SOURCE",
  "remarkets_source": "https://DEMO_SESSION_CONFIRMATION_SOURCE"
}
```

Only provide these confirmations after reviewing actual calendar/demo-session
evidence. The validator checks date, weekday and the full run fitting in the
published window; it **does not fetch or independently verify these operator
assertions**. Their provenance is saved and this limitation is in the report.
Missing evidence blocks streaming/recovery, even when the clock is within hours.

For a separately authorized, bounded **exploratory demo** observation without
claiming a verified trading session, add `--exploratory-demo` to `--live`:

```powershell
validate-live --live --exploratory-demo --output-dir data/EXPLORATORY_NEW --duration 300
```

Both opt-ins are required for network activity. Strict mode remains the default.
Exploratory mode always leaves `session: BLOCKED` and aggregate exit 2, even if
stream/recovery observations pass. It bypasses only the session prerequisite;
fixed demo endpoints, allowlists, secret filters, no redirects/orders and exclusive
files remain unchanged. Any HTTP WebSocket upgrade rejection stops validation
without retry (including redirects and maintenance/rate-limit responses). The
installed websocket-client 1.9.2 redirect-exception path is tested through real
`create_connection`/`WebSocket.connect` with only transport/handshake mocked;
received non-101 responses are terminal even when library connect raises.
Transport failures without a rejected HTTP response
retain the three-retry limit. Review current service status first; do not hammer
maintenance. A subsequent credentialed attempt needs a documented corrective or
service-status change, not just a new output path.

The stream defaults to 300 seconds (`--duration` can shorten, never exceed 300;
REST preflight has separate bounded network timeouts). A validation-only socket
wrapper closes the real socket **once**, after the first retained selected `Md`
with BI/OF/LA/TV content. This is an **induced local disconnect**, not a real outage.
The normal recorder must retry, establish a new connection, resubscribe, and record
new selected data, in that order. Pongs, other symbols and pre-fault data cannot
satisfy recovery. `subscription_sent` alone does not mean accepted.
An `ended` event must prove the monotonic duration deadline was reached. Only
`full_300_seconds_completed: true` establishes the full five-minute observation;
a short run or missing end marker cannot establish it. Stream acceptance also
requires at least **one** nonempty requested BI/OF/LA/TV entry in selected Md.
Entry completeness is assessed separately: null LA is an explicit limitation,
not a transport failure. TV zero is an explicit numeric observation, not positive
trade volume. Recovery stage acceptance requires duration completion, no terminal
recorder error/rejection, and the ordered recovery sequence. `observations` separates
ordered transport recovery, duration completion and entry completeness; the
`session` stage independently records strict-session verification.
The validator, fault wrapper and recorder share a registry of **all** locally
issued tokens, including the initial REST-preflight token. The same frame
validity/secret checks are used for recording, fault triggering and summary
evidence. Authentication/subscription rejection is recorded as sanitized terminal
metadata only, never as market data, and prevents recovery from reporting PASS.

`stream_summary.json` gives JSONL line evidence, per-entry nonempty/empty/missing
observations and supplied server timestamps. Diagnostic ages are calculated only for
timezone-aware ISO or epoch timestamps; ambiguous timestamps remain unknown.
Last-trade age is not book age; receipt time is not exchange freshness. BI/OF delta
semantics and production liquidity are still unverified. Exit 0 requires all
stages PASS; 2 means incomplete/blocked/failed stages; 1 means input/artifact error.

### Observed bounded credentialed attempt (2026-10-02)

`data/live_validation_20261002_01/report.json` records real authentication,
catalog/detail and REST snapshot evidence at 16:09 UTC / 13:09 Buenos Aires,
selecting `ROFX | RFX20/DIC26` (CFI FXXXSX, maturity 2026-12-29). The snapshot
contained BI and OF, LA null, TV 0; no server quote timestamp was supplied.
Artifacts: `discovery.json`, `contract.json`, `snapshot.json`, `report.json`.
The report has `session`, `stream`, `recovery: BLOCKED`: no date-specific
calendar/demo-session confirmation was supplied. **That attempt did not test
WebSocket recovery. No verified market-hours stream has been demonstrated.** The
later exploratory observations below are separate; offline tests are simulations,
not credentialed evidence.

### Exploratory demo attempt (2026-10-02, one credentialed run)

Public snapshots in `data/session_sources_20261002_03/` confirm Primary's
[24×7 common-scenario testing FAQ](https://apihub.primary.com.ar/#faq12), A3's
RFX20 10:30–17:00 schedule, and the [REMARKETS maintenance
banner](https://remarkets.primary.ventures/). Machine UTC/BA was captured directly
as Friday 16:34 UTC / 13:34 BA, not via locale formatting; clock synchronization
was not independently verified. The national calendar HTML loads its dates via
JavaScript. Its official [2026 JSON dataset](https://www.argentina.gob.ar/sites/default/files/holidays-2026-es.json),
saved in `data/calendar_dataset_20261002_01/`, has no October 2 entry (October 12 is
listed). This is not an A3 exchange-calendar or demo-session confirmation.

`data/live_exploratory_20261002_01/` records the single authorized read-only run
16:34:48–16:39:50 UTC / 13:34:48–13:39:50 BA, selecting ROFX RFX20/DIC26.
Authentication/discovery/contract/snapshot passed. It recorded 18 selected Md
frames and genuine ordered evidence at JSONL lines **4 → 6 → 7 → 8 → 9 → 10**:
data → one induced local close → retry → new connection → resubscription → new data.
Two connections, two subscriptions, one retry, no terminal rejection occurred.
Line 85 proves the duration deadline, **300.048 seconds**; the external 840-second
watchdog did not trigger (overall 303.217 seconds). The homepage banner did not
prevent these particular API observations; it is not proof maintenance ended.

BI and OF were nonempty in all 18 selected frames; TV was explicitly numeric zero;
**LA was null in all 18**, so entry completeness was not met.
The immutable original report is `session: BLOCKED`, `stream: BLOCKED`,
`recovery: FAIL`, exit **2**, despite `stream_summary.json` containing
`recovered: true` and `full_300_seconds_completed: true`. The original validator
erroneously coupled transport recovery to all-entry completeness; its generic
failure reason should not be read as absence of the observed reconnect sequence.
The corrected criterion requires at least one nonempty requested entry and keeps
completeness separate. Original artifacts and statuses are preserved, not rewritten.

`data/live_exploratory_reanalysis_20261002_01/retrospective_analysis.json` is a
**separately labeled offline reanalysis**, not a rerun. It includes original-file
SHA-256 hashes, original statuses and corrected observational assessments. Its
transport stream/recovery assessments are PASS, while strict `session` remains
BLOCKED and aggregate exit equivalent remains 2. No second credentialed attempt
was made. This historical review-pending note is superseded by the offline acceptance
review below; the original retrospective artifact remains unchanged.

Remaining strict-session blockers: date-specific exchange-calendar and demo-session
applicability. Missing LA remains a data-availability limitation, not a transport
blocker. Server frame
timestamps had millisecond-assumed diagnostic receipt differences around
0.473–0.476 seconds in the historical summary, not authoritative quote ages.
Their exact units/semantics,
clock synchronization, per-entry replace/delta rules, production liquidity and
real exchange-outage recovery remain unverified. No orders were sent and no
network adapters/firewalls were changed.

### Offline replay acceptance audit

See [REPLAY_ACCEPTANCE.md](REPLAY_ACCEPTANCE.md) for source decisions, unresolved
update/timestamp rules, clock requirements, prospective capture matrix and fill
assumptions. `config/replay_acceptance_policy.json` defines future thresholds; it is not
permission for a credentialed run. Historical comparisons are explicitly retrospective.

```powershell
replay-readiness --original-dir data/live_exploratory_20261002_01 --output-dir data/replay_acceptance_NEW
python -m unittest discover -s tests -v
```

`data/replay_acceptance_20261002_02/` contains corrected measured quality/readiness
reports; `data/replay_acceptance_20261002_01/` preserves the earlier audit and
independent-review findings. The reviewer confirmed the saved
18 messages, 300.048-second duration, hashes and ordered local recovery; also found
edge cases subsequently corrected with offline tests. The new guard never reconstructs
or enables a book while rules are unknown. Only offline raw-observation exploration
is READY; all book/strategy replay remains BLOCKED. No new credentialed run occurred.

Final reviewer verdict: **PASS for the offline implementation and supported
observations only**, recorded separately in
`data/replay_acceptance_20261002_02/independent_review_final.md`.
**52 offline tests pass.** Strict session, exchange rules, clocks, coverage and
usable-book recovery remain unresolved; this is not replay or trading approval.

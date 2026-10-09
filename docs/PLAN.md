# RFX 20 market-making project

Target environment: https://api.remarkets.primary.com.ar

Reference: [Primary API v1.21](https://apihub.primary.com.ar/assets/docs/Primary-API.pdf).

## Project checkpoint — 2026-10-09

- The main worktree now includes the separate `market_making.ppi` research package
  and `ppi-readonly` command. The full offline suite passes **249 tests**.
- [GGAL Desk](DESKTOP.md) provides a native Windows interface and a locally
  verified executable, with Start/Stop, synthetic demo, prices/yields, settings,
  normalized depth and snapshot export. The Market making tab is a placeholder.
- Recurring GUI/CLI checks reuse authentication and HTTP pools, overlap spot
  and futures provider reads, renew expired tokens with bounded recovery, and
  back off on transient failures. The default interval is 5 seconds and the
  application minimum is 1 second; account rate capacity is not verified.
- RFX20 REST discovery, WebSocket recording/reconnect, offline readiness analysis
  and the guarded single-order harness are implemented. Book reconstruction,
  strategy replay and integrated market making are not ready; existing freshness
  and execution gates remain blocked.
- GGAL research supports bounded PPI production discovery, hypothetical carry
  calculations, and recurring October/December console or NDJSON monitoring.
  The demo is synthetic. The live-input path uses **PPI production spot and
  REMARKETS simulated futures**, not two executable production legs.
- Full returned futures depth is displayed, but modeled edges/capacity use best
  levels only. Strict checks block unknown book timestamps; explicit manual-check
  mode exposes potentially stale price comparisons, never verified freshness.
- No credentialed refresh benchmark or order was made for the desktop/session
  update. The separately documented PPI caucion inspection is preserved in the
  research guide. Offline tests
  do not verify entitlements, contract semantics, timestamp scope or profitability.

### Separate GGAL next steps

Validate live refresh latency/account limits and the providers' streaming
subscriptions before moving the checker from REST polling to event updates.
Review snapshot/delta rules, trade events, reconnects and timestamps. See
[REFRESH_PERFORMANCE.md](REFRESH_PERFORMANCE.md).

1. Obtain reviewed exact aware maturities, units/multipliers and contract semantics
   for `ROFX GGAL/OCT26` and `ROFX GGAL/DIC26`.
2. Establish PPI spot entitlement and timestamp scope, plus explicit funding-side
   conventions, rate observation times and direction-specific all-in costs.
   Default caucion discovery skips undocumented blank-ticker enumeration;
   `--caucion-ticker` requires an exact broker-reviewed identifier, not a guess.
3. Complete a local copy of the intentionally invalid watch template. Only after
   separate authorization, perform a finite bounded read-only check and review
   statuses/blockers, not just the exit code. Do not infer live acceptance from tests.
4. Independently review the research implementation. Keep all outputs
   non-executable; mixed production/demo prices cannot establish real arbitrage.

Procedure and limits: [PPI_GGAL_ARBITRAGE.md](PPI_GGAL_ARBITRAGE.md).
The RFX20 milestones and historical evidence below remain a separate track.

## Current freshness next steps — BLOCKED (2026-10-03)

Offline follow-up only; runtime gates, validate_live and replay guards unchanged.
No authoritative timestamp mapping or source/local clock bound was established;
`timestamp_gate` remains unconditional. Request the written specification and
clock evidence using [FRESHNESS_SPEC_REQUEST.md](FRESHNESS_SPEC_REQUEST.md), then
seek separate authorization/service-status review for exact October read-only
evidence. No capture/order/cancellation was executed. Do not roll OCT26 to another
expiry or change tick 100/quantity 1. Adapter design/implementation is deferred
until prerequisites exist; independent review of exact hashes is still required.
Artifacts: `data/freshness_followup_20261003T171340082973Z/`.
Historical next-action/review counts below are preserved, not current approval.

## 1. Connection and discovery — real read-only REST evidence collected

- Authenticate with `POST /auth/getToken` (PDF p. 9).
- Retrieve segments and instrument catalog (pp. 12–13).
- Discover RFX 20 symbols, then explicitly select a contract and retrieve its
  specifications and REST snapshot (pp. 16–17, 38–41).
- Inspect CFI (futures start with F), expiry, currency, multiplier, price conversion
  factor, price tick (`minPriceIncrement`), quantity increment (`tickSize`) and minimum size.
- Acceptance: successful authenticated run and saved JSON for a confirmed RFX 20
  future; distinguish empty market data from a failed API request.
- Credentialed attempt 2026-10-02: `data/live_validation_20261002_01/` contains
  real authenticated catalog, detail and snapshot for ROFX RFX20/DIC26,
  CFI FXXXSX, maturity 2026-12-29. BI/OF present, LA null, TV zero;
  exchange freshness unknown (no server timestamp in snapshot).

## 2. Streaming data and recording

- Subscribe to BI/OF/LA/TV through `wss://api.remarkets.primary.com.ar/` using the token
  and `smd` message (pp. 39–41). Use WebSockets for continuous quotes, per p. 37.
- Record raw messages with UTC receipt times and server timestamps when provided.
- Implement token renewal, bounded reconnect/backoff, resubscription and stale-data detection.
- Verify whether updates replace each entry or provide deltas before maintaining a book.
- Acceptance: record a session, recover from a disconnect, identify missing/stale sides.
- `validate_live.py` implements explicit opt-in, bounded (max 300s) data-only
  validation and one induced local socket close through a validation-only wrapper.
  It requires ordered selected data → induced close → retry → connection →
  resubscription → new selected data, recording per-stage results and evidence.
- Review-cycle hardening: share all preflight/recorder/renewal tokens in the
  retention filter; use a common frame-validity predicate for recording, fault
  triggering and summarization; terminal authentication/subscription rejection
  cannot satisfy recovery. Full-validator offline regressions cover old-token
  plain/escaped nested echoes and numeric/string 401/403 after resubscription.
- Strict live-session acceptance remains **BLOCKED**, not passed by offline
  simulations or exploratory observations. The first 2026-10-02 attempt had credentials and successful REST calls
  but lacked date-specific holiday/calendar and REMARKETS session confirmation.
  Published A3 RFX20 hours (10:30–17:00 BA) were reviewed; applicability to demo
  and trading-day evidence were not independently verified.

## 3. Measure the opportunity

- Measure spreads in ticks, depth, trade frequency, quote persistence and volatility
  across available RFX 20 expiries; select the initial contract using observed activity.
- Define the proposed arbitrage legs (calendar spread or futures versus a replicating
  basket), executable bid/ask prices, hedge ratios and funding/dividend assumptions.
- Include commissions, exchange fees, slippage, latency and partial-fill/legging risk.
- Acceptance: a reproducible report of net opportunities and their duration/size.
  A wide spread alone does not establish arbitrage, and demo liquidity does not
  establish production profitability.

## 4. Quote-only strategy and replay

- Start with a tick-rounded two-sided quote around a reference price, configurable
  spread/size, inventory skew and position limits.
- Replay recorded events with conservative queue/fill assumptions and costs.
- Track inventory, realized/unrealized P&L, adverse selection and drawdown.
- Acceptance: deterministic replay and valid quotes under empty, crossed and stale books.

## 5. REMARKETS execution

- An isolated October single-order harness is implemented for independent review
  (`DEMO_ORDER_TEST.md`), not an integrated quoting engine. Initial execution is
  offline tests only; credentialed order sending is explicitly deferred.
- `instrument_rules.py` resolves only ROFX RFX20/OCT26, requires exact 100 price
  tick/one-contract metadata. REST book snapshots do not infer WS merge rules.
- Preflight requires independent safety review and supported timestamp semantics, account monitoring
  access, no active orders, current detail and fresh two-sided book; missing
  exchange timestamps block send. Unknown submit retains a global lock and
  requires manual reconciliation, never duplicate submission.
- Review-cycle 1 corrective implementation separates read-only diagnostics
  from send gates. `data/demo_diagnostic_review1_20261002_01/preflight.json`
  confirms ROFX RFX20/OCT26 expiry 2026-10-29/tick 100/quantity 1, but exchange
  timestamp is absent and PRIMARY_ACCOUNT is not configured. Sending remains
  BLOCKED; no timestamp/freshness alternative or new-account report subscription
  is claimed. Empty history explicitly blocks unverified REST monitoring.
- Any correlated PARTIALLY_FILLED/FILLED status is sticky failure despite
  contradictory quantities; quantity anomalies also persist across cleanup and
  restart. Later clean cancellation can prove cleanup, never erase failure.
- Further independent safety review is required. No order was submitted.
- Review-cycle 2 correction retains correlated partial/filled observations
  despite malformed optional orderId/intent metadata; sanitized protocol
  anomalies persist separately from cleanup proof. A durable observation marker
  prevents restart after lost evidence writes from becoming a false smoke PASS.
  Real-client mocked-transport regressions cover these paths. Final independent
  review remains pending; no workaround for external account/freshness blockers
  and no order submission was introduced.

- Configure the test account and reconcile positions, balances and existing orders.
- Subscribe to execution reports before sending orders; implement new/replace/cancel
  lifecycle, partial fills, rejection handling and persistent client/exchange ID mapping.
- Reconcile uncertain submissions before retrying; REST status OK is not order acceptance.
- Add maximum order/inventory/loss limits, rate limiting, stale-data quote withdrawal,
  kill switch and verified shutdown cancellation.
- Acceptance: small test orders traverse their full lifecycle, including restart recovery,
  without duplicate orders or unexplained inventory.

## 6. Integrated demo bot

- Run bounded market-making sessions, reconcile fills/P&L and evaluate execution metrics.
- Stress disconnects, delayed acknowledgements, partial fills and contract rollover.
- Acceptance: repeatable demo sessions with reconciled positions and documented results.

## Historical immediate next action (superseded by current freshness steps above)

Independent review cycle 2 of corrected implementation and artifacts is next.
`data/live_exploratory_20261002_01/` contains real 300.048-second deadline evidence,
18 selected Md frames and the ordered local-close/retry/new-connection/resubscribe/
new-data sequence (lines 4, 6, 7, 8, 9, 10). Strict session remains BLOCKED and
exit is 2. All 18 LA observations were null; BI/OF were nonempty and TV was zero.
The immutable original report has stream BLOCKED/recovery FAIL due to an erroneous
all-entry prerequisite; summary `recovered: true` records the ordered observation.
Corrected acceptance separates at-least-one-entry transport observations, ordered
recovery, duration completion, entry completeness and strict-session verification.
All non-101 upgrade responses (including redirects) stop validation without retry.
Cycle-3 correction inspects the received handshake response when websocket-client
1.9.2 raises a generic redirect-limit exception. Real library connection-path tests
mock only underlying transport/handshake and verify one fixed-host handshake for
each rejected status; genuine transport failures retain bounded retries.
Full-validator offline regressions exercise partial entries, invalid evidence and
301/302/307/308/401/403/429/503 rejection paths.
`data/live_exploratory_reanalysis_20261002_01/retrospective_analysis.json` separately
reassesses the original transport observations with source hashes and original
statuses. It is offline only, never strict-session PASS; originals are untouched.
No second credentialed attempt was made.

Source snapshots: `data/session_sources_20261002_03/` records the 24x7 demo FAQ,
A3 schedule and maintenance banner; `data/calendar_dataset_20261002_01/` records
national holidays (no October 2 entry), not an exchange/calendar demo-session
confirmation. The read-only exploratory opt-in does not relax strict mode or
claim market-hours PASS. Future attempt only after documented corrective/service
change; resolve strict date-specific exchange/demo applicability. Null LA remains
an explicit availability limitation, not failure of the observed transport recovery.
Do not claim an induced local close tests a real exchange outage, or receipt
freshness establishes exchange freshness; book update semantics remain unknown.

## Offline replay acceptance follow-up

The independent reviewer confirmed the preserved observational facts, but rejected
edge cases in the generalized acceptance implementation. All induced faults are now
counted, recovery is attempt-scoped, completion needs consistent session evidence,
and retrospective process exit status is distinguished from an inferred equivalent.
Regression tests cover the corrections without changing original evidence.

`REPLAY_ACCEPTANCE.md` records session/rule/timestamp/clock blockers.
`config/replay_acceptance_policy.json` defines prospective thresholds and a 12-slot capture
matrix; no run is authorized by that file. `replay_readiness.py` creates exclusive
offline quality/readiness reports without reconstructing a book. Historical
threshold comparisons are retrospective, all replay intervals excluded, LA null
and TV zero explicit. New-data receipt delay is separate from unknown usable-view
recovery time. Replay is BLOCKED; only raw offline exploration is READY.

Final reviewer decisions and measured results belong in the separate new
`data/replay_acceptance_20261002_02/` evidence directory (the preceding `_01` audit
and review findings are preserved). Future credentialed captures
still need explicit approval plus a documented corrective/service-status change,
reviewed calendar/demo applicability, confirmed contracts and measured clock uncertainty.

# Single-order demo smoke (submission NOT_RUN/BLOCKED)

## Separate read-only WebSocket milestone

[Order-report observation](WEBSOCKET_ORDER_FLOW.md) is available through
`python -m market_making.execution.watch_order_reports`, default NOT_RUN.
Explicit DEMO-only `--live` sends only account-scoped `os` subscriptions, never
orders/cancellations. Fill/gap evidence is non-authoritative and cannot satisfy
`monitor_ready`, REST lifecycle status, smoke success, reconciliation, preflight,
submission gates or lock release. No historical evidence/journal/lock is changed.
No credentialed validation was performed; freshness and independent-review
blockers below remain intact.

## Current policy: 24/7 demo, authoritative freshness still blocked

REMARKETS is a 24/7 test environment: you can submit order requests outside
production trading hours. Testing may work better during trading hours.
Availability does not guarantee acceptance, execution, liquidity, fresh quotes
or uninterrupted service. No date, weekday, holiday or 10:30-17:00 production
window gates this demo harness. The exact October instrument still must be valid
and unexpired. Strict live-session/replay validation is unchanged.

Independent safety review is separate (`--review-evidence`, legacy alias
`--session-evidence`; `independently_reviewed=true` is necessary, never sufficient).
No boolean `server_snapshot_timestamp_verified` can unlock sending: no supported
authoritative timestamp mapping exists. ISO-aware timestamps are parsed only for
diagnostic ages; numeric units are not guessed. Clock uncertainty is unknown
unless established by a future reviewed adapter. See [FRESHNESS_FINDINGS.md](FRESHNESS_FINDINGS.md).

The same snapshot is checked after durable submit intent with elapsed monotonic
duration, wall-clock consistency and conservative clock uncertainty included in
the five-second maximum. Review evidence is loaded before lifecycle, not read
from disk at the final callback. No intervening IO is added before submit.
Synthetic offline fixtures test the gate mechanics only, not Primary provenance.
No credentialed requests, orders or cancellation were performed in this change.
Historical evidence/backups/hashes: `data/freshness_247_20261002/`.
Current offline follow-up: `data/freshness_followup_20261003T171340082973Z/`;
independent review pending. No runtime source or gates changed. The written
specification request, common-reference clock method and conditional authorized
October read-only capture checklist are in
[FRESHNESS_SPEC_REQUEST.md](FRESHNESS_SPEC_REQUEST.md). No broker capture was run.

**Historical sections below are retained as history and superseded wherever they
describe demo session/calendar restrictions or parseable timestamps as verified
quote ages. Historical artifacts in data/ have not been rewritten.**

## Independent review cycle 1 freshness/session correction

Review rejected the pre-persistence freshness check: a slow durable intent write
could age the snapshot past five seconds or cross the session close before submit.
Correction evidence: `data/demo_acceptance_review1_fix_20261002T2341416907170Z/`.
The lifecycle now requires a separate local final-check callback **after** the
durable submit-intent write and immediately before submission. The CLI callback
revalidates timestamp semantics, the current reviewed session and the exact
snapshot retained from the full recheck; it does not fetch a replacement book.
No broker request or journal write occurs between final gates and submit.

An expired final gate records `phase=aborted_no_send`, `orders_sent=0` and
`BLOCKED_FINAL_SEND_GATE`; no submit/cancel/status request is attempted. Once that
no-send proof is durable, the CLI releases its own lock. If proof persistence
fails, existing conservative lock retention/manual reconciliation still applies.
Unknown submission outcomes must never be reclassified as no-send.

Mocked regression tests inject intent-storage delays: exactly five seconds is
permitted by the unchanged age policy, 5.000001 and six seconds prevent all
submission calls, and crossing 16:59:59 to 17:00 BA prevents submission even with
a one-second-old book. A CLI regression verifies durable zero-send evidence and
safe lock release. Unittest discovery reported 108 tests, exit 0; compile exit 0.
All earlier acceptance, sticky-failure, immediate cancellation and no-retry
controls remain in place. Actual verification is still NOT_RUN/BLOCKED; genuine
snapshot timestamps and current reviewed session evidence remain unavailable.
Updated exact hashes require another independent review before any broker write.

## Acceptance-evidence correction, 2026-10-02

User authorization is restricted here to one demo ROFX:RFX20/OCT26 BUY LIMIT
DAY quantity 1; this implementation stage performs only offline/read-only work.
Before any send, an independent reviewer must approve the exact source hashes.
Evidence: `data/demo_acceptance_initial_20261002T2334574668944Z/`.
Baseline backups/hashes were preserved; lock/journal inventory found neither an
existing lock nor an intent journal. Nothing unresolved was removed.

Baseline unittest discovery reported 101 tests, exit 0; updated discovery
reported 104 tests, exit 0. Broker transport tests are mocked, not live orders.
The credentialed read-only preflight returned BLOCKED with orders_sent=0:
authentication, exact contract, account read access, zero active orders and
correlated historical REST monitoring were observed. Snapshot receipt was
2026-10-02T23:35:11.586964Z (20:35:11 BA, outside the conservative session gate).
The two-sided book had bid/ask 425000/425700, but **no snapshot timestamp**.
Quote freshness, safe price, independently reviewed current demo session and
timestamp semantics remain unverified. Existing public source evidence in
`data/session_sources_20261002_03/source_evidence.json` gives production RFX20
10:30-17:00 hours, generic demo 24x7 scenarios, and maintenance-banner caution;
none supplies current instrument/session or authoritative snapshot freshness.

The former success path could incorrectly classify CANCELLED/0/0 plus absent
active list as permission/acceptance proof. It now also requires a sanitized,
correlated, quantity-consistent, anomaly-free **NEW** report. PENDING_NEW,
HTTP/API OK and CANCELLED alone do not establish acceptance. Cancellation stays
immediate in finally; no waiting for NEW before cancel and no retries were added.
This harness does **not** yet implement authoritative allById lifecycle history
as an alternative: if NEW is missed due to immediate cancellation, cleanup may
be proven but the result remains BLOCKED_ACCEPTANCE_UNPROVEN, with lock retained.
Lost monitoring/storage observations remain sticky BLOCKED_EVIDENCE_GAP even
after NEW and later clean cancellation. Existing fill/protocol/quantity failures
still dominate. A separate bounded historical read-only diagnostic was blocked;
it contains no acceptance claim or raw identifiers.

There is no presently executable send plan: prerequisites are not established.
Only after genuine timestamp/session evidence, fresh zero-active/monitoring
preflight and independent approval of exact hashes may the existing bounded
`--preflight --send` command below be considered. One submission only; cancel
immediately; at most five status polls; preserve lock on any evidence gap/failure.

Implementation offline evidence: `data/implementation_offline_20261002_01/`.
Baseline: 52 tests; final run: 83 tests, exit 0 (4.531 seconds). Compile exit 0.
Default smoke/view both returned NOT_RUN/2. During that initial implementation,
no credentialed preflight or order was executed. Backups and unchanged-source hashes are in
`data/implementation_backup_20261002_01/`; independent review is still pending.

## Review-cycle 1 corrections and actual diagnostic

Correction test evidence: `data/review_cycle1_offline_20261002_01/` — 93 tests,
exit 0, 5.524 seconds; compile exit 0. The original read-only/replay source
hashes still match the initial backups. Further independent review is pending.

The first independent review failed on contradictory PARTIALLY_FILLED/zero
quantity reporting. Correlated PARTIALLY_FILLED **or** FILLED is now sticky fill
failure, independent of cumulative/leaves quantities. State-specific quantity
consistency is checked for the one indivisible contract; anomalies are sticky
protocol failures. Sanitized evidence survives bad numeric quantities and
restart, including earlier journals with retained partial-fill reports. Later
CANCELLED/0/0 may establish cleanup but cannot turn a failed smoke into PASS.
Bounded cleanup continues after nonterminal/contradictory observations.

`--preflight` without `--send` is now a **read-only diagnostic**, separate from
send-readiness. Missing account/session/timestamp attestations do not prevent
authenticated catalog/detail/snapshot observation. It records every gate and
all blockers; no submission or cancellation method is called. Authentication
is the existing fixed-host read-only client's token POST. Missing credentials
also produce a blocked diagnostic without inventing observations.

Real diagnostic: `data/demo_diagnostic_review1_20261002_01/preflight.json`,
receipt 2026-10-02T19:38:52.415988Z, exit 2/BLOCKED, zero orders sent:

- Authentication and exact October rules observed: expiry 2026-10-29, price
  tick 100, permitted quantity 1, bands 362400–486400.
- Available REST book best bid/ask 425800/426000, spread 200 (2 ticks).
- Server snapshot timestamp absent, exchange quote age **unknown**.
- PRIMARY_ACCOUNT absent: account access, active-order reconciliation and
  monitoring unverified; reviewed session/timestamp evidence also absent.

No supported timestamp source or reviewed alternative freshness policy was
established. **Sending remains blocked**; an attestation flag cannot fabricate
a missing timestamp. There is no newly implemented WS monitoring alternative:
an empty historical account produces `EMPTY_HISTORY_MONITORING_UNVERIFIED`,
not a mock pass. Do not create an order to seed history or assert subscription
acceptance without broker evidence. A future new-account subscription/freshness
alternative needs implementation, evidence, tests and another safety review.

## Review-cycle 2 corrections (pending final independent review)

Evidence: `data/review_cycle2_offline_20261002_01/` — 101 tests, exit 0,
5.460 seconds; compile exit 0. Default smoke remained NOT_RUN/2. Original
read-only/replay hashes still match the initial backups. All new broker
transport tests are mocked; no network/order was performed in this correction.

The second review identified optional malformed `orderId` metadata discarding
a correlated fill observation before cleanup received it. Status processing
now first correlates request IDs/account, then returns the sanitized lifecycle
observation even when optional `orderId` is malformed. The ID is withheld as
null and `OPTIONAL_ORDER_ID_INVALID` is a sticky protocol anomaly; partial/filled
status remains sticky fill failure. A later clean cancellation can prove
cleanup separately, but cannot produce smoke success.

The analogous-field audit covers instrument/side/order type/TIF, intent
price/quantity, status type, numerical quantities (including Decimal arithmetic
failures), and optional IDs. Intent anomalies return static codes rather than
discarding correlated fill evidence. Unused `execId`, `avgPx`, `transactTime`
and `text` are not persisted or used for acceptance. Non-JSON, withheld secret
echoes and uncorrelated report schemas yield sanitized sticky protocol errors;
no arbitrary server text is retained.

Before reading any lifecycle report, cleanup durably records
`reconciliation_in_progress=true` (cancellation is still attempted first).
Complete evidence closes this marker only through a durable final update.
If a crash/storage failure loses an observed report, restart sees the open
marker and records `RECOVERY_OBSERVATION_CONTINUITY_UNPROVEN`: cleanup may
subsequently be proven, but the smoke must fail. This is deliberately stricter
than treating absent durable fill evidence as proof that no fill was observed.
An inability to persist the marker prevents report polling, retains the lock
and requires storage repair/manual reconciliation; no order is resubmitted.

New regression tests use the real `DemoClient` with mocked HTTP transport for
malformed optional IDs on PARTIALLY_FILLED/FILLED followed by cancellation,
transient and persistent evidence-write failure, restart recovery, analogous
malformed intent/quantity fields, and withheld optional secret echoes. No new
credentialed run or order was performed for this correction. Account, session,
timestamp/freshness and independent-review blockers remain unchanged.

This is an isolated execution experiment, **not market making** and not replay
acceptance. `check_connection.py` remains read-only; its allowlist is unchanged.
`UnverifiedBookGuard` is unchanged. No production URL, replacement, hedging,
cancel-all or automatic submission retries are implemented.

## Historical send checklist — superseded, not executable authorization

1. Obtain independent safety review of code, offline tests and fresh preflight.
2. Set local `PRIMARY_USER`, `PRIMARY_PASSWORD`, `PRIMARY_ACCOUNT` (never CLI).
3. Verify exact ROFX `RFX20/OCT26` in a **fresh** catalog/detail. `RFX20/OCT`
   resolves only to October 2026: December is never a fallback. Expiry must be
   future and in October; `minPriceIncrement` must be exactly Decimal 100, and
   size metadata must permit one contract. LIMIT/DAY and price bands are checked.
4. Obtain the authoritative specification and valid bounded clock evidence
   described in FRESHNESS_SPEC_REQUEST.md; a reviewed future adapter and exact
   source-hash safety review are prerequisites, not available in current code.
   This existing project's observed REST snapshots have **no server timestamp**;
   fresh HTTP receipt is NOT quote freshness. Those snapshots block sending.
   Do not fabricate timestamp evidence or relax the gate to make a smoke pass.
5. The account must have no active orders. Account history plus a correlated
   historical `/rest/order/id` query must prove monitoring access before submit.
   An account with no historical IDs is blocked until monitoring is verified
   independently; do not create an order just to bypass this gate.

Legacy session evidence example (retained for history; cannot unlock sending):

```json
{
  "date": "YYYY-MM-DD",
  "trading_day": true,
  "remarkets_session_confirmed": true,
  "independently_reviewed": true,
  "server_snapshot_timestamp_verified": true
}
```

The prior interpretation of the last field as whole-book timestamp authority is
withdrawn: no top-level path, epoch units or scope has been specified. The flag
cannot override the unconditional timestamp block. BA production hours no longer
gate this demo harness; strict validate_live session policy is unchanged.

## Historical command reference (PowerShell; send remains BLOCKED)

Only default offline commands were run in this follow-up. The credentialed
commands below are not an approved procedure or authorization; fresh read-only
capture requires separate explicit approval and current service-status review.
Do not run send/cancel commands while freshness prerequisites are missing.

```powershell
python -m unittest discover -s tests -v
view-order-book --live --depth 5
smoke-demo-order
# Read-only diagnostic: choose NEW directory; account/session evidence optional
smoke-demo-order --preflight --evidence-dir data/demo_preflight_NEW
# Supplying real reviewed evidence evaluates additional gates (still read-only)
smoke-demo-order --preflight --evidence-dir data/demo_preflight_REVIEWED --session-evidence data/reviewed_demo_session.json
# ONLY AFTER REVIEW + EXPLICIT AUTHORIZATION; choose another NEW directory
smoke-demo-order --preflight --send --evidence-dir data/demo_order_NEW --session-evidence data/reviewed_demo_session.json
# Restart recovery: SAME evidence directory, never sends
smoke-demo-order --cancel-only --evidence-dir data/demo_order_NEW
```

Default makes no network calls, loads no credentials and exits 2 (NOT_RUN).
The view uses whole independent REST snapshots at available depth 1–5, sorts
finite positive grid-valid levels, and displays best sides, spread/ticks,
local receipt time, server time and quote age/unknown. Missing/locked/crossed
states are not a usable book; invalid/error responses fail closed. No WebSocket
merge is inferred, and this is not a full exchange-depth claim.

## Lifecycle and recovery

The fixed host is `https://api.remarkets.primary.com.ar`. Documented Primary
GET mutation endpoints `/rest/order/newSingleOrder` and `/rest/order/cancelById`
are confined to `demo_execution.py`; `/rest/order/id`, `/rest/order/actives`,
`/rest/order/all` monitor/reconcile. No redirects or HTTP retry adapters are used.
All network waits/polls are bounded; no token refresh/resubmit loop occurs.
Proxy/netrc inheritance is disabled for the smoke CLI.

Preflight precedes a second full gate check. Price is ten 100-point ticks below
best bid, grid-rounded and inside bands; if that fixed price becomes unsafe,
abort rather than reprice. **Even this price may fill if the market moves.**
Only one BUY LIMIT DAY quantity 1 is sent. `cancelPrevious=false`/`iceberg=false`.
An exclusive project `.demo_order_lock.json` prevents another harness send while
the first intent is unresolved. The lock contains evidence path and account
fingerprint, not credentials or plaintext account. Exclusive directories and
fsync/atomic local journal updates persist intent before submit and IDs after
acknowledgement. Account/ID/instrument/side/price/quantity/TIF reports are checked;
HTTP/API OK alone does not mean accepted. Credentials/tokens and raw server text
are never printed/persisted; API secret echoes are withheld. Keep all artifacts
private; local file trust and filesystem durability are not a server-side
idempotency guarantee.

Cancellation occurs immediately in `finally` after acknowledgement, even after
ID persistence failure; status polling follows cancellation, never delays it.
Success requires a correlated valid NEW acceptance observation followed by
correlated terminal CANCELLED, cumQty=0, leavesQty=0
and absence from active account orders. Any observed partial/filled status,
positive cumulative fill, inconsistent quantities, rejection or expiry fails;
unproven acceptance/cleanup or any lost evidence is BLOCKED. No inventory hedge or replacement is sent. A
bounded cleanup may leave an order active: inspect the demo UI immediately.

For a lost submit acknowledgement, the durable intent is **uncertain** and
contains no usable IDs: retain the global lock, reconcile manually in REMARKETS
UI/account history, cancel the identified order there, and independently prove
zero fill/zero leaves/no active order. `--cancel-only` refuses ID-less recovery
and never guesses a matching order or resubmits. A crash between server submit
and durable ID write has the same limitation. Do not delete an uncertain lock
just to retry. A failure with fill/rejection remains a failed smoke even if
later manual cleanup succeeds. No credentialed smoke was executed during
initial implementation. Review-cycle 1 subsequently ran a read-only diagnostic,
not an order test; further review and current live gates remain blockers.

# Read-only DEMO WebSocket order reports

First order-flow milestone: **observation only**, not WebSocket submission,
cancellation, reconciliation, a trading loop, or account-monitoring readiness.
No credentialed validation was performed. Offline tests cannot verify server
entitlements, replay/snapshot completeness, ordering or recovery guarantees.

## Protocol and scope

Repository protocol reference: [Primary API v1.21](https://apihub.primary.com.ar/assets/docs/Primary-API.pdf)
(also linked in README/PLAN); the Primary skill describes account subscriptions
and execution reports. The reference PDF was not fetched during this offline work.
The fixed DEMO endpoint is `wss://api.remarkets.primary.com.ar/` with
`X-Auth-Token` in the upgrade header. The **only application message sent** is:

```json
{"type":"os","account":{"id":"<configured PRIMARY_ACCOUNT>"}}
```

Reports are `{"type":"or","orderReport":{...}}`, correlated only when
`orderReport.accountId.id` exactly equals the configured string account. Missing,
different or unsupported account shapes are withheld and recorded as gaps; no
inference from the subscription alone. There is no all-account `os`, `no`, `co`,
`smd`, REST order query, submit, replace or cancel path in this collector.

## Running (separate authorization required)

```powershell
python -m market_making.execution.watch_order_reports --help
python -m market_making.execution.watch_order_reports
# Only after separate authorization; read-only DEMO, not production:
python -m market_making.execution.watch_order_reports --live --duration 60 --output NEW_REPORTS.jsonl
```

Default/help do not read credentials, dotenv or evidence, create output, or use
network. There is no new console entry point; the existing eight are unchanged.
`--live` reads `PRIMARY_USER`, `PRIMARY_PASSWORD`, `PRIMARY_ACCOUNT` from the
environment only, after bounds and exclusive-new-file reservation. The parent
directory must exist. Existing files are never overwritten. No URL, account,
dotenv, token or mutation CLI override is provided. Treat evidence as sensitive.

HTTP has `trust_env=False` (no ambient proxies/netrc), fixed authentication POST,
no redirects, bounded waits. WebSocket explicitly uses `http_no_proxy=["*"]`,
fixed WSS host, no redirects, and rejects every non-101 upgrade. No socket is
exposed to evidence consumers. Tests inject transports and do not authenticate.

## Observations are not readiness

Events include `connected`, `subscription_sent`, `report_observed`, `disconnected`,
`reconnecting`, `token_renewal`, `payload_withheld`, `failed`, `interrupted`, `ended`.
Sending a subscription is **not** an acceptance acknowledgment. A quiet account,
socket timeout, heartbeat or pong is not a missing order, a working subscription,
or account readiness. Every event declares `orders_sent=0`, `readiness=unverified`,
`continuity=unverified`, and sticky evidence gaps. Initial account history is
unverified even on the first connection. Each successful reconnection increases
generation; disconnects and token-renewal gaps are never cleared by later reports.
Local UTC receive times are not exchange timestamps.

Limits: duration `(0,3600]` seconds, interval `(0,30]`, reconnects `[0,10]`, renewal
`(0,86400]`; connect/auth waits at most five seconds and receives at most interval
or remaining time. Backoff is capped by the deadline. At most 100 connection
attempts (including renewals) and 10,000 received frames are allowed. Cleanup
always closes consumer, HTTP session and opened socket; sink failures are terminal.
These are cooperative synchronous bounds, not hard real-time process deadlines.

Exit 2: NOT_RUN, blocked, failed, or no qualifying report. Exit 130: interruption.
Exit 0: bounded collection completed with a correlated identifiable lifecycle
report and valid supplied quantities, **not** authoritative state or readiness.
Optional metadata anomalies do not erase otherwise qualifying observations.
Known gaps can coexist with exit 0 and remain visible in evidence.

## Evidence interpretation and privacy

Strict JSON rejects invalid UTF-8, duplicate keys at any depth, nonstandard
NaN/Infinity, nonobjects, and over-one-MiB text frames. Quantity observations are
finite/nonnegative and independent of the smoke harness's one-contract constraint.
There is no separate quantity precision or magnitude cutoff: integer/decimal JSON
numbers are parsed exactly as Decimal within the frame-size limit. Consistency
sum comparisons use sparse decimal digits, not context-rounded arithmetic or
exponent-sized buffers. Valid numeric tokens beyond Decimal's representation
range become token-free, typed unsupported-number markers locally, not strings
or a discarded frame. Optional/nested/envelope markers produce fixed anomalies
without erasing supported correlated fills. Unsupported core quantities are
distinguished from ordinary invalid values, never inferred as fills or retained
as quantities; numeric identifiers remain rejected. Report envelopes (`type: or`) are processed before error
heuristics: optional envelope text cannot suppress correlated fills. Genuine
non-report authentication/server-error envelopes still terminate collection.
Only lifecycle enums, sanitized numeric quantities, salted ID digests, fixed
anomaly codes and collection metadata are persisted. No raw frame, plain account,
arbitrary server text, symbol, timestamp, credentials or token is retained. A new
random HMAC salt per run prevents linking digested IDs across recordings; it is
not saved. Numeric fields echoing known secrets/account are withheld.

Duplicate sanitized reports are marked and never summed. Cumulative maxima are
observation lower bounds, not an execution ledger; `lastQty` is never added to
`cumQty`. Reorders, lifecycle changes and identity/execution conflicts are flagged,
not resolved into an authoritative latest state. Positive correlated `cumQty` or
`lastQty`, and supplied PARTIALLY_FILLED/FILLED statuses remain sticky fill
evidence even with malformed optional metadata or invalid identifiers/status.
Status-only fill evidence does not manufacture a quantity. Later NEW/CANCELLED,
malformed reports, reconnects or silence cannot erase a prior fill observation.
Uncorrelated or syntactically invalid frames cannot establish account fills.

This is deliberately separate from `DemoClient.status`, `monitor_ready`, smoke
success/preflight, account reconciliation, submission journals and lock release.
It does not authorize a future send or resolve whole-book timestamp/clock,
market-data delta/replay, independent review or execution-recovery blockers.

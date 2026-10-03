# Primary freshness specification request — BLOCKED (2026-10-03)

This is a request/checklist, not a specification, adapter, attestation or send
authorization. No authoritative mapping or defensible clock bound has been
established. `smoke_demo_order.timestamp_gate` must remain an unconditional block.
See [FRESHNESS_FINDINGS.md](FRESHNESS_FINDINGS.md) for existing evidence limitations.

## Written response requested from Primary

Please provide a versioned, attributable technical specification applicable to
**REMARKETS, market ROFX, exact future RFX20/OCT26**, for REST and/or WS separately:

1. Exact endpoint/message type and JSON timestamp path, data type and allowed
   representations. Identify originating clock (exchange, gateway or another
   component), event stamped, and all caching/forwarding delays. Explain whether
   it certifies current executable book state, not merely request processing,
   last trade, heartbeat or any unrelated record. A gateway timestamp without
   a bounded exchange-to-gateway delay cannot establish exchange quote age.
2. Units, epoch, timezone/offset rules, precision/resolution, rounding/truncation,
   permitted clock error and synchronization guarantees. Define missing, null,
   malformed and future timestamp behavior. Numeric shape is not unit evidence.
3. Coverage of **both BI and OF** and every requested depth level (REST depths
   1–5); atomicity across sides/levels. If separate entry clocks apply, specify
   how the oldest covered state is certified. One-side updates must not silently
   refresh the other side. Is an unchanged side explicitly recertified?
4. Snapshot versus delta/replace semantics; omission versus explicit empty side;
   clears, deletes, level ordering, duplicate updates and partial depth. Define
   sequence scope, gaps, duplicates, out-of-order messages, resets and reconnect:
   when is a new full baseline required and when may a two-sided view be used?
   REST snapshots must not be merged into a WS book without a specified contract.
5. Confirm this applies to demo as well as production, instrument and expiry
   above, API revision and transport. Give service/cache behavior during
   maintenance, disconnect, no liquidity and quiet periods, with example payloads
   tied to specified events. Production hours/generic 24x7 FAQ are insufficient.

Retain the original written response/document, URL or correspondence reference,
issuer, revision, issue/retrieval UTC, relevant pages/sections, environment and
SHA-256. Independently review applicability and sanitized examples. No review
boolean, HTTP Date, receipt time, trade or heartbeat can confer book authority.

## Clock evidence required (no bound currently established)

Compare source and local clocks to a **common independently trusted reference**;
local synchronization alone does not bound source error. Obtain source error
guarantees plus measured local offsets/errors and measurement method, tool/version,
UTC and monotonic pairing, sample times, reference identity and uncertainty.
Account conservatively for source/local errors, measurement error, timestamp
precision/rounding, any specified gateway delay, drift and wall-clock jumps.
Document the resulting finite nonnegative uncertainty, derivation, validity
interval/expiry and refresh/invalidation rules. Do not infer a bound from small
receipt differences, latency, numeric epoch shape or HTTP Date.

Unknown, expired, negative or nonfinite uncertainty must block; so must future or
nonfinite quote ages and rollback/divergent clocks. At the final local check:

`authoritative_age_at_receipt + monotonic_elapsed + uncertainty <= 5 seconds`

This is a prospective evidence/adapter requirement, **not** a claim that current
code models bound expiry. Current real books have no authority or uncertainty,
and the hard gate blocks before any such adapter could be used.

## Conditional October read-only capture — NOT AUTHORIZED / NOT EXECUTED

Only after explicit authorization for a bounded credentialed read-only capture:

- Review current official service status/maintenance notices; preserve source,
  retrieval UTC and change/corrective basis since previous attempt. A new output
  filename alone is not a reason to retry. Do not hammer maintenance/rejections.
- Reconfirm fresh catalog/detail for ROFX:RFX20/OCT26, unexpired October maturity,
  exact price tick 100 and quantity 1 metadata. No December/other-expiry fallback,
  rollover or tick/quantity change. If unavailable/expired, stop and report it.
- Use an exclusive new directory; preserve existing locks/journals untouched.
  Choose a reviewed fixed-demo-host, allowlisted read-only REST/WS procedure with
  bounded duration/timeouts and no order/cancel endpoints. Obtain explicit review
  of the capture procedure before running it; commands elsewhere are not approval.
- Retain sanitized exact market frames where safe, subscription parameters,
  instrument identity, sequence/connection markers, UTC/monotonic receipt pairs,
  capture start/end and actual exits/errors. Preserve timestamp values unaltered;
  redact/withhold secrets and document omissions. Hash evidence and capture code.
- Obtain paired valid clock evidence and spec references. Capture both sides,
  depths and documented event semantics; receiving a frame is not freshness proof.
  Reconnect only under reviewed bounded policy and invalidate prior-side state.
  Strict `validate_live.py` session requirements remain unchanged; exploratory
  transport observations never become strict-session or freshness acceptance.

Historical December WS data and October REST data without timestamps remain
diagnostics only. New capture cannot by itself substitute for a written spec.

## Conditional adapter branch — explicitly deferred

After specification + clock evidence + authorized October evidence, design only
the exact supported mapping; reject unsupported paths/units/environments. Add
offline fixtures for absent/malformed/future timestamps, coverage of both sides,
empty/delta/gap/reconnect behavior, invalid/expired clock bounds and five-second
boundary cases. Do not guess architecture or enable gates in this follow-up.

Preserve lifecycle ordering: full recheck -> durable submit intent -> final local
check of the **same snapshot** -> unchanged submission, with no intervening I/O
or repricing. Prove zero-send aborts durably before releasing only an owned lock;
retain locks on uncertain outcomes/storage failures. No bypass of `validate_live`,
replay guards, account/monitoring/active-order gates or independent source-hash
review. Offline test success is not readiness or authorization to send/cancel.

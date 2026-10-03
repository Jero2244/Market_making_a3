# RFX20 replay acceptance — evidence decisions, 2026-10-02

## Current scope

Only offline raw-message exploration is READY. Book reconstruction and quote-,
time- and trade-dependent strategy replay are BLOCKED. There is no book builder,
execution path, approved queue model, inferred fill, or production access in the
new audit. A test passing does not verify an exchange rule or live safety.

## Session evidence (steps 2–4)

Test date: **2026-10-02** (both preserved credentialed observations).

| Evidence | Saved source / review | Decision |
|---|---|---|
| A3 RFX20 hours | `data/session_sources_20261002_03/a3_hours.html`; URL and collection dates in `source_evidence.json` | Published trading 10:30–17:00 UTC−03; pre/post periods are not continuous trading. Date-specific exceptions unverified. |
| National 2026 holidays | `data/calendar_dataset_20261002_01/` | No October 2 holiday observed; not an exchange calendar. |
| Exchange calendar candidate | Public search found `https://a3mercados.com.ar/docs/calendario-fyo-2026` (listed publication 2026-05-21); direct retrieval during this review returned HTTP 404 | Calendar body not obtained; do not accept search snippets as authoritative date-specific evidence. |
| REMARKETS applicability | Saved Primary FAQ and REMARKETS homepage in `data/session_sources_20261002_03/` | 24x7 common-scenario testing and derivatives availability do not establish RFX20 session applicability. Maintenance banner not proof of outage or resolution. |

Strict-session acceptance remains BLOCKED. Obtain an exchange-issued calendar and
date-specific exceptions, plus Primary confirmation of the applicable REMARKETS
schedule. Retain exact sources, publication/retrieval dates, hashes and reviewer
decisions. Existing operator assertions in `session_gate` are not independent
verification and must not be used as replay authorization.

### Rule evidence register

Primary API reference: `https://apihub.primary.com.ar/assets/docs/Primary-API.pdf`;
the installed Primary skill gives entry names and sample subscriptions, not
authoritative replacement/deletion/ordering semantics. Existing samples and
18 similar frames cannot distinguish snapshot from delta behavior.

The following remain **UNVERIFIED**, with no exchange-rule tests claimed:

- Whole-message versus per-side replacement versus price/level deltas.
- Meaning of absent/null/empty BI and OF; whether zero size deletes a quote.
- Array ordering, sequence numbers, duplicates, and gap detection guarantees.
- Snapshot identification, complete refresh scope/depth and subscription recovery.

Offline tests cover **quarantine policy**, not exchange semantics: no merging of
partial sides, no reuse after disconnect/gap/staleness/terminal error, no promotion
of a two-sided Md or operator `snapshot` flag to a usable market view. The guard
has deliberately no enabling pathway. Implement verified rules with source-linked
tests before any usable-state recovery or book construction is permitted.

### Timestamp/clock register

| Field | Source / observed format | Verified meaning |
|---|---|---|
| `received_at_utc` | Recorder machine `datetime.now(timezone.utc)`; ISO 8601 UTC | Local processing receipt marker; not exchange event time. Wall-clock synchronization and drift historically unknown. |
| Top-level `timestamp` | Server numeric values compatible with Unix milliseconds | Source clock, unit, timezone and event meaning not authoritatively verified. Do not use inferred conversion as exchange latency/freshness. |
| BI/OF quote time | None supplied in preserved frames | Unknown/unavailable. Frame time is not automatically quote time. |
| LA trade time | LA null in every selected frame | Unavailable; no inferred trade time. |
| `elapsed_seconds` | Recorder monotonic clock; seconds | Local duration measurement, not server timestamp or clock synchronization evidence. |

Historical clock uncertainty stays **unknown**, not zero. The new report does not
calculate server-to-receipt latency. Before an approved future capture, record UTC
clock source, sync status, measured offset, sample times and uncertainty (including
measurement delay) before/after the run. Use a monotonic receipt clock for future
gap timing; detect wall-clock steps. If uncertainty cannot be bounded to the
prospective policy limit, time-dependent use remains BLOCKED. No clock settings
were changed in this audit.

## Prospective capture matrix (step 5)

`replay_acceptance_policy.json` defines 12 slots: two **to-be-confirmed** dates ×
two **to-be-discovered** available expiries × early/middle/late five-minute windows.
The window times assume the published schedule and require exception/applicability
review before use. No dates, contracts or coverage are fabricated. The original
single-expiry middle-window run does not complete this prospective matrix.

This policy is **not approval for a run**. Each new credentialed run additionally
needs explicit user approval and a documented corrective or service-status change.
Use only the fixed demo endpoints/read-only allowlists, new output paths, no orders,
no retained credentials/tokens. Unavailable windows/expiries must be recorded as
coverage gaps, not quietly substituted or marked complete.

## Quality and recovery (steps 6–8)

Run offline only:

```powershell
python -m unittest discover -s tests -v
python replay_readiness.py --original-dir data/live_exploratory_20261002_01 --output-dir data/replay_acceptance_NEW
```

The exclusive new directory contains `quality_report.json` and `readiness.json`.
Raw within-frame spreads and supplied quantities are observations, not a persisted
book. Reports include missing/null/empty/zero counts, exact-frame duplicates versus
repeated quotes, gaps and boundaries, receipt-order regressions, timestamp presence,
crossed observations, increment/limit checks against saved contract specs, volume,
stale receipt intervals, exclusions, and source hashes. Exchange-order errors and
historical clock uncertainty remain unknown. Zero quote sizes are quarantined, not
interpreted as deletions. Last-trade null and volume zero are explicit limitations,
not automatic transport failures.

Thresholds are predefined for **future** captures only; historical comparisons are
labeled retrospective. All historical intervals are excluded from replay because
session/rules/clocks are unresolved; additional long receipt-gap exclusions are
reported separately. Time to first new data after the **local** disconnect is a
receipt-wall-clock observation; time to a verified valid market view is unknown.
Reconnect transport recovery is not exchange-outage recovery or book recovery.

Each use has READY/BLOCKED and explicit fill assumptions. Reviewer approval is
recorded separately and cannot remove unresolved acceptance prerequisites. No
strategy executions, fills, queue positions, costs, or P&L are asserted here.

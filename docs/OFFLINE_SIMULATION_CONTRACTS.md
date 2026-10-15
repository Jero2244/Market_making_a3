# Offline remediation contracts (v1)

**BLOCKED for broker use and production.** The Primary skill was consulted for
context only. No credentials, broker calls, endpoint probing, sends, retries or
trading activation are part of this framework. Existing unconditional demo
timestamp/send blockers and demo restrictions remain unchanged.

`offline_contracts`, `offline_risk` and `offline_coordinator` are a separate,
bounded **single-instrument, single-currency, local simulation**. They do not wire
the observation-only Primary order-report parser to a production ledger. Nothing
in these contracts is a verified mapping of Primary field semantics.

## Versioned local contracts

1. **Freshness**: full synthetic book source UTC timestamps are explicitly defined
   by `synthetic_full_book_source_utc_v1`, separately for bid and ask. Integer UTC
   and monotonic nanosecond pairs have uncertainty bounds and calibration expiry.
   Both source ages must be bounded, source timestamps unambiguously nonfuture,
   and wall/monotonic elapsed times consistent. Positive generations start at
   sequence 1 with a baseline; updates are contiguous. Exact latest duplicates
   are idempotent; conflicting duplicates, gaps, rollback and stale opposite-side
   data quarantine the generation. Reconnect discards the book and requires a
   strictly newer generation baseline. The real freshness gate remains blocked.
2. **Execution**: durable intent reservation precedes any simulated dispatch.
   Dispatch has no network transport or callback; it commits one attempt as
   UNKNOWN before returning. Lost ack/restart never permits resend. Reports use
   the existing exact normalized reducer, preserving maximum cumulative fills
   and deduplicating identical events. Unsupported correction/replace reports or
   identity conflicts quarantine the reservation; cancel/fill terminal conflicts
   retain all capacity. No report or cancel ack alone releases capacity. Complete
   sequential identifier-bound terminal history, all previously observed reports,
   deduplicated fills and reconciled inventory are required to resolve it.
3. **Read-only account evidence**: typed metadata, positions, cash balance,
   available margin, working/historical orders and fills share one identity,
   currency, contracts unit, capture and source time. Metadata carries multiplier,
   tick, lot, price bounds and synthetic per-contract margin. Missing kinds,
   incomplete history, stale or inconsistent snapshots, conflicting execution
   IDs and baseline-plus-fill inventory disagreement reject the capture. Loss,
   drawdown and valuation are **supplied fixture values**, not computed broker
   accounting. Snapshot consistency and completeness are fixture declarations.
4. **Coordination/journal**: SQLite WAL/FULL synchronous transactions serialize
   ownership, reservations, final risk rechecks, attempts and audit entries for
   cooperating processes using **one durable local file**. Ownership epochs fence
   stale workers. Every acquire requires reconciliation; takeover additionally
   requires explicit old-dispatch isolation. Unresolved reservations become
   UNKNOWN on takeover, quarantines persist, no expiry frees capacity. Releases
   invalidate reconciliation atomically. One instrument/specification is bound
   to an account journal; other scopes are rejected, not approximated.
   Dispatch also requires a local nonpersisted acquire capability; reopening a
   connection cannot dispatch with an old epoch token. Cooperating connections
   may reserve under a current owner's token, but only the acquiring connection
   can perform controlled simulation dispatch. Explicit disconnect invalidates
   this capability and reconciliation, retaining all uncertain capacity.
   Journal schema v3 adds durable reconciliation anchors: the inventory baseline
   is fixed, source capture time cannot regress, and known execution IDs/fill
   contents and **every observed historical/working account order**, including
   external UNKNOWN orders with no local intent, must remain present in every
   accepted snapshot and resolution. Total quantity, side, identity and active
   price cannot change (replacement/correction is unsupported). Status transitions
   are allowlisted, cumulative fills cannot regress, active capacity cannot shrink
   faster than newly accounted fills, and same-status uncertainty cannot silently
   downgrade. A consistent terminal record plus complete fills/inventory permits
   removal from working capacity, but the terminal history remains immutable.
   Newer timestamps cannot erase these
   obligations. There is no automatic rebasing/history compaction. Broker IDs
   have account-wide SQLite uniqueness across all intents, including resolved
   records. Binding collisions quarantine affected intents and durably block the
   account. Complete resolution history is retained so exact late duplicates
   remain idempotent; any contradictory or unsupported late report restores the
   full intent reservation, invalidates reconciliation and sets a sticky account
   blocker. Acquire/restart/reconcile cannot clear these account blockers. Legacy
   populated journals without anchors require external review; they are not
   silently upgraded into continuity authority. Legacy duplicate broker bindings
   fail journal opening when the unique index cannot be established.
   Migration from populated schema v1/v2 accounts blocks even when there are no
   local intents, because old captures cannot prove complete external-order
   obligations. A previously anchored external identity cannot later be adopted
   by an unbound local dispatch. Local identity binding must precede its account
   observation; ambiguous recovery mapping remains blocked pending external review.
   Missing known orders/fills and unsupported account-order transitions durably
   block the account and invalidate reconciliation. Correcting the submitted
   capture or restarting cannot clear that manual-review blocker. In particular,
   rejected captures introducing new orders cannot be followed by a capture that
   silently ignores those observations to resume dispatch.
5. **Risk/quote proposal**: bounded decimal strings and an isolated precision-128
   arithmetic context enforce exact tick/lot, passive price, quantity, per-order
   notional, gross, worst-case net/inventory, cash, synthetic margin, loss,
   drawdown and attempt-rate limits. All pending/uncertain buys and sells reserve
   conservatively with no netting (sells also consume cash). Existing working
   orders and local reservations may double-reserve; conservatism is intentional.
   A proposal binds account capture, book generation/content, metadata, policy,
   approval and expiry hashes. Reserve and dispatch recheck every gate and epoch
   under the transaction. Disconnect or changed book/capture invalidates it.
6. **Config/approval**: strict mapping schemas, v1, OFFLINE_SIMULATION identities,
   policy hash, scope, expiry and distinct requester/reviewer fixture identities
   are required. Production flags must be false. Hashes provide content binding,
   **not signatures or authenticated independent approval**. No fixture approval
   is an institutional or operational approval.

## Required external evidence and runbook checklist

- [ ] Broker/venue specification and signed review of source timestamp meaning,
  source coverage, clock calibration, baseline/sequence and reconnect semantics.
- [ ] Broker identifier/report mapping including duplicates, complete execution
  history, cancel/fill races, correction/bust/replacement semantics and lost ack.
- [ ] Verified read-only endpoint adapters and units/currency/multiplier,
  valuation, history coverage since a verified inventory baseline, capture cutoff
  and atomic snapshot consistency. No operational endpoint is guessed here.
- [ ] Durable shared **deployment** coordination covering all hosts, tools and
  human activity; enforceable broker/deployment fencing and old-dispatch
  isolation evidence before takeover. SQLite is not that authority. Copying a
  database, using different paths, network filesystems or restoring old backups
  can bypass local authority and is unsupported.
- [ ] Approved multi-instrument/currency valuation, loss/drawdown computation,
  broker margin methodology and independently approved portfolio/rate limits.
- [ ] Authenticated independently signed approval manifests, account authority,
  secure configuration distribution and a separately reviewed production adapter.
- [ ] Independent code/security review and controlled failure/recovery exercises.

On disconnect/restart: freeze simulated dispatch, preserve database/WAL and audit
evidence, isolate the old dispatcher, acquire a new epoch, establish a new book
generation, capture complete consistent account evidence and reconcile. Keep
unknown/quarantined capacity reserved. Resolve only identifier-bound complete
history and matching fills/inventory; otherwise require external manual review.
There is deliberately no quarantine-clearing API, timeout release or retry API.
Do not delete/copy the journal to bypass a blocker. A SQLite disk failure raises
and stops the simulation; no fallback or automatic recovery authority is inferred.

## Checks

`python tests/run_offline.py` forbids network and dotenv reads and prioritizes this
checkout for imports (including subprocess lease/crash tests). New adversarial
fixtures cover both-side freshness, sequence/clock failures, evidence mismatch,
fill deduplication, cancel/fill conflicts, UNKNOWN restart, competing local
reservations, stale epochs, proposal binding and config/approval rejection.
These tests validate local synthetic behavior only, not broker verification or
production readiness. Readiness inventory v2 keeps six remediation contracts'
local validation, external evidence and operational approval separate.

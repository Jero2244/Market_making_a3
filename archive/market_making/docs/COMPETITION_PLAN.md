# Implementation plan: live Remarkets competition bot

2026-10-10. Findings and evidence: [COMPETITION_REVIEW.md](COMPETITION_REVIEW.md).

## Objective

Run a reliable market-making bot in **Remarkets, the live competition venue**, with eligible futures discovered from the catalog, limits obtained from the account, market-session enforcement, and P&L marked using the real-market prices specified by the competition. Proving operation and performance in this environment is the objective. Real-money deployment is outside this plan.

All phases below are planned, not implemented by this review. Documentation updates do not enable orders or remove existing code gates.

## Design decisions

1. Maintain three distinct inputs: Remarkets execution books/fills; mapped real-market references and official valuation marks; account positions/balances/limits. Preserve provider, contract identity, units, source time when available and receipt time for every observation.
2. Keep a single canonical contract registry and a single account/order/position state used by strategy, risk, UI and replay. Build separate adapters around them. Keep the current single-order smoke test separate.
3. Discover all eligible futures, then select a small configured subset for initial operation. Do not attempt to quote every future before validating one complete vertical slice.
4. Never substitute a Remarkets close for a required real-market mark. Never silently substitute an underlying spot price for a same-expiry futures mark. Mark a value unavailable when the correct input is unavailable.
5. Determine opportunity from executable Remarkets prices and relevant real-price references, after competition costs, uncertainty and capacity constraints. Track basis exposure explicitly. A favorable mark alone does not prove realizable profit or guarantee convergence.
6. Keep core correctness separate from statistical performance: first prove no duplicate orders, bounded exposure, correct positions and correct accounting; then evaluate strategy quality.

## Phase 0 — freeze the competition contract and make development repeatable

**Findings:** F01, F16. **Dependency:** none.

Deliverables:

- A versioned competition configuration: eligible futures, competition account identity, start/end dates, allowed sessions, fees, scoring inputs, end-position policy and real-market mark conventions. Record the four user-confirmed rules from the review now.
- A short unresolved-input register. Determine the exact official mark source and cadence, score formula, limit field meanings and applicable costs from competition/account evidence. Do not invent defaults for unknown account capacity or official marks.
- A reproducible Python environment and dependency lock/constraints, fresh-install test workflow and Windows build check. Make the build detect an unusable preexisting environment and provide a controlled rebuild path.
- Update current documentation to distinguish synthetic/offline runs, live competition runs and historical research procedures. Preserve old evidence and its original meaning.

Acceptance: a clean checkout installs and runs the existing offline suite; configuration validation rejects unknown required trading inputs with specific messages. Documentation has one current roadmap. Missing external inputs do not block offline implementation of later components.

## Phase 1 — contracts, sessions, real marks and account state

**Findings:** F02, F03, F08, F09, F15. **Dependency:** Phase 0's schema; external fields can initially use fixtures.

Deliverables:

- General instrument registry: exact venue symbol/market, futures classification, expiry and cutoff timezone, price/size increments, variable ticks if applicable, bands, multiplier, price scale, currency, settlement conventions, sessions and real-market identity mapping.
- Market-session service shared by validation and execution. Use the applicable instrument schedule, holidays, special sessions and expiry cutoff. Distinguish closed-market state from failed or empty data. An available Remarkets service is not a session-open signal.
- Separate real-price adapters for strategy references and official competition marks. Identify an entitled provider and contract mapping; verify actual API fields during implementation rather than guessing endpoints. If official marks arrive only periodically, model their effective time separately from continuously updated references.
- Account adapter for positions, balances, loaded limits and working orders, with last successful reconciliation time and unknown/stale states. Capture limit units and scope; preserve broker-enforced limits as an additional backstop.
- Expire individual GGAL watch targets independently and replace assumed maturities with verified registry data where available. Keep explicit assumption mode for the research UI only.

Acceptance: fixtures for at least two futures with different tick/multiplier rules normalize correctly; expiry-day boundary and holiday tests pass; expired October does not disable valid December; same-underlying but wrong-expiry reference mapping is rejected; missing limits or official marks cannot masquerade as zero exposure or valid valuation. A later read-only account check must match the displayed account state.

## Phase 2 — a valid market view and a measurable recording path

**Findings:** F06, F07, F11, F14. **Dependency:** Phase 1 identities and sessions.

Deliverables:

- Verify actual Remarkets snapshot/update, deletion, empty-side, ordering and reconnect semantics with provider documentation and controlled observations. Save sanitized representative frames and an adapter contract.
- Implement a normalized book with validity states and connection generations. Use a snapshot-only initial path if its guarantees and performance are adequate; do not merge updates whose meaning is unknown. Invalidate on gaps/reconnect and require a verified refresh before reuse.
- Define separate transport, instrument, book and real-reference health. Replace the unconditional timestamp blocker with implemented evidence-based checks. An ISO-looking timestamp or last-trade date is not enough to establish whole-book age.
- If authoritative book timestamps are unavailable, specify an alternative validity policy based on verified feed guarantees, bounded acquisition/receipt age and resynchronization. Document what it cannot establish and validate failure behavior. Keep unsupported latency claims unavailable; do not add a manual “fresh” switch.
- Reject duplicate price levels unless documented semantics provide an unambiguous normalization rule. Apply tick/size rules from the registry.
- Benchmark receive-to-decision lag, storage time and end-to-end request duration. Add bounded asynchronous market-data recording if measurements justify it; record monotonic receipt times, sequence/generation metadata and any lost events. Keep order-intent durability on its required critical path.

Acceptance: malformed, duplicate, missing, one-sided, crossed, delayed, reordered and reconnect sequences have deterministic outcomes. Pongs with stalled instrument data do not preserve quote eligibility. Bounded queues/deadlines are tested under slow disk and network simulations. Valid observations have an implemented path to usable status without falsifying source age.

## Phase 3 — recoverable orders and account-aware risk

**Findings:** F03, F05, F10, F13. **Dependency:** Phase 1 state; Phase 2 required before market-driven sending. Most implementation and failure testing can proceed offline.

Deliverables:

- Fix interrupted-write recovery first: unique temporary files or a transactional journal, validated committed state, orphan handling and explicit unresolved state. Preserve sticky fill/protocol evidence. Prove cancellation and reconciliation remain possible after each persistence failure point.
- Build an order manager for BUY/SELL and configurable quantities. Model pending/new/partial/filled/cancel-pending/cancelled/rejected/expired/unknown states, cumulative fills and fees. Make repeated reports idempotent; handle out-of-order reports and fills racing cancellation.
- Establish a monitoring path for a brand-new account without requiring fabricated order history. Subscribe to supported execution reports before sending and reconcile through supported account/order queries. Verify the real provider contract before choosing identity/idempotency mechanisms.
- Persist order intent and identifiers and reconcile uncertain sends before any retry. If the API cannot correlate an unknown submission automatically, stop new sends for the affected scope and provide a concrete manual reconciliation path.
- Enforce account capacity using positions plus worst-case outstanding and pending fills, including cancel-pending orders. Implement local caps for order size, instrument/portfolio exposure, losses, active orders and action rate, all bounded by the loaded account limit. Do not assume opposite pending orders offset.
- Add an account-scoped single-writer lease, restart reconciliation, prioritized cancellation, bounded authentication recovery/backoff, stale-feed withdrawal, kill switch and market-close handling. Separate stop-quoting, cancel-orders and any permitted inventory-reduction actions.

Acceptance: crash/timeout before and after send, lost acknowledgement, duplicate/out-of-order report, partial fill, cancel/fill race, disk-full/orphan-temp recovery, stale account data and two simultaneous bot instances cannot create unexplained duplicate exposure. New-account startup works through a verified monitoring path. Kill/close/restart leaves reconciled orders and positions or an explicit unresolved state that blocks new risk.

## Phase 4 — competition accounting and a simple strategy

**Findings:** F02, F04. **Dependency:** Phases 1–3 interfaces; strategy can be developed against fixtures before live validation.

Deliverables:

- Event-sourced position and cash/P&L ledger with execution price, multiplier/scale, fees, official real marks and settlement events. Reconcile to account records. Keep realized trading P&L, valuation changes and any cash settlement transfers distinct to avoid double counting.
- For an unsettled linear position, verify the appropriate signed quantity × contract value factor × (real mark − accounting basis) calculation using hand-worked fixtures. Adapt this to actual contract and competition settlement rules; do not assume one formula covers every future.
- Baseline two-sided strategy: tick-rounded reference-centered quotes, configurable size/spread, inventory skew, minimum repricing movement/time, maximum quote age and price collars. Inputs are validated books, relevant real references, positions, capacity and session state.
- A separately configurable price-divergence signal using Remarkets executable bid/ask and the mapped real reference. Bound basis exposure and stale-reference behavior; log why each quote was chosen or suppressed. Do not automatically buy every apparent discount or sell every premium.
- Deterministic desired-versus-working-order reconciliation to avoid cancel/requote storms. Keep quote generation pure enough for offline replay.

Acceptance: known fill/mark/settlement fixtures reconcile exactly within currency rounding rules. Changing only the Remarkets close must not change official marked P&L. Changing the applicable real mark must. Stale references, full account capacity and session closure suppress new risk. Quotes remain on-grid and within bands and exposure limits under inventory shocks.

## Phase 5 — evidence-driven replay and strategy selection

**Findings:** F12, F14, F16. **Dependency:** Phase 2 recording and Phase 4 accounting/strategy.

Deliverables:

- Replace hard-coded readiness results with computed evidence and versioned criteria. Keep historical reports as historical artifacts. Derive coverage and trade availability from each recording instead of fixed narrative strings.
- Capture representative allowed sessions and contracts, synchronized as far as the feeds permit with real references, marks and account/order events. Set useful coverage targets for the selected universe rather than blindly requiring two expiries for every strategy.
- Event replay with fees, delays, rejections, partial fills and conservative queue assumptions. Do not infer fills from a quote merely being touched or disappearing. Where trade evidence is insufficient, report ranges/scenarios instead of fabricated execution accuracy.
- Evaluate spread capture, inventory, adverse selection, drawdown, quote uptime, cancellations, action budget, basis exposure and competition P&L/score. Compare against no-trade and simple quoting baselines. Use held-out sessions to reduce overfitting.
- Determine numeric thresholds from competition rules, account limits and observed data, and freeze them before acceptance runs. Record remaining uncertainty explicitly.

Acceptance: replay is deterministic, accounting reconciles, exclusions are explicit, and all safety invariants hold through fault scenarios. Held-out performance and sensitivity reports exist. Lack of demonstrated edge must be reported rather than hidden by optimistic fills; positive replay results do not substitute for actual Remarkets execution evidence.

## Phase 6 — integrated operation, UI and staged competition validation

**Findings:** F01, F04, F05, F13, F16, F17. **Dependency:** Phases 0–5; headless UI work can start against stable interfaces earlier.

Deliverables:

- Headless runner and operator commands first, then the Market making desktop tab using the same runtime. Display mode, account capacity, positions, working/pending orders, feed ages/validity, real references/official marks, marked P&L, risk stops and reconciliation status.
- Package the implemented runtime explicitly; revise the current build exclusions deliberately. Distinguish synthetic preview, shadow mode and order-enabled Remarkets operation. Stop polling must not be presented as cancellation success.
- Runbook for startup, shutdown, connectivity loss, stale references, uncertain submission, expiry, account discrepancy, machine restart and competition end. Export a sanitized run manifest with configuration/version and relevant evidence.
- Staged validation: offline integration → read-only/shadow allowed-market session → small supervised Remarkets lifecycle tests → one-contract/limited-instrument quoting session → longer sessions and broader selected universe. Perform order-enabled steps only under the user's authorization and verified competition configuration.
- Measure actual acknowledgement/cancel latency, fill reconciliation, rate usage, session behavior and ledger agreement. Exercise disconnect/restart and kill-switch behavior with outstanding exposure under controlled conditions.

Acceptance: the clean-built deliverable completes representative allowed-market sessions; all orders/fills/positions reconcile; official real marks drive P&L; account and local limits hold; restart and stop outcomes are observable and correct. Widen scope only after the prior stage passes. Performance claims must use actual competition-environment results.

## Coverage and execution order

| Finding | Resolution phases |
| --- | --- |
| F01 Competition scope | 0, 6 |
| F02 Real marks and P&L | 1, 4 |
| F03 Account state and risk | 1, 3 |
| F04 Strategy and runtime | 4, 6 |
| F05 Order lifecycle | 3, 6 |
| F06 Freshness success path | 2 |
| F07 Book validity and stale-feed response | 2, 3 |
| F08 Hard-coded contracts | 1 |
| F09 Sessions | 1, 3 |
| F10 Journal crash recovery | 3, first repair |
| F11 Duplicate levels | 2 |
| F12 Readiness and replay | 5 |
| F13 Operational supervision | 3, 6 |
| F14 Latency and storage | 2, 5 |
| F15 GGAL maturity and expiry | 1 |
| F16 Reproducible verification | 0, 5, 6 |
| F17 Operator interface/build | 6 |

Start with Phase 0 and the journal-recovery repair, then build a single-future vertical slice through Phases 1–4. Generalize the registry early, but defer broad quoting until the slice reconciles correctly. Work on fixture-driven risk, order state and accounting while provider details are being resolved. Replay and live-data evidence should validate those implementations, not act as a blanket prohibition on developing them.

This plan deliberately leaves calendar dates and effort estimates unset: current account/API access, official mark availability and competition timing have not been established. The deliverables and acceptance conditions define completion without claiming an unsupported schedule.

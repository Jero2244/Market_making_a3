# Remarkets competition bot: problem review

Reviewed 2026-10-10. Implementation plan: [COMPETITION_PLAN.md](COMPETITION_PLAN.md).

## Target and review boundary

**Remarkets is the live execution environment for this competition.** Success means a working, measured bot in Remarkets. Migration to a real-money venue is a separate future project, not a prerequisite for this one.

The user confirmed these requirements during this review:

- All futures are eligible; eligibility does not require quoting every contract simultaneously.
- The applicable limit is loaded in the account. Its value, units, scope and API representation must be read and reconciled, not guessed.
- Trading hours follow the market. Instrument-specific market sessions and exceptions matter even if the Remarkets service is reachable outside them.
- Remarkets prices generally follow real prices but can diverge.
- Mark-to-market differences use real-market prices, not the Remarkets closing price.

The exact real-market valuation source, valuation times, score formula, account-limit fields, competition dates, costs and end-of-competition position treatment remain unspecified. These are implementation inputs, not reasons to postpone independent engineering work. Real-money liquidity, funding or borrowing requirements should only become competition gates when the competition actually applies them.

This review examined the source, configuration, documentation and existing tests. It did not read `.env`, query either broker, inspect the current account, send orders, or retest the packaged GUI. Findings distinguish missing functionality, reproduced defects, and unmeasured risks. Existing historical reports were not rerun against live services.

## Overall assessment

The repository contains useful discovery, recording, GGAL monitoring and guarded single-order test components. It does **not yet contain an integrated competition market maker**. Passing tests demonstrate the behavior of these components, including deliberate blocking behavior; they do not demonstrate quote generation, account-risk enforcement or competitive performance.

The highest-impact gaps are real-price valuation, account-aware risk, a quoting strategy, and a recoverable order manager. Existing protections worth retaining include fixed Remarkets execution routing, disabled automatic submission retries, durable submission intent, correlated order reports, persistent fill/anomaly evidence, credential sanitization, and separation of read-only clients from execution.

Priorities below mean: **P0** blocks an integrated competition launch; **P1** must be resolved before a reliable competition run; **P2** affects supporting tooling, robustness or maintainability. P0 does not imply that the current read-only tools are causing losses.

## Findings

### F01 — P0: requirements and documentation target the wrong completion criteria

**Confirmed scope mismatch.** README and the previous roadmap organize progress around research, demo qualification and eventual production use. They do not define the competition account, scoring, official marks or all-futures universe. Production spot versus Remarkets futures is repeatedly described as a research limitation; for this competition, a real-market reference can be an intended input, although it still needs exact contract mapping and correct valuation semantics.

Evidence: [README](../README.md), [previous roadmap](PLAN.md), [GGAL research](PPI_GGAL_ARBITRAGE.md).

Consequence: work can become stuck proving properties irrelevant to scoring while essential competition behavior is absent. Keep the history, but make the competition specification and acceptance tests authoritative.

### F02 — P0: no real-market mark-to-market ledger or mapped futures reference feed

**Confirmed missing capability.** The implemented PPI path compares GGAL spot with two Remarkets futures and calculates carry yields. It is not a general real-futures price adapter or an official competition valuation feed. There is no position/P&L ledger that records execution prices, official real-market marks, settlement changes, contract multipliers and currency conversions.

Evidence: [monitor.quote_summary/evaluate](../src/market_making/ppi/monitor.py#L24), [carry assessment](../src/market_making/ppi/arbitrage.py#L70), package inventory under `src/market_making`.

Consequence: the bot cannot measure competition performance correctly or safely size a position taken on a Remarkets/real-market price difference. Real spot, a real futures last trade, a real futures bid/ask, and the competition's settlement mark are distinct values. A favorable valuation difference is not proof that an offsetting fill is available.

### F03 — P0: no account-wide positions, capacity or risk engine

**Confirmed missing capability.** `DemoClient` reads order history, active orders and an individual order. There are no implemented balance, position or account-limit reconciliation flows. One-contract submission and rejection of preexisting active orders are smoke-test restrictions, not ongoing portfolio controls.

Evidence: [DemoClient endpoints](../src/market_making/execution/demo_execution.py#L18), [preflight](../src/market_making/execution/smoke_demo_order.py#L39).

Consequence: the bot cannot enforce the limit loaded in the account or reserve exposure for outstanding/pending orders. Required controls include actual account capacity, worst-case fills, instrument and portfolio exposure, loss limits, stale-state handling, and position changes made outside the bot. Contract size limits in instrument metadata are not a substitute for account limits.

### F04 — P0: no quoting strategy or integrated runtime

**Confirmed missing capability.** There is no event loop joining normalized data, fair value, inventory, desired quotes, risk decisions and actual orders. Inventory skew, quote sizes, spread selection, repricing hysteresis and adverse-selection controls remain roadmap items.

Evidence: [market-making placeholder](../src/market_making/desktop/app.py#L93), previous roadmap sections 4–6, source package inventory.

Consequence: the current application cannot run the intended strategy. A simple deterministic baseline should precede complicated optimization or aggressive trading on price differences.

### F05 — P0: the order harness cannot serve as a competition order manager

**Confirmed design limitation.** It submits one BUY LIMIT DAY, attempts immediate cancellation, and treats a fill as smoke-test failure. It cannot maintain two-sided quotes, manage arbitrary quantities, process normal partial fills as inventory, or reconcile replacements. An account with no history fails `monitor_ready()`. Unknown submission IDs require manual UI reconciliation.

Evidence: [submit/monitor_ready](../src/market_making/execution/demo_execution.py#L109), [cleanup](../src/market_making/execution/demo_execution.py#L213), [unknown submission recovery](../src/market_making/execution/smoke_demo_order.py#L220).

Consequence: a new account cannot pass the existing readiness path, and the first successful trade is a failure under the test's semantics. Retain this harness for its narrow test; build a separate order manager with execution reports, durable identity mapping, reconciliation and supported cancel/replace behavior. Never retry an uncertain order blindly.

### F06 — P0: freshness gates have no implemented success path

**Confirmed deliberate block.** `timestamp_gate()` always raises. Every RFX snapshot returned by `parse_snapshot()` has unknown quote age and clock uncertainty and unverified timestamp authority. The GGAL Remarkets parser also always reports missing/ambiguous source timestamp evidence.

Evidence: [timestamp_gate](../src/market_making/execution/smoke_demo_order.py#L51), [Book.gate and snapshot construction](../src/market_making/market_data/order_book.py#L55), [GGAL snapshot parser](../src/market_making/ppi/remarkets.py#L114).

Consequence: credentials and review flags cannot make the send path operational. This needs a supported data-validity design, not another attestation or a disabled check. Determine the actual feed guarantees. Preserve distinctions between receipt time, transport liveness, book validity and exchange timestamp age. If the API has no authoritative book timestamp, explicitly evaluate and test an alternative based on its actual snapshot/update guarantees; do not invent source freshness.

### F07 — P0: no usable streaming book or automatic quote withdrawal on data loss

**Confirmed missing integration.** Streaming records raw observations and resets freshness on reconnect. Replay's `UnverifiedBookGuard.usable` always returns false. The recorder reconnects on stale transport; pongs can keep transport alive while selected-instrument quotes age. It reports instrument staleness but has no execution action attached to it.

Evidence: [Freshness](../src/market_making/market_data/stream_market_data.py#L58), [transport checks](../src/market_making/market_data/stream_market_data.py#L287), [UnverifiedBookGuard](../src/market_making/validation/replay_readiness.py#L50).

Consequence: the recorder must not be treated as a trading book. Implement verified snapshot/update, deletion, ordering and resynchronization behavior, or a deliberately snapshot-only baseline with measured limits. A healthy socket alone must never preserve quote eligibility.

### F08 — P1: contract handling is hard-coded and incompatible with an all-futures universe

**Confirmed limitation.** Execution/book parsing assumes ROFX RFX20/OCT26, a 100-point price grid, integer size and one contract. Recorder symbol validation accepts only the RFX20 family. The GGAL checker fixes October and December 2026 targets. RFX expiry validation rejects the entire maturity date using the UTC date, without an actual instrument cutoff time.

Evidence: [instrument rules](../src/market_making/market_data/instrument_rules.py#L7), [book parser](../src/market_making/market_data/order_book.py#L74), [validate_symbol](../src/market_making/market_data/stream_market_data.py#L44), [GGAL targets](../src/market_making/ppi/monitor_config.py#L8).

Consequence: another eligible future can be rejected or handled with the wrong units. Introduce a registry containing exact venue identity, expiry/cutoff, tick rules, size rules, multiplier, currency, session and real-price mapping. Do not auto-roll an open position or silently switch contracts.

### F09 — P1: market-session validation is fixed to one product and one workflow

**Confirmed limitation; clarified by the user.** `validate_live.session_gate()` enforces weekdays 10:30–17:00 Buenos Aires and date-specific review evidence. The order harness's separate `session_gate()` only checks a review boolean. Thus the two identically named gates mean different things, and neither is a reusable all-futures competition calendar.

Evidence: [validation session gate](../src/market_making/validation/validate_live.py#L41), [order review gate](../src/market_making/execution/smoke_demo_order.py#L22).

The offline Saturday rejection was reproduced. **That rejection is not itself a bug under the user's market-hours requirement.** The issue is generalizing the applicable market schedules, exceptions and expiry cutoffs, then enforcing them in the actual quote/send path. Remarkets service availability must not imply permission to quote around the clock.

### F10 — P1: an interrupted journal write can block cancellation reconciliation

**Reproduced defect.** `durable()` writes to a fixed `intent.json.tmp` using exclusive creation. A crash before replacement can leave this file behind. Every subsequent write fails with `FileExistsError`. `cleanup()` attempts cancellation, then writes its reconciliation marker before reading status; that write fails on the leftover file.

Evidence: [durable](../src/market_making/execution/demo_execution.py#L196), [cleanup marker](../src/market_making/execution/demo_execution.py#L223).

Offline reproduction produced **one cancel attempt and zero status reads**. Recovery needs collision-resistant temporary files or a transaction journal, validated restart behavior and preserved evidence. Do not solve this by deleting an unresolved lock or declaring cleanup successful without evidence.

### F11 — P2: RFX snapshot parsing accepts ambiguous duplicate price levels

**Reproduced defect, currently behind blocked send gates.** Two bids at price 10000 with quantities 1 and 2, plus ask 10200, produce `state="two_sided"` and retain both duplicate bids. The GGAL parser already rejects duplicate prices.

Evidence: [RFX parsing loop](../src/market_making/market_data/order_book.py#L88), [GGAL duplicate check](../src/market_making/ppi/remarkets.py#L106).

Consequence: future depth/size logic could interpret an ambiguous book as valid. Reject or normalize duplicates only according to verified feed aggregation semantics; share structural validation across adapters without erasing provider differences.

### F12 — P1: replay readiness is a historical report, not a general evaluator

**Confirmed limitation.** `audit()` always emits blocked reconstruction/replay states and fixed statements about one expiry and absent trades, regardless of newly supplied evidence. The policy contains placeholder dates/expiries and fixed RFX capture windows. There is no strategy/fill replay or account/mark ledger simulation.

Evidence: [audit decision](../src/market_making/validation/replay_readiness.py#L288), [policy](../config/replay_acceptance_policy.json).

Consequence: collecting better data alone cannot produce readiness, and historical claims may be misleading when auditing a different recording. Compute outcomes from versioned policy and actual evidence. Separate book-quality evaluation, strategy replay, fill uncertainty and operational acceptance. Legitimate empty or one-sided periods need explicit handling, not blanket failure of an otherwise useful recording.

### F13 — P1: no continuous trading supervision, action budget or complete shutdown protocol

**Confirmed missing integration.** The recorder has a finite reconnect budget and stops on authentication rejection. The GGAL watcher has useful bounded renewal/backoff, but no shared account-wide order budget. The existing desktop Stop control stops read-only polling; it is not an order cancellation or position policy.

Evidence: [recorder recovery](../src/market_making/market_data/stream_market_data.py#L304), [watch session](../src/market_making/ppi/monitor.py#L294), [controller.stop](../src/market_making/desktop/controller.py#L68).

Consequence: no integrated response exists for a stalled feed, expired credentials, throttling, process restart, disconnection or market close while orders remain live. Add bounded recovery, account-scoped instance ownership, prioritized cancellation, a kill switch and verified shutdown. Cancellation failure must remain visible; a stopped process does not prove orders are gone.

### F14 — P2: recording durability is directly on the market-data receive path

**Confirmed architecture; performance impact unmeasured.** Each recorded event flushes and synchronizes the file before more events are consumed. There is no bounded asynchronous data-recording pipeline or queue-lag measurement. Main REST calls use connect/read inactivity timeouts rather than an end-to-end cycle deadline; the separate PPI path already implements stronger deadline handling.

Evidence: [Recorder.write](../src/market_making/market_data/stream_market_data.py#L118), [main REST request](../src/market_making/market_data/check_connection.py#L47), [PPI deadline transport](../src/market_making/ppi/deadline.py).

Consequence: slow storage or slow-progress responses can delay observation and action. This is not a measured latency claim. Benchmark first; separate bulk market-data persistence from critical order-intent durability, with bounded buffering and fail-safe handling of dropped events or excessive lag.

### F15 — P2: GGAL maturity assumptions and expiry handling undermine continuity

**Confirmed assumptions and reproduced startup defect.** Default maturity uses the last weekday at end of day rather than verified contract maturity. README records different dates from saved instrument details. Both CLI and desktop reject the entire watch when any maturity expires. On 2026-11-01, October is expired and December is not, yet startup blocks both.

Evidence: [default maturity](../src/market_making/ppi/monitor_config.py#L16), [CLI expiry check](../src/market_making/ppi/monitor.py#L483), [desktop expiry check](../src/market_making/desktop/controller.py#L89).

Consequence: carry yields use an assumed horizon, and one obsolete target disables a valid one. Resolve exact metadata and expire each target independently. Keep a manually assumed yield clearly separate from real competition valuation. This is supporting-tool work unless GGAL is selected for the first strategy.

### F16 — P1: verification and deployment are not reproducible competition acceptance

**Confirmed local setup problem and coverage gap.** The normal `python` interpreter lacked `requests`, `websocket` and the installed project. The existing build environment could not launch its configured Python 3.12 base interpreter. Using Python 3.14 with project source and the existing build environment's package directory, all **249 tests passed in 20.169 seconds**. This is a useful offline result, not a clean-install or packaged-runtime verification.

The only checked-in GitHub workflow invokes an assistant from comments; there is no automated test/build workflow. Runtime/build dependencies use version ranges. Tests largely exercise the existing data tools and deliberately blocked harness, not a complete competition session.

Evidence: [dependencies](../requirements.txt), [build](../build_desktop.ps1), [project layout tests](../tests/test_project_layout.py), `.github/workflows`.

Consequence: a different machine or fresh competition deployment may fail before connecting. Add a reproducible environment, CI, clean-build checks and end-to-end failure scenarios. Do not count the alternative test interpreter as proof that the existing executable works.

### F17 — P1: the deliverable has no trading operator interface

**Confirmed missing capability.** The Market making tab says “Coming later.” The desktop build explicitly excludes execution, validation and the original market-data package. Existing screens show GGAL prices/yields, not account capacity, working orders, inventory, real marks or competition P&L.

Evidence: [placeholder](../src/market_making/desktop/app.py#L93), [build exclusions](../build_desktop.ps1#L14).

Consequence: even a later backend implementation will not appear in the current executable automatically. Deliver a headless operator interface first, then connect the desktop to that same runtime and verified controls.

## Verification record

- 249 existing offline tests passed using the alternative interpreter/package path described above. No new behavior was implemented or claimed tested.
- Offline probes confirmed the unconditional timestamp block, empty-account monitoring block, duplicate-price acceptance, interrupted-temp-file recovery failure, and all-target expiry block.
- The Saturday session probe confirmed current behavior; after the user's clarification, weekend rejection is treated as expected, not as a defect.
- No broker request, order, credential read, current account-limit validation or current real-price entitlement check was performed.
- The review is a prioritized assessment of the inspected implementation, not proof that every possible defect has been found. The plan maps every finding to deliverables and acceptance criteria.

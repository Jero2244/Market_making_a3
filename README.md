# RFX 20 · Market Making

Python research tools for RFX 20 futures on **Primary REMARKETS**: discover
contracts, record market data, inspect snapshots, and assess replay readiness.

**Python 3.10+** · **Demo environment** · **Research in progress**

[Quick start](#quick-start) · [Commands](#commands) · [Next steps](#next-steps) · [Documentation](#documentation)

---

## Current status

| Area | Status |
| --- | --- |
| REST discovery & snapshots | Implemented; historical read-only evidence collected |
| WebSocket recording & reconnect | Implemented; exploratory transport recovery observed |
| Offline tests | Historical restructure baseline: 123; transport regressions added separately |
| Exchange freshness & strict-session validation | **Blocked** — timestamp, clock, and session evidence unresolved |
| Book reconstruction & strategy replay | **Blocked** — update rules and data quality not yet verified |
| Demo order sending | **Blocked** — freshness, account readiness, and independent review required |

> **Research tools, not a production trading bot.** No orders have been sent by
> this implementation. Receiving data does not establish exchange freshness,
> and offline test results do not authorize trading.

## Quick start

Run these commands from the **project root** in PowerShell.

### 1. Install

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\Activate.ps1
```

The editable installation includes the dependencies in `requirements.txt`.
Keep this installation editable: configuration and the order lock live in the repository.

### 2. Configure credentials

If you do not already have a `.env` file:

```powershell
Copy-Item .env.example .env
```

Fill in `PRIMARY_USER` and `PRIMARY_PASSWORD` locally. You can create a demo account
at [REMARKETS](https://remarkets.primary.ventures/).

- Never commit credentials; `.env` is git-ignored.
- Existing environment variables take precedence over `.env`.
- Market-data discovery does not require an account number.

### 3. Explore

For a first **read-only network request**, discover available contracts:

```powershell
check-connection
```

Copy an exact futures symbol from the output to inspect and save its snapshot:

```powershell
check-connection --symbol "EXACT_SYMBOL_FROM_OUTPUT" --depth 3 --output data/first_snapshot.json
```

Use a **new output path for each run**. Existing evidence files are not overwritten.

<details>
<summary>Command not found or virtual environment activation blocked?</summary>

Use the virtual environment's Python directly; activation and a Scripts entry on
`PATH` are not required:

```powershell
.\.venv\Scripts\python.exe -m market_making.market_data.check_connection --help
.\.venv\Scripts\python.exe -m market_making.market_data.check_connection
```

If installed into your current Python instead, use `python -m` with the same module.
Old root-level `python <script>.py` invocations have been replaced by package commands.

</details>

## Commands

Every command supports `--help`. Run from the project root to preserve `.env`
and relative `data/` paths.

| Command | Purpose |
| --- | --- |
| `check-connection` | Discover contracts and fetch REST snapshots |
| `stream-market-data` | Record read-only WebSocket market data |
| `view-order-book` | Exact `ROFX RFX20/OCT26`; REST default, optional raw WebSocket observations |
| `validate-live` | Run bounded read-only demo validation; requires `--live` for network access |
| `collect-session-sources` | Fetch public reference and service-status pages |
| `reanalyze-demo` | Reassess saved evidence offline without modifying originals |
| `replay-readiness` | Audit saved data against the replay acceptance policy |
| `smoke-demo-order` | Guarded demo order harness; no network or order by default |

Read-only viewer transport (requires separate authorization before any live use):
`view-order-book --live --transport websocket --duration 30 --depth 5`.
Without `--live`, both transports return 2 without network or dotenv loading.
WebSocket mode prints raw single-frame BI/OF/LA/TV observations with explicit
omitted/empty/partial/stale/disconnected/generation-reset states; it never merges
frames into a book. Receipt age is **not exchange quote age**; update semantics
and exchange freshness remain unverified. A selected retained frame containing
at least one requested entry (including an explicitly empty entry) is an eligible
observation, not an executable quote. No eligible observation by deadline returns
2; interrupts return 130. REST is still the default. WebSocket mode does not call
REST market-data snapshots, but uses HTTP authentication and exact catalog checks.
No execution/preflight gates consume these observations. Recorder JSONL output
and eight console entry points remain unchanged.

**Live validation and demo orders are separate procedures.** REMARKETS is a 24/7
test environment, not a guarantee of liquidity or fresh quotes. Strict validation
still requires reviewed session evidence. See the [technical notes](docs/TECHNICAL_NOTES.md)
and [demo safety procedure](docs/DEMO_ORDER_TEST.md) before any credentialed test.

### Run offline tests

```powershell
python -m unittest discover -s tests -v
```

## Project layout

### Read-only order-report milestone

`python -m market_making.execution.watch_order_reports` defaults to NOT_RUN (exit 2).
Explicit `--live --duration 60 --output NEW_REPORTS.jsonl` enables bounded,
account-scoped **DEMO observation only**, after separate authorization. Credentials
come from environment only; no dotenv, orders, cancellations or reconciliation.
Exit 0 means reports collected, never readiness. See
[WebSocket order flow](docs/WEBSOCKET_ORDER_FLOW.md). Smoke gates, journals/locks,
eight console commands and all freshness blockers remain unchanged.

```text
src/market_making/
├── market_data/    # Discovery, recording, instrument rules, book inspection
├── execution/      # Guarded demo order lifecycle
└── validation/     # Live validation and offline evidence analysis
config/            # Replay acceptance policy
docs/              # Procedures, findings, and roadmap
tests/             # Offline regression tests
data/              # Local evidence and historical artifacts (git-ignored)
```

Treat recordings and reports as sensitive. Review them before sharing.
The replay policy lives at `config/replay_acceptance_policy.json`; `--policy`
allows an explicit override.

## Next steps

Work through the prerequisites **before moving toward automated trading**:

1. **Resolve exchange freshness.** Obtain authoritative timestamp units, timezone,
   and scope, plus bounded source/local clock evidence. Follow the
   [freshness specification request](docs/FRESHNESS_SPEC_REQUEST.md).
2. **Collect reviewed read-only evidence.** After separate authorization and a
   service-status review, capture the exact October contract. Resolve date-specific
   exchange/demo-session applicability for strict validation.
3. **Verify book rules and replay quality.** Confirm replacement/delta, deletion,
   ordering, and refresh semantics; meet the [replay acceptance criteria](docs/REPLAY_ACCEPTANCE.md)
   before reconstructing books or simulating fills.
4. **Measure and test a quote-only strategy.** Once replay is accepted, evaluate
   spreads, depth, costs, inventory risk, and conservative fill assumptions offline.
5. **Review execution readiness.** Resolve account monitoring and reconciliation,
   complete independent safety review, and obtain explicit authorization before
   any demo order. Integrated quoting comes later.

These are prerequisites, **not permission to capture data or send orders**.
The full milestones and acceptance criteria are in the [roadmap](docs/PLAN.md).

## Documentation

| Guide | What you will find |
| --- | --- |
| [Roadmap](docs/PLAN.md) | Project milestones and acceptance criteria |
| [Technical notes & history](docs/TECHNICAL_NOTES.md) | Recorder behavior, validation modes, and historical evidence |
| [Freshness findings](docs/FRESHNESS_FINDINGS.md) | Known limitations and source citations |
| [Freshness specification request](docs/FRESHNESS_SPEC_REQUEST.md) | Required timestamp and clock evidence |
| [Replay acceptance](docs/REPLAY_ACCEPTANCE.md) | Data quality gates and replay assumptions |
| [Demo order procedure](docs/DEMO_ORDER_TEST.md) | Safety gates, recovery limitations, and review requirements |
| [WebSocket order flow](docs/WEBSOCKET_ORDER_FLOW.md) | Read-only account reports, bounded cleanup, and continuity limitations |
| [Primary API reference](https://apihub.primary.com.ar/assets/docs/Primary-API.pdf) | Official REST and WebSocket documentation |

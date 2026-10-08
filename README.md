# RFX 20 · Market Making & GGAL Research

Python research tools for RFX 20 futures on **Primary REMARKETS**: discover
contracts, record market data, inspect snapshots, and assess replay readiness.
Separate GGAL carry research combines PPI production spot reads with REMARKETS
simulated futures, or runs entirely offline with synthetic inputs.

**Python 3.10+** · **Demo environment** · **Research in progress**

[Quick start](#quick-start) · [Commands](#commands) · [Next steps](#next-steps) · [Documentation](#documentation)

---

## Current status

Verified locally on **2026-10-08** with offline tests; no credentialed market-data
request or order was made during this documentation update.

| Area | Status |
| --- | --- |
| REST discovery & snapshots | Implemented; historical read-only evidence collected |
| WebSocket recording & reconnect | Implemented; exploratory transport recovery observed |
| PPI discovery & carry calculations | Implemented; read-only, bounded, metadata/freshness limitations remain |
| GGAL October/December monitor | Offline demo implemented; mixed-provider proxy and explicit manual-check mode available, not live-validated |
| Offline tests | **208 passing** with `python -m unittest discover -s tests -v` |
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

### Separate PPI read-only research

`ppi-readonly credentials-status` reports names/statuses only. Fill `PPI_API_KEY`
and `PPI_API_SECRET` locally in ignored `.env`; the two client identifiers have
official SDK defaults and optional overrides. Primary credentials are not reused.
`ppi-readonly discover --live` opts into bounded production market-data reads,
never orders. `ppi-readonly assess` is offline by default. See
[PPI GGAL carry research](docs/PPI_GGAL_ARBITRAGE.md) for hypothetical scenarios,
metadata/freshness blockers, and bid/ask formulas. No executable arbitrage claim
is possible from instrument names or receipt timestamps alone.

GGAL **October/December 2026 recurring console alerts** work offline now:

```powershell
ppi-readonly watch --demo --interval 1 --iterations 8
ppi-readonly watch --demo --interval 0 --iterations 4 --json
```

The deterministic demo repeats no-edge, cash-carry edge, reverse edge, and
unavailable inputs for both expiries. It makes no network/credential reads.
Each line shows `Fteo-Fobs` (theoretical minus observed), direction and net edge.
All alerts are **non-executable**. Omit `--iterations` to repeat until Ctrl+C.

For a later explicitly authorized **live-input proxy**, first fill all placeholders
in a local copy of `config/ppi_ggal_watch.example.json`: PPI production GGAL spot
and exact REMARKETS simulated futures `symbol` with `provider=remarkets` and
`market_id=ROFX`, reviewed maturities, units, rates and costs. Legacy PPI futures
configs are rejected. Set `PRIMARY_USER`/`PRIMARY_PASSWORD` alongside PPI keys
in the selected `--env-file`; environment values take precedence. No account,
guessed symbols, automatic discovery or fallback is used:

```powershell
ppi-readonly watch --live --watch-config config/ppi_ggal_watch.local.json --interval 30 --iterations 2
```

This is `LIVE-PROXY`, not verified real-time or executable arbitrage. Market access
and book timestamp semantics remain unverified. Static rate/cost inputs do not
refresh automatically. Each cycle has two bounded logins, one PPI spot read and
two REMARKETS futures reads. Outputs label the mixed production/simulated sources.
Unknown REMARKETS book timestamps block strict mode; `LA.date` is not book time;
identity-free snapshots are accepted only when bound to the adapter's trusted
request for the exact `GGAL/OCT26` or `GGAL/DIC26` target. Present wrong identity
still blocks. Console `BOOK` lines and JSON `books` retain all sorted BI/OF levels,
quantities, depth, identity basis and separate receipt/source timestamps, even
when strict assessment is unavailable. Calculations use best levels only.
Saved simulated details report multiplier 100, conversion 1 and maturity dates
2026-10-29 / 2026-12-29, **not** verified exact maturity times/timezones. Review
contract semantics and supply your own exact aware maturities, rates and costs.
Repeated authentication can cause throttling. See the [watch procedure and gates](docs/PPI_GGAL_ARBITRAGE.md#recurring-ggal-octoberdecember-2026-watch).

For manual price inspection, watch supports explicit `--manual-check`: timestamp
and invalid-last-trade diagnostics become visible caveats, not price-comparison
blockers. Strict behavior remains the default; quoted depth, inputs and auth
checks remain required. Output is marked `MANUAL-CHECK`, never fresh/executable.
Zero traded volume is not quoted depth and does not block either mode. See
[manual-check limits](docs/PPI_GGAL_ARBITRAGE.md#explicit-manual-price-checks).

Every command supports `--help`. Run from the project root to preserve `.env`
and relative `data/` paths.

| Command | Purpose |
| --- | --- |
| `ppi-readonly` | Read-only PPI discovery, hypothetical carry scenarios, and GGAL demo/proxy monitoring |
| `check-connection` | Discover contracts and fetch REST snapshots |
| `stream-market-data` | Record read-only WebSocket market data |
| `view-order-book` | Inspect the exact `ROFX RFX20/OCT26` REST book |
| `validate-live` | Run bounded read-only demo validation; requires `--live` for network access |
| `collect-session-sources` | Fetch public reference and service-status pages |
| `reanalyze-demo` | Reassess saved evidence offline without modifying originals |
| `replay-readiness` | Audit saved data against the replay acceptance policy |
| `smoke-demo-order` | Guarded demo order harness; no network or order by default |

**Live validation and demo orders are separate procedures.** REMARKETS is a 24/7
test environment, not a guarantee of liquidity or fresh quotes. Strict validation
still requires reviewed session evidence. See the [technical notes](docs/TECHNICAL_NOTES.md)
and [demo safety procedure](docs/DEMO_ORDER_TEST.md) before any credentialed test.

### Run offline tests

```powershell
python -m unittest discover -s tests -v
```

## Project layout

```text
src/market_making/
├── market_data/    # Discovery, recording, instrument rules, book inspection
├── execution/      # Guarded demo order lifecycle
├── ppi/            # Read-only PPI and REMARKETS GGAL carry research/monitoring
└── validation/     # Live validation and offline evidence analysis
config/            # Replay policy and deliberately incomplete GGAL watch template
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

For the separate GGAL track, verify exact contract maturity/units and PPI access,
obtain explicit funding-side conventions and all-in costs, and resolve book
timestamp scope before any separately authorized live-input check. The template
is intentionally not runnable as-is. Manual-check comparisons do not remove
these research limitations or enable orders.

## Documentation

| Guide | What you will find |
| --- | --- |
| [Roadmap](docs/PLAN.md) | Project milestones and acceptance criteria |
| [PPI GGAL carry research](docs/PPI_GGAL_ARBITRAGE.md) | Credentials, discovery limits, carry formulas, GGAL monitor, and manual-check caveats |
| [Technical notes & history](docs/TECHNICAL_NOTES.md) | Recorder behavior, validation modes, and historical evidence |
| [Freshness findings](docs/FRESHNESS_FINDINGS.md) | Known limitations and source citations |
| [Freshness specification request](docs/FRESHNESS_SPEC_REQUEST.md) | Required timestamp and clock evidence |
| [Replay acceptance](docs/REPLAY_ACCEPTANCE.md) | Data quality gates and replay assumptions |
| [Demo order procedure](docs/DEMO_ORDER_TEST.md) | Safety gates, recovery limitations, and review requirements |
| [Primary API reference](https://apihub.primary.com.ar/assets/docs/Primary-API.pdf) | Official REST and WebSocket documentation |

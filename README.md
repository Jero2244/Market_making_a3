# RFX 20 · Market Making & GGAL Research

Python research tools for RFX 20 futures on **Primary REMARKETS**: discover
contracts, record market data, inspect snapshots, and assess replay readiness.
Separate GGAL carry research combines PPI production spot reads with REMARKETS
simulated futures, or runs entirely offline with synthetic inputs.

**Windows desktop app** · **Python 3.10+ for source** · **Research in progress**

[Quick start](#quick-start) · [Commands](#commands) · [Next steps](#next-steps) · [Documentation](#documentation)

---

## Current status

Verified locally on **2026-10-09**: **249 offline tests** and source/packaged
desktop widget checks passed. No credentialed refresh benchmark or order was
made during the desktop/session optimization work.

| Area | Status |
| --- | --- |
| REST discovery & snapshots | Implemented; historical read-only evidence collected |
| WebSocket recording & reconnect | Implemented; exploratory transport recovery observed |
| PPI discovery & carry calculations | Implemented; read-only, bounded, metadata/freshness limitations remain |
| GGAL October/December monitor | Simple price/yield mode and optional full assessment; persistent sessions, bounded renewal and provider backoff |
| Windows desktop app | GGAL prices, yields, Start/Stop, offline demo, settings, depth and snapshot export; executable built and checked locally |
| General market-making bot | Desktop tab is a placeholder; strategy and execution integration remain undeveloped |
| Offline tests | Run `python -m unittest discover -s tests -v` (deterministic offline coverage) |
| Exchange freshness & strict-session validation | **Blocked** — timestamp, clock, and session evidence unresolved |
| Book reconstruction & strategy replay | **Blocked** — update rules and data quality not yet verified |
| Demo order sending | **Blocked** — freshness, account readiness, and independent review required |

> **Research tools, not a production trading bot.** No orders have been sent by
> this implementation. Receiving data does not establish exchange freshness,
> and offline test results do not authorize trading.

## Quick start

### Windows desktop app

Open `dist/GGALDesk.exe` for the simple GGAL checker. Click **Start live check**
to use your existing `.env`, or **Try demo** for an offline preview. The screen
shows spot/futures bid and ask, implied TNA and your manual caucion comparison.
Live refresh defaults to **5 seconds** and accepts **1 second or longer**.
Sessions are reused, independent provider reads overlap, and provider failures
trigger backoff. These settings do not establish a broker/account rate allowance.
The Market making tab is reserved for later development.
See [GGAL Desk setup and build instructions](docs/DESKTOP.md).

The executable and build environment are generated locally and are **not tracked
in Git**. After cloning on Windows, build the executable with:

```powershell
.\build_desktop.ps1 -Python python
```

Python is needed to build, but the resulting executable includes its runtime.
No credentials are bundled. Select your `.env` in Settings, or keep it beside
the executable; a build in this project's `dist` folder also finds the project's
existing `.env`.

From an editable installation, run `python -m market_making.desktop.app` or
`ggal-desk`. The installation procedure below covers source and console use.

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
Each console record shows `remarkets book: bid xxxx | ask xxxx, ppi ggal price:
bid xxxx | ask xxxx implied yield XX% caucion XX%`, with expiry and status.
Implied yield is gross annual nominal cash-carry yield using the futures bid
and spot ask; caucion is the configured borrow TNA, not a live rate quote.
Use `--verbose` for both directions' `Fteo-Fobs`, net edges, full books and timing
diagnostics, or `--json` for complete structured records.
All alerts are **non-executable**. Omit `--iterations` to repeat until Ctrl+C.

For a simple **live price/yield comparison**, use the existing `.env` directly:

```powershell
python -m market_making.ppi.cli watch --live --interval 5
```

No JSON configuration is needed. Prices are assumed to be pesos per share, with
scale 1 and basis 365. Maturity is the last Monday-Friday of the contract month
at 23:59:59 Buenos Aires time (October 30 and December 31, 2026), as a user
assumption; holidays and official expiry rules are not applied. This mode shows
`PRICE CHECK` and the gross implied yield without requiring costs or contract size.
Use `--caucion-tna 30` to supply an example manual 30% TNA, or set `CAUCION_TNA`
in `src/market_making/ppi/monitor_config.py` to your own rate. Until supplied,
caucion displays `n/a`; no funding rate is guessed. The `.env` supplies the PPI
and Primary credentials automatically. `--env-file` selects another file.

For the **full arbitrage assessment**, first fill all placeholders
in a local copy of `config/ppi_ggal_watch.example.json`: PPI production GGAL spot
and exact REMARKETS simulated futures `symbol` with `provider=remarkets` and
`market_id=ROFX`, reviewed maturities, units, rates and costs. Legacy PPI futures
configs are rejected. Set `PRIMARY_USER`/`PRIMARY_PASSWORD` alongside PPI keys
in the selected `--env-file`; environment values take precedence. No account,
guessed symbols, automatic discovery or fallback is used:

```powershell
ppi-readonly watch --live --manual-check --watch-config config/ppi_ggal_watch.local.json --interval 30 --iterations 2
```

This is `LIVE-PROXY`, not verified real-time or executable arbitrage. Market access
and book timestamp semantics remain unverified. Static rate/cost inputs do not
refresh automatically. The recurring checker reuses authenticated HTTP sessions;
normal cycles need one PPI spot read and two REMARKETS futures reads. Independent
provider reads overlap; authentication renews only when needed. The default
interval is 5 seconds, the application minimum is 1 second, and transient
provider failures trigger backoff. See [refresh findings](docs/REFRESH_PERFORMANCE.md).
Outputs label the mixed production/simulated sources.
Unknown REMARKETS book timestamps block strict mode; `LA.date` is not book time;
identity-free snapshots are accepted only when bound to the adapter's trusted
request for the exact `GGAL/OCT26` or `GGAL/DIC26` target. Present wrong identity
still blocks. Verbose console `BOOK` lines and JSON `books` retain all sorted BI/OF levels,
quantities, depth, identity basis and separate receipt/source timestamps, even
when strict assessment is unavailable. Calculations use best levels only.
Saved simulated details report multiplier 100, conversion 1 and maturity dates
2026-10-29 / 2026-12-29, **not** verified exact maturity times/timezones. Review
contract semantics and supply your own exact aware maturities, rates and costs.
Authentication is reused until renewal is needed; throttling slows polling.
See the [watch procedure and gates](docs/PPI_GGAL_ARBITRAGE.md#recurring-ggal-octoberdecember-2026-watch).

For manual price inspection, watch supports explicit `--manual-check`: timestamp
and invalid-last-trade diagnostics become visible caveats, not price-comparison
blockers. Strict behavior remains the default; quoted depth, inputs and auth
checks remain required. Output is marked `MANUAL-CHECK`, never fresh/executable.
Seconds of skew (including 1, 5, 29, 31 or 120 seconds) do not change manual
price eligibility. Missing, naive, stale or future source times are advisory;
unknown source ages/skew remain null, never replaced by receipt times. Omit
`--manual-check` for strict timestamp gates. A missing/invalid future blocks only
that expiry; unavailable spot blocks both while retaining the returned books.
Empty data is not diagnosed as market closure. Production futures remain unresolved.
Zero traded volume is not quoted depth and does not block either mode. See
[manual-check limits](docs/PPI_GGAL_ARBITRAGE.md#explicit-manual-price-checks).

Every command supports `--help`. Run from the project root to preserve `.env`
and relative `data/` paths.

| Command | Purpose |
| --- | --- |
| `ggal-desk` | Native desktop GGAL price/yield checker; Market making placeholder |
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
├── desktop/        # Native GGAL GUI and background monitor controller
├── market_data/    # Discovery, recording, instrument rules, book inspection
├── execution/      # Guarded demo order lifecycle
├── ppi/            # Read-only PPI and REMARKETS GGAL carry research/monitoring
└── validation/     # Live validation and offline evidence analysis
config/            # Replay policy and deliberately incomplete GGAL watch template
docs/              # Procedures, findings, and roadmap
tests/             # Offline regression tests
data/              # Local evidence and historical artifacts (git-ignored)
build_desktop.ps1  # Isolated Windows executable build
dist/              # Generated GGALDesk.exe (git-ignored)
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

For faster GGAL updates, validate streaming subscriptions, book/trade events,
reconnection and source timestamps before integrating the providers' streaming
feeds. The current desktop checker uses REST. See
[refresh findings and further options](docs/REFRESH_PERFORMANCE.md).

## Documentation

| Guide | What you will find |
| --- | --- |
| [GGAL Desk](docs/DESKTOP.md) | GUI controls, credentials, portable use, Windows build and widget checks |
| [Refresh performance](docs/REFRESH_PERFORMANCE.md) | Token lifetime sources, session reuse, scheduling/backoff and streaming options |
| [Roadmap](docs/PLAN.md) | Project milestones and acceptance criteria |
| [PPI GGAL carry research](docs/PPI_GGAL_ARBITRAGE.md) | Credentials, discovery limits, carry formulas, GGAL monitor, and manual-check caveats |
| [Technical notes & history](docs/TECHNICAL_NOTES.md) | Recorder behavior, validation modes, and historical evidence |
| [Freshness findings](docs/FRESHNESS_FINDINGS.md) | Known limitations and source citations |
| [Freshness specification request](docs/FRESHNESS_SPEC_REQUEST.md) | Required timestamp and clock evidence |
| [Replay acceptance](docs/REPLAY_ACCEPTANCE.md) | Data quality gates and replay assumptions |
| [Demo order procedure](docs/DEMO_ORDER_TEST.md) | Safety gates, recovery limitations, and review requirements |
| [Primary API reference](https://apihub.primary.com.ar/assets/docs/Primary-API.pdf) | Official REST and WebSocket documentation |

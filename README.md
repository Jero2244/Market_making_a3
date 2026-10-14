# GGAL Desk

A desktop helper for futures traders: compare GGAL spot and futures prices,
check implied carry against your caucion rate, and explore the P/L of manually
entered positions.

The app has two screens:

- **GGAL arbitrage**: bid/ask prices, implied TNA, manual caucion comparison,
  book details, snapshot export and an optional assessment with funding and costs.
- **Position scenarios**: long/short GGAL positions, projected gross P/L,
  historical return distributions and horizon-based probability estimates.

Spot reads use **PPI production**; futures reads use **REMARKETS simulated**.
Positions are entered manually. The app does not import broker holdings or send
orders. Source labels and quote-quality diagnostics stay visible.

## Windows quick start

Open `dist/GGALDesk.exe`. Choose your credentials file in **Settings**, then
click **Start live check**. Use **Try demo** for a synthetic preview without
credentials or a network connection. The executable is generated locally and
is not committed to Git.

To rebuild it with Python installed:

```powershell
.\build_desktop.ps1 -Python python
```

See [desktop usage](docs/DESKTOP.md) for controls, scenarios and portable copies.

## Run from source

Requires Python 3.10 or newer and Tkinter.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m market_making.desktop.app
```

The installed desktop shortcut is `ggal-desk`; the console tool is
`ppi-readonly`. The project distribution is named `ggal-desk`. The Python import
namespace remains `market_making` to preserve existing integrations.

For an existing checkout installed under the old project name, remove its
obsolete installed launchers before reinstalling:

```powershell
.\.venv\Scripts\python.exe -m pip uninstall rfx20-market-making
.\.venv\Scripts\python.exe -m pip install -e .
```

## Credentials

Copy `.env.example` to `.env` only if you do not already have a credentials file.
Fill in the PPI key/secret and Primary user/password locally. Existing environment
variables take precedence over file values. Credentials stay local and ignored
by Git; they are not included in the executable or archive.

Manual spot mode needs only Primary credentials. Position spot/history fetches
need only PPI credentials.

## Contract assumptions

[Shared contract settings](src/market_making/contracts.py) define the monitored
symbols, display labels, assumed expiries and shares per contract. Both screens
use these defaults:

| Contract | Assumed expiry date | Shares per contract |
| --- | --- | --- |
| GGAL/OCT26 | 2026-10-30 | 100 |
| GGAL/DIC26 | 2026-12-30 | 100 |

The checker assumes end of day in Buenos Aires and a 365-day rate basis.
These are user assumptions, not verified exchange specifications. Edit the
shared settings when rolling contracts, then restart the app and rebuild any
executable. Editing an expiry in Position scenarios affects only that scenario.
A full-assessment JSON is an explicit override for that assessment.

## Console monitoring

```powershell
ppi-readonly watch --demo --interval 0 --iterations 4
ppi-readonly watch --live --interval 5 --caucion-tna 30
ppi-readonly credentials-status
```

The live example's 30% caucion TNA is a manual input. It is not a fetched rate.
Instrument discovery and hypothetical carry calculations remain optional:
`ppi-readonly discover --help` and `ppi-readonly assess --help`.
See [checker modes and formulas](docs/PPI_GGAL_ARBITRAGE.md).

## Verification

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
New-Item -ItemType Directory -Force build | Out-Null
.\.venv\Scripts\python.exe -m market_making.desktop.app --self-test build/source_gui_check.json
```

These checks use synthetic data, mocks and local test servers. The widget check
also runs against `dist/GGALDesk.exe --self-test build/exe_gui_check.json`.

Verified on 2026-10-14: **214 offline tests** passed in the isolated desktop
runtime, and both source and rebuilt executable widget checks passed. No live
broker requests or orders were made during the cleanup.

## Project layout

```text
src/market_making/
  contracts.py     Shared contract assumptions
  desktop/         The two-screen Windows interface
  ppi/             Read-only provider adapters and GGAL monitoring
  scenarios/       Position P/L, historical returns and trading sessions
config/            Optional full-assessment template
tests/             Active helper regression tests and GGAL fixtures
docs/              Usage, formulas, refresh behavior and data limitations
archive/           Preserved original project and retired bot tools
data/              Current local working data, ignored by Git
build/, dist/      Generated build output, ignored by Git
```

The bot execution tools, replay validation, separate RFX20 data tools and
competition roadmap are preserved in [the archive](archive/README.md), outside
the installed app and active test suite. Historical local research data remains
ignored under `archive/market_making/data/`; earlier generated outputs live under
`archive/generated/`. Hidden virtual environments and locked caches remain local.

Read [data limitations](docs/DATA_LIMITATIONS.md) when interpreting prices and
P/L, and [refresh behavior](docs/REFRESH_PERFORMANCE.md) for connection details.

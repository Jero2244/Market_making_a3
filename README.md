# GGAL Desk

A local desktop app for analysing GGAL stock futures in Argentina. Compare spot
and futures prices, check implied financing against your own caucion rate, and
explore how stock moves and financing assumptions affect a futures position's
profit or loss (P/L).

The active app is **read-only: it does not send orders**. It combines PPI
production spot data, simulated reMarkets futures and account data, and a
separate comparison with real futures quotes from A3's public price viewer.
The source of each price stays visible.

## Is this useful for you?

GGAL Desk is useful if you want to:

- Follow GGAL futures in reMarkets and compare them with real-market references.
- Compare the annualised spot-to-futures premium with a funding rate you enter.
- Model long/short positions manually or import your simulated account's GGAL
  futures, then explore different stock moves and rates.
- Inspect historical GGAL return ranges and estimated end-of-horizon P/L
  probabilities.
- Try the interface offline before setting up broker credentials.

The desktop workflow and executable build target Windows. Price monitors
currently focus on October and December 2026 GGAL futures; account imports can
also include other validated GGAL expiries. Automated trading and the original
market-making experiments are preserved separately in [the archive](archive/README.md).

## Recent changes — 14 October 2026

- **Simulated-account imports:** load GGAL futures quantities, entry bases,
  valuation prices and expiries from reMarkets, alongside account-wide funds and
  margin. Copy the snapshot into a manual simulation to edit assumptions.
- **Real vs reMarkets screen:** compare A3 and simulated futures bid/ask prices,
  with adjustable minimum differences in ticks and maximum observation age.
- **Manual spot bid/ask:** enter both GGAL spot sides and apply changes while the
  carry monitor is running, using only Primary credentials.
- **Expanded scenarios:** choose a trading-day horizon, change the assumed
  future TNA, inspect historical return distributions and estimate combined
  position P/L probabilities.
- **Focused project layout:** the installed app contains the GGAL analysis desk;
  retired execution, replay and research tools live under `archive/`.

## What the three screens do

### GGAL arbitrage: spot, futures and financing

Click **Start live check** to read GGAL spot bid/ask from PPI production and
`GGAL/OCT26` / `GGAL/DIC26` futures from simulated reMarkets. The default refresh
is five seconds. Enter **Caucion TNA (%)** to compare your annual nominal
borrowing rate with the futures' gross implied TNA. The caucion rate is a manual
input; the app does not fetch it.

With spot ask `S`, futures bid `F` and calendar days to assumed expiry `D`:

```text
Gross implied TNA (%) = (F / S - 1) × 365 / D × 100
```

For your own spot reference, select **Manual GGAL spot**, enter positive bid and
ask prices in ARS per share, and click **Apply bid/ask** or press Enter. Both
prices are retained; the calculation uses the ask. Changes take effect on the
next refresh. Uncheck the option to return to fetched spot.

**Book details** shows depth and quote diagnostics; **Export snapshot** saves the
latest reports as JSON. An optional reviewed configuration enables a fuller
cash-carry/reverse-carry assessment with explicit funding and costs. Manual spot
mode supports the simple checker only. See [checker modes and
formulas](docs/PPI_GGAL_ARBITRAGE.md).

### Position scenarios: what happens to your P/L?

1. Enter GGAL spot manually or click **Fetch PPI spot** for the current
   last-trade price. It is a different reference from the carry screen's spread.
2. Choose **Manual simulation** and enter Long/Short, whole contract count,
   average entry, current futures price and expiry. Alternatively, select
   **reMarkets account** and click **Load / refresh account**.
3. Choose **Trading days** from 1 to 252, enter the signed **Stock move (%)**,
   and optionally set **Assumed future TNA (%)**. Blank keeps each future's
   current implied TNA; zero assumes a zero rate. **Use caucion** copies the
   rate from the carry screen.
4. Click **Calculate** to see projected spot and futures prices, P/L from today,
   P/L since entry, and combined totals.

The model applies your stock move to spot and reprices each future using the
assumed TNA and remaining calendar days. It multiplies the futures price change
by signed contracts and shares per contract. Trading dates use the XBUE calendar
with the project's 2026 corrections; financing decay uses actual elapsed calendar
days. If an active position expires on or before the scenario date, that leg
and the combined results are unavailable.

Account imports include **outright GGAL futures only**. GGAL shares, options and
other instruments are excluded from scenario P/L. Additional expiries are
checked against reMarkets metadata, up to 24 distinct GGAL expiries. Imported
rows are read-only snapshots; **Use as manual simulation** makes an editable
copy. Cash, available funds, margin and collateral describe the whole simulated
account, including excluded instruments. Refresh explicitly to load new
holdings; failed or cancelled refreshes clear the previous import.

**Copy simulated futures** fills manual current-price inputs with valid
midpoints from the carry monitor's latest snapshot, retaining source labels.
Position inputs and loaded history stay in the current session and are not
saved on exit.

For probabilities, fetch one, three or five years of PPI daily GGAL history.
**Return distribution** shows historical frequencies and a smoothed probability
density for return or final-price ranges. **Futures P/L** estimates the chance of
meeting the combined scenario P/L from today and of finishing profitable. Both
use the selected horizon; changing it reuses loaded history. At least 30 valid
historical return windows are required. Today's unfinished record is excluded;
missing sessions are not filled.

### Real vs reMarkets: compare the same futures across sources

Click **Start comparison** to compare real A3 viewer quotes with simulated
reMarkets books for October and December 2026. The displayed directions are:

- **Buy difference:** A3 bid − reMarkets ask.
- **Sell difference:** reMarkets bid − A3 ask.

The screen shows differences in ARS per share, ticks, percent and gross ARS per
contract, plus quoted quantities. The default highlight threshold is one tick;
the default maximum observation age is ten seconds. Edit either control and
click **Apply** without restarting.

A3 uses an anonymous WebSocket with subscription snapshots refreshed every five
seconds. reMarkets is polled every second by a separate reader. Slow requests do
not overlap. Missing sides disable the affected direction; invalid books pause
comparisons, and old observations appear gray. Highlights are visual only.
This screen starts and stops independently of the carry monitor.

## Try it locally

### Run from source

Requires **Python 3.10 or newer with Tkinter**. From the repository root in
PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m market_making.desktop.app
```

Click **Try demo** in the carry or comparison screen for synthetic prices with
no credentials or network access. You can also calculate position scenarios
offline using manually entered spot, entry and futures prices. Fetching history
for probabilities requires PPI access.

The installed desktop launcher is `ggal-desk`; the console tool is
`ppi-readonly`. The distribution is named `ggal-desk`, while Python imports
remain under `market_making` for compatibility. If this checkout was previously
installed under the old name, remove its obsolete launchers before reinstalling:

```powershell
.\.venv\Scripts\python.exe -m pip uninstall rfx20-market-making
.\.venv\Scripts\python.exe -m pip install -e .
```

### Build a Windows executable

With Python installed, run:

```powershell
.\build_desktop.ps1 -Python python
```

The script installs build dependencies in an isolated environment and generates
`dist/GGALDesk.exe`. The executable is built locally and is not committed to Git.
Once built, it can run without a separate Python installation. See [desktop
usage](docs/DESKTOP.md) for portable copies and detailed controls.

## Data sources and credentials

| Workflow | Data used | Credentials needed |
| --- | --- | --- |
| Demo or manually entered P/L scenario | Synthetic data or your inputs | None |
| Carry monitor with fetched spot | PPI production spot + simulated reMarkets futures | PPI + Primary |
| Carry monitor with manual spot | Your spot bid/ask + simulated reMarkets futures | Primary |
| Real vs reMarkets | A3 public real-futures viewer + simulated reMarkets futures | Primary; A3 is anonymous |
| Fetch scenario spot or daily history | PPI production | PPI |
| Import account positions and balances | Simulated reMarkets account reports | Primary |

Copy [`.env.example`](.env.example) to `.env` if you do not already have a
credentials file. Fill in the values locally and select the file in **Settings**:

- **Primary/reMarkets:** `PRIMARY_USER` and `PRIMARY_PASSWORD`. A sole available
  account is discovered automatically. Set optional `PRIMARY_ACCOUNT` when your
  login has several accounts.
- **PPI:** `PPI_API_KEY` and `PPI_API_SECRET`, separate from Primary credentials.
  `PPI_AUTHORIZED_CLIENT` and `PPI_CLIENT_KEY` are optional application-identifier
  overrides; blank values use the adapter defaults.

Existing environment variables take precedence over file values. Credentials
stay local, are ignored by Git and are not bundled into the executable. Stop a
running monitor before changing connection settings. Each account refresh
rereads the selected file, so changing account credentials needs no restart.

## Assumptions and practical limits

The carry monitor and default manual scenarios share these settings in
[`contracts.py`](src/market_making/contracts.py):

| Contract | Assumed expiry date | Shares per contract |
| --- | --- | --- |
| `GGAL/OCT26` | 2026-10-30 | 100 |
| `GGAL/DIC26` | 2026-12-30 | 100 |

The carry checker assumes end of day in Buenos Aires and a 365-day basis. These
defaults are user assumptions. Account imports validate expiries and units
against reMarkets metadata; the A3 comparison separately validates its explicit
contract mapping. Rolling the carry monitor requires editing the shared
settings; rolling the A3 comparison also requires updating its mappings in
[`comparison/models.py`](src/market_making/comparison/models.py) and
[`comparison/a3.py`](src/market_making/comparison/a3.py). Restart and rebuild any
executable after changes. A scenario expiry edit affects only that scenario;
a full-assessment JSON explicitly overrides its own assumptions.

Keep these limits in mind when deciding whether the tool fits your workflow:

- **Real and simulated prices have different roles.** The carry screen uses
  simulated futures. A3 differences provide real-market references, without
  establishing an executable offsetting trade or a competition's final value.
- **Receipt age is not exchange freshness.** A newly received snapshot may
  contain old or end-of-session prices. The public A3 viewer is a website feed;
  format changes can require an adapter update.
- **Ordinary carry and scenario P/L are gross.** They exclude commissions,
  taxes, execution spreads, margin funding and settlement cashflow adjustments.
  Full assessment accepts explicit costs but still does not establish fillability.
- **Account valuations are snapshots.** Risk-report prices may predate the
  request, and daily settlement may reset the API's entry basis. Since-entry
  P/L is an economic calculation, not an unpaid broker balance.
- **Historical probabilities depend on assumptions.** Overlapping return
  windows are not independent forecasts. Estimates describe the selected
  end-of-horizon outcome under fixed rate assumptions, with all legs sharing
  the same stock return; they do not describe interim drawdowns.

See [data limitations](docs/DATA_LIMITATIONS.md) and [refresh and connection
behavior](docs/REFRESH_PERFORMANCE.md) for details.

## Console monitoring

After installing, use the console launcher from your virtual environment:

```powershell
.\.venv\Scripts\ppi-readonly.exe watch --demo --interval 0 --iterations 4
.\.venv\Scripts\ppi-readonly.exe watch --live --interval 5 --caucion-tna 30
.\.venv\Scripts\ppi-readonly.exe credentials-status
```

The live example's 30% caucion TNA is an illustrative manual input. `--json`
emits one record per expiry per cycle, `--verbose` adds books and diagnostics,
and `--env-file` selects a credentials file. Without `--iterations`, monitoring
continues until Ctrl+C. Live monitoring requires explicit `--live`.

The console covers the carry checker and optional `discover` / `assess`
utilities. Account import, historical probability views and the A3 comparison
are desktop workflows. See [console modes](docs/PPI_GGAL_ARBITRAGE.md).

## Verification and current status

Run the offline suite and desktop widget check after installation:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
New-Item -ItemType Directory -Force build | Out-Null
.\.venv\Scripts\python.exe -m market_making.desktop.app --self-test build/source_gui_check.json
```

For a built executable, the equivalent widget check is:

```powershell
.\dist\GGALDesk.exe --self-test build/exe_gui_check.json
```

These checks use synthetic data, mocks and local test servers. They cover price
parsing, carry and P/L calculations, history/probabilities, account filtering,
connection handling and desktop controls; they do not validate live execution
or market-hours data quality.

Checked on **2026-10-14**: all **264 offline tests passed on a full rerun** in the
isolated desktop runtime. The source widget check and four-cycle console demo
also passed. One comparison-demo test failed on the initial suite run and
passed both separately and on the full rerun; that intermittent failure remains
to be investigated.

A previously recorded read-only after-hours check received both GGAL contracts
from A3 and reMarkets, including A3 subscription refreshes and clean Stop
behavior. Open-market comparison and timing validation remain pending. See the
[recorded check](docs/REFRESH_PERFORMANCE.md).

## Project layout and further reading

```text
src/market_making/
  contracts.py     Shared GGAL contract assumptions
  desktop/         The three-screen Tkinter interface
  comparison/      A3 viewer adapter and reMarkets difference monitor
  ppi/             Read-only PPI/reMarkets adapters and carry CLI
  scenarios/       Account imports, position P/L, history and probabilities
config/            Optional full-assessment template
tests/             Active regression tests and GGAL fixtures
docs/              Usage, formulas, refresh behavior and limitations
archive/           Preserved original project and retired bot tools
data/              Local working data, ignored by Git
build/, dist/      Generated build output, ignored by Git
```

- [Desktop guide](docs/DESKTOP.md): controls, account imports and scenario use.
- [Carry checker guide](docs/PPI_GGAL_ARBITRAGE.md): formulas, full assessments
  and console options.
- [Data limitations](docs/DATA_LIMITATIONS.md): interpreting quotes and results.
- [Connection behavior](docs/REFRESH_PERFORMANCE.md): refresh, cancellation and
  the recorded after-hours check.
- [Archive guide](archive/README.md): original market-making code, execution
  tools, replay validation and historical research.

Historical local research data remains ignored under
`archive/market_making/data/`; earlier generated outputs live under
`archive/generated/`.

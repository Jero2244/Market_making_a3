# GGAL Desk

Open `dist/GGALDesk.exe`. Python is bundled; a separate Python installation is
not required. The app opens idle. Click **Start live check** to connect using
your existing `.env`, or **Try demo** to see synthetic scenarios without a
connection or credential reads. The **Market making** tab is a placeholder.
The **Position scenarios** tab provides a separate, session-only futures P/L
calculator and historical return distribution with a selectable trading-day horizon.

The main screen shows GGAL spot bid/ask, October and December 2026 futures
bid/ask, gross implied cash-carry TNA, and its difference from your manual
caucion rate in percentage points. Leave the rate blank to show unknown.
Live refresh accepts 1 second or longer and defaults to 5 seconds; demo
refreshes every 2 seconds. Request time counts toward the selected interval.
Slow cycles never overlap. Provider transport failures or throttling trigger
30/60/120/240/300-second backoff, then the selected interval resumes after a
transport-successful cycle. This is an application setting, not a verified
broker rate allowance. See [refresh findings](REFRESH_PERFORMANCE.md).
Connection settings are locked while monitoring; stop before changing them.

To enter Galicia spot yourself, select **Manual GGAL spot (ARS/share)** before
starting, enter a positive price, and click **Start live check**. Use **Apply
price** to change it during monitoring; the next refresh uses the updated value.
Uncheck the option while stopped to return to PPI. Manual mode needs only the
Primary credentials for futures and skips PPI requests. The display, book
details and exports label the spot as user input. This single reference price
is used for gross yield comparison; spread, depth and source freshness are
unknown. A full assessment remains unavailable with this reference price.

Spot prices come from **PPI production**, while futures come from **REMARKETS
simulated**. This is the existing read-only proxy checker. It does not send
orders or establish source freshness. A yield above caucion is a gross
comparison, not a net executable opportunity. The simple checker uses the
existing last-weekday month-end maturity assumption and 365-day basis.

**Settings** lets you choose your `.env` and, optionally, a reviewed full
assessment JSON matching `config/ppi_ggal_watch.example.json`. Full assessment
uses rates, costs and units from that file: clear the main caucion field.
Strict timestamp gates apply unless you select the manual-check option.
See [the checker guide](PPI_GGAL_ARBITRAGE.md) for these modes and assumptions.

The executable first looks for `.env` beside itself. When it lives in this
project's `dist` folder, it can reuse the project's existing `.env`. For a
portable copy, select the credentials file in Settings or put your own `.env`
beside the executable. Credentials are **not included in the build**.
Environment variables keep their existing precedence over `.env` values.

**Book details** displays normalized depth and diagnostic data for the last
received snapshot. **Export snapshot** saves those reports as JSON. The
last-received time is a local receipt time, not exchange freshness. After a
failed cycle, missing prices replace the previous prices. Stop interrupts
the refresh wait immediately and cancels active sockets. Restart becomes
available after worker cleanup; late results are discarded. Closing the app
stops the worker without blocking the window.

Authenticated HTTP sessions persist until Stop. PPI uses its returned expiry
and refresh token to renew when needed; Primary renews ahead of its documented
24-hour expiry. A quote's HTTP 401 permits one renewal and one retry per
provider per cycle; a second 401, a 403 or invalid authentication stops the
run. Spot and futures are read concurrently, while each provider's client is
used sequentially. The status line shows the last acquisition duration.

## Position scenarios

The third tab works independently of the arbitrage monitor. Enter GGAL spot in
ARS per share, or click **Fetch PPI spot** to request the current PPI last-trade
price using the credentials file selected in Settings. Only PPI credentials are
needed for the scenario tab. The source timestamp is shown when supplied;
it is not a guarantee of freshness. PPI current spot requires today's reference
date. For an earlier reference date, enter that day's spot manually.

Open **Futures P/L** within Position scenarios. For October and December 2026,
enter **Long** or **Short**, the number of
contracts, average entry price and current futures price. Prices are ARS per
share; each contract represents **100 shares**. Zero contracts disables the
row and excludes it from the calculation. The editable expiry defaults are
**2026-10-30** and **2026-12-30**. These are scenario inputs, not verified
execution metadata, and do not change the arbitrage checker's maturity settings.

**Copy simulated futures** explicitly copies valid bid/ask midpoints from the
latest arbitrage snapshot. Its receipt timestamp and REMARKETS-simulated or
synthetic-demo label stay beside the imported prices. Editing a copied price
changes its label to manual input. Spot, position direction, quantity and entry
prices are not replaced by this action. Missing, crossed, locked or structurally
invalid books are not copied.

Enter a signed **Stock move (%)** and click **Calculate**. A value of `5`
means a 5% rise; `-5` means a 5% fall. Decimal points and decimal commas are
accepted. Whole-number contract counts are required. Input edits clear the old
results until Calculate is clicked again.

Set **Trading days** to a whole number from **1 to 252** (default 2).
The scenario date is that number of trading sessions strictly after the reference date,
using Buenos Aires dates and the XBUE exchange calendar. Weekends and trading
closures are skipped; settlement-only holidays still count as trading sessions.
The 2026 calendar includes corrections checked against
[BYMA's calendar](https://www.byma.com.ar/mercado/calendario-bursatil) on
2026-10-14, including November 9/10 closures and December 31. Future calendar
changes require a library/correction update. Actual elapsed calendar days are
displayed and used in financing decay.

Set **Assumed future TNA (%)** in Futures P/L to the financing rate you expect
the futures to reflect on the scenario date. It applies to both October and
December. **Use caucion** copies the manual Caucion TNA from GGAL arbitrage.
Before you edit the assumed rate yourself, it is prefilled and follows that
caucion input when valid. A manual override remains independent; Use caucion
re-enables that link. The value stays in this session and is not an API rate.

Leaving the assumed rate blank explicitly preserves each future's current
implied TNA. A value of **0** is an explicit zero-rate assumption. Negative
assumptions are accepted if they produce positive projected prices; nonfinite
rates are rejected. Editing the assumption clears previous P/L until Calculate
is clicked again.

**Implied TNA** still describes today's spot/future prices on an ACT/365 basis.
With current spot S, future F and calendar days to expiry D, it is
`(F / S - 1) * 365 / D * 100`. **Projection TNA** shows the actual percentage
rate used: your assumption when entered, otherwise that leg's implied TNA.
For projected spot S' and remaining calendar days D', the projected future is
`S' * (1 + projection TNA / 100 * D' / 365)`.

An assumed rate models repricing to that rate by the scenario date. P/L can
therefore change even with a zero stock move, through both time decay and a
change in the spot/future relationship. An active position whose expiry is on or before the
scenario date is unavailable; the other leg can still be shown, but the
combined total is suppressed.

The table shows projected futures prices, current implied TNA, projection TNA and two signed ARS
results for each expiry and their sum:

- **P/L from today**: signed contracts × 100 × (projected future − current future).
- **P/L since entry**: signed contracts × 100 × (projected future − average entry).

The **Futures P/L** summary shows the combined **Scenario P/L from today**,
the chance of finishing with **at least that amount**, and the chance of
finishing **profitable (strictly above zero)** at the selected horizon.
Fetch history directly in this section or reuse the history loaded in Return
distribution; both views share the lookback and trading-day horizon. The two
percentages use the same smoothed historical return distribution and the
scenario's TNA assumptions. All legs share each stock outcome, so shorts and
offsetting positions are combined before calculating probabilities.
These are end-of-horizon estimates. At least 30 valid historical periods are
required. Editing scenario inputs clears the old summary until Calculate is
clicked again; unavailable active expiries suppress the combined probabilities.

These are gross theoretical results before commissions, spread, taxes and
margin financing. Since-entry P/L is cumulative economic P/L and includes
differences that may already have been settled. It is not the unpaid balance
in the broker account.

Choose **1, 3 or 5 years** (default 3) and click **Fetch / refresh PPI history**.
The tab requests local GGAL/ACCIONES/INMEDIATA daily prices in bounded annual
chunks. PPI's documented
[historical endpoint](https://itatppi.github.io/ppi-official-api-docs/api/documentacionRest/)
is added to the existing read-only allowlist. Each action is limited to twelve
requests, 120 seconds, and the existing 2 MB ceiling per response; a rejected
authentication read permits at most one renewal/retry. A failed action does not
publish partial history. **Cancel fetch**, a replacement request, or closing
the app interrupts active transport reads and discards late results. Editing
spot during a pending spot read cancels it so manual input is preserved.

Open **Return distribution**, the default view within Position scenarios.
Here PDF means **probability density function**. The selected Trading days
setting applies to both the distribution and the futures P/L calculation.
Changing it recomputes historical returns from the loaded prices without
another PPI request or a square-root-of-time approximation.

Historical N-session returns are `close[t+N] / close[t] - 1` across N+1
consecutive trading dates. Today's unfinished record is excluded. Prices are
sorted by session date; identical daily closes are deduplicated, conflicting
dates and invalid prices are excluded. A missing session excludes each window
crossing it; missing prices are never filled. Large valid moves are retained.
The tab shows the actual date coverage, missing sessions, excluded windows,
matching count and total sample size. At least **30 valid windows** are needed
to show a frequency. Shorter coverage is displayed, not represented as the full
requested lookback.

For `+5%`, the displayed frequency counts returns **at least +5%**; for `-5%`,
it counts returns **at or below −5%**. At zero it counts nonnegative returns.
This is an observed close-to-close frequency for the chosen horizon, not a
probability of touching a price during a session. The orange marker shows
the selected Stock move. Overlapping
periods are not independent, past frequencies do not predict future moves,
and PPI's dividend/split adjustment treatment remains unverified.

Use **End-of-horizon range** to enter lower and upper bounds in either
**Return (%)** or **Final price (ARS)**. Choose **From X to Y** for an inclusive
interval, or **More than X**, **Less than X**, **>= X**, or **<= X** for a
single threshold. The second input is hidden for threshold comparisons.
More/less than exclude outcomes equal to X; >=/<= include them in historical
counts and constant-return samples. The chart shades the selected interval
or tail. For example, with hypothetical spot
10,000 ARS, a final-price range of 9,500–10,500 is equivalent to −5% through +5%.
Switching units converts the existing bounds when a valid spot is available.
Prices must be positive and the lower bound cannot exceed the upper bound.
The return view works without a spot or futures position; the price view needs
spot to translate returns into possible final prices.

The range result displays a **smoothed estimate**, the **historical frequency**
and matching count, plus historical percentages below and above the range
or the complementary comparison for a single threshold.
The grey bars are a normalized historical density. The blue curve is a Gaussian
kernel density estimate (KDE) of log growth factors, transformed back into
percentage returns or final prices. This transformation keeps price outcomes
positive. The green area shows the requested range; the estimate is calculated
from Gaussian CDF differences, independently of chart pixels. Density is
measured per percentage point or per ARS, and height at one point is not itself
a probability. The three empirical regions sum to 100%.

Bandwidth uses
[Silverman's robust univariate rule](https://stat.ethz.ch/R-manual/R-devel/library/stats/html/bandwidth.html),
with the sample standard deviation used when the interquartile range is zero.
The curve smooths observed outcomes rather than assuming all returns follow
one normal distribution. A constant sample is shown as a point mass without
inventing volatility. A single-point interval has zero probability under a
continuous smooth curve, while its historical count can be nonzero. The view
also shows historical 5th, median and 95th percentile outcomes.

These are unconditional estimates based on the selected historical sample.
They do not incorporate today's news, a volatility forecast or a financing-rate
forecast. At least 30 complete N-day periods are required; a longer horizon
can leave too few observations, particularly with a short lookback or gaps.

Inputs and history are kept only in memory and reset when the app closes.
History failures do not prevent manual P/L calculations. Changing the lookback
or reference date clears the old historical sample before another fetch.

## Run from source

After the existing editable installation:

```powershell
python -m market_making.desktop.app
```

The installation also provides the windowed `ggal-desk` shortcut.

## Rebuild on Windows

```powershell
.\build_desktop.ps1 -Python python
```

This creates an isolated `.venv-desktop-build` and uses PyInstaller's
[one-file windowed packaging](https://pyinstaller.org/en/stable/usage.html).
The resulting `dist/GGALDesk.exe` bundles the GUI and read-only adapters.
It excludes the order harness, validation commands and market-data CLI.
Build artifacts and the build environment are ignored by git.
The GitHub source checkout does not contain `dist/GGALDesk.exe`; run the build
command first. Existing credentials are neither copied into the bundle nor
published with the source.

## Offline checks

On **2026-10-14**, all **330 offline tests** passed, and both source and rebuilt
`dist/GGALDesk.exe` passed the widget checks including Position scenarios.
The checks cover variable horizons, PDFs, percentage/final-price ranges,
assumed future TNAs and caucion prefill/manual overrides.
The PPI scenario paths were tested with mocked responses; no credentialed live
spot/history request was made during this implementation.

```powershell
python -m unittest discover -s tests -v
New-Item -ItemType Directory -Force build | Out-Null
python -m market_making.desktop.app --self-test build/source_gui_check.json
```

The packaged executable accepts the same `--self-test OUTPUT_JSON` option.
It creates hidden real Tk widgets, checks all four synthetic states and
Start/Stop, and writes a small success report. It also checks offline scenario
P/L, simulated-price provenance, the PDF and shaded return/price ranges,
variable horizons, packaged XBUE calendar imports,
and cancellation of late spot updates. No live requests are made.
The check also verifies that live refresh defaults to 5 seconds and accepts
1 second. On 2026-10-09 the full suite passed 249 tests and both the source
and executable passed these widget checks.

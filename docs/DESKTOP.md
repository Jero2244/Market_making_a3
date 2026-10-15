# GGAL Desk usage

## Start and settings

Open `dist/GGALDesk.exe`, or run `python -m market_making.desktop.app` from an
editable installation. The app opens idle with **GGAL arbitrage**,
**Position scenarios**, and **Real vs reMarkets** tabs. **Try demo** previews
synthetic data offline.

In **Settings**, choose your `.env`. A portable executable looks beside itself
for that file; an executable in this project's `dist/` can reuse the project's
file. Existing environment variables take precedence. Credentials are never
bundled. Stop monitoring before changing connection settings.

## GGAL arbitrage

**Start live check** reads PPI production spot and REMARKETS simulated futures.
The cards show bid/ask, gross cash-carry implied TNA, and its difference from
your manual **Caucion TNA (%)**. Blank caucion means unknown. The default refresh
is five seconds, with a minimum of one second. Failed requests trigger backoff;
cycles do not overlap. See [refresh behavior](REFRESH_PERFORMANCE.md).

Select **Manual GGAL spot** before starting or while monitoring. Enter positive
**Bid** and **Ask** prices in ARS/share, with bid no greater than ask. Click
**Apply bid/ask** or press **Enter** to apply both prices on the next refresh.
Blank fields start with the latest spot bid and ask when available.
Uncheck the option to return to PPI spot on the next refresh (or synthetic spot
in demo). PPI spot requires PPI credentials; manual mode uses only Primary
credentials. The cards and exported snapshot preserve both entered prices;
cash-carry implied yield uses the spot ask. Manual quotes provide no verified
market depth or freshness. Full assessment is unavailable with manual spot.

**Book details** shows normalized depth and diagnostics for the last received
snapshot. **Export snapshot** saves the reports as JSON. Receipt time records
when this app received data; it does not certify exchange freshness. Failed
cycles show missing prices instead of silently carrying forward old values.

The ordinary comparison uses shared user-assumed expiry dates and a 365-day
basis. A yield above caucion excludes fees and does not establish an executable
opportunity. In Settings, an optional reviewed JSON based on
`config/ppi_ggal_watch.example.json` enables the full assessment. Clear the main
caucion field when using this file. Strict timestamp checks apply unless the
manual-check option is selected. See [checker modes](PPI_GGAL_ARBITRAGE.md).

**Stop** cancels active reads and interrupts waiting. Restart becomes available
after cleanup. Closing the app cancels its background work.

## Real vs reMarkets

Choose **Real vs reMarkets** and click **Start comparison**. This tab compares
`GGAL/OCT26` and `GGAL/DIC26` in the public A3 viewer with those same futures in
the simulated reMarkets order book. It uses `PRIMARY_USER` and
`PRIMARY_PASSWORD` from the credentials file in Settings; PPI is not required.

A3 updates arrive through the viewer's anonymous WebSocket. Subscription
snapshots refresh every five seconds, including when prices are unchanged.
The reMarkets books are requested every one second; slow reads never overlap.
Each source has an independent background reader. Transient failures clear
that source's observations and reconnect with 1/2/4/8/16/30-second backoff.
Repeated login failure or a changed A3 format stops the comparison with a
message. **Stop** clears highlights immediately and interrupts active reads.

The two directions use executable reMarkets sides and real-market references:

- Buy difference = A3 bid minus reMarkets ask.
- Sell difference = reMarkets bid minus A3 ask.
- Percentage = difference divided by the relevant A3 reference price × 100.
- Gross difference per contract = difference × shares per contract.

The default minimum is **one tick**. A3 metadata supplies the tick and validates
the contract's expiry, currency and 100-share multiplier. **Maximum observation
age** defaults to **10 seconds**. Edit these controls and click **Apply** to
change comparisons without restarting. A zero minimum still requires a strictly
positive difference. Missing sides disable their affected direction; malformed
or crossed books disable both. Old observations stay visible in gray, with
comparisons paused. The largest qualifying difference appears above the cards.
Highlights are visual only; no sounds or order controls are included.

**Observed ago** measures the time since this app received a quote or refreshed
snapshot. It does not measure exchange quote age. Last trades and their supplied
dates are context only. The feed can return end-of-session prices after market
hours; receiving a new snapshot does not make those prices current trading
opportunities. Differences exclude costs and do not guarantee a closing trade
or the competition's final valuation.

**Try demo** cycles through qualifying buys, qualifying sells, ordinary prices,
missing sides, crossed books and old observations. It reads no credentials and
uses no network. This tab starts and stops independently of the other screens.

## Position scenarios

This screen operates independently of the monitor. Enter GGAL spot manually or
use **Fetch PPI spot**, which reads the current last-trade price with PPI
credentials. A current fetch requires today's reference date; for an earlier
date, enter that date's price manually. Broker holdings are not imported.

Under **Futures P/L**, enter Long/Short, whole contract count, average entry,
current futures price and expiry. Zero contracts disables the row. Prices are
ARS/share; shares per contract come from the shared contract settings. The
October/December defaults are 2026-10-30 and 2026-12-30. Editing a row's expiry
changes only that scenario, leaving the monitor's defaults unchanged.

**Copy simulated futures** imports valid bid/ask midpoints from the monitor's
latest snapshot. It keeps a simulated/synthetic source label and receipt time.
Editing a copied price labels it manual. This action preserves your spot,
position direction, quantity and entry price. Invalid books are not copied.

Choose **Trading days** from 1 to 252, enter the signed **Stock move (%)**, and
click **Calculate**. Decimal commas and points are accepted. Trading sessions
use the XBUE calendar with the existing 2026 corrections. Actual elapsed
calendar days determine financing decay.

**Assumed future TNA (%)** sets the rate used for future repricing. Blank keeps
each future's current implied TNA; zero means a zero-rate assumption. **Use
caucion** copies the monitor's manual rate. Until you edit the assumed rate,
it follows valid caucion changes; a manual override stays independent.

The calculation uses:

- Projected spot = spot × (1 + stock move / 100).
- Projected future = projected spot × (1 + projection TNA / 100 × remaining
  calendar days / day basis).
- P/L from today = signed contracts × shares per contract × (projected future
  − current future).
- P/L since entry = signed contracts × shares per contract × (projected future
  − average entry).

An active position that expires on or before the scenario date is unavailable;
its combined totals and probabilities are suppressed. Input edits clear old
results until recalculation. P/L is gross and theoretical; since-entry P/L is
cumulative economic P/L, not an unpaid broker balance.

## History and probabilities

Choose one, three or five years and fetch PPI daily GGAL history. Both inner
views share that history and the selected trading-day horizon. Changing the
horizon reuses loaded prices. Today's unfinished observation is excluded;
missing sessions are not filled and conflicting observations are excluded.

**Return distribution** shows the probability density function (PDF), historical
frequencies and smoothed estimates for return or final-price ranges. Coverage
and sample counts are shown. At least 30 valid return windows are needed for
probabilities. Overlapping historical windows are not independent forecasts.

**Futures P/L** estimates the chance of meeting the combined scenario P/L from
today and of finishing profitable. The legs share each simulated stock return
and your TNA assumptions. These are estimates at the selected horizon, not
guarantees or probabilities of avoiding a drawdown along the way.

**Cancel fetch**, a replacement request and closing the app cancel reads and
discard late responses. Editing spot during a pending quote preserves manual
input. All position inputs remain in this session; they are not saved on exit.

## Build and checks

```powershell
.\build_desktop.ps1 -Python python
python -m unittest discover -s tests -v
python -m market_making.desktop.app --self-test build/source_gui_check.json
.\dist\GGALDesk.exe --self-test build/exe_gui_check.json
```

The isolated build environment and executable are generated locally and ignored
by Git. The widget check exercises both tabs, demo states, Start/Stop, shared
contract defaults, scenario P/L, distributions and cancellation, without live
broker requests. See [data limitations](DATA_LIMITATIONS.md).

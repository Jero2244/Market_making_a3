# GGAL Desk

Open `dist/GGALDesk.exe`. Python is bundled; a separate Python installation is
not required. The app opens idle. Click **Start live check** to connect using
your existing `.env`, or **Try demo** to see synthetic scenarios without a
connection or credential reads. The **Market making** tab is a placeholder.

The main screen shows GGAL spot bid/ask, October and December 2026 futures
bid/ask, gross implied cash-carry TNA, and its difference from your manual
caucion rate in percentage points. Leave the rate blank to show unknown.
Live refresh accepts 1 second or longer and defaults to 5 seconds; demo
refreshes every 2 seconds. Request time counts toward the selected interval.
Slow cycles never overlap. Provider transport failures or throttling trigger
30/60/120/240/300-second backoff, then the selected interval resumes after a
transport-successful cycle. This is an application setting, not a verified
broker rate allowance. See [refresh findings](REFRESH_PERFORMANCE.md).
Controls are locked while monitoring; stop before changing settings.

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

```powershell
python -m unittest discover -s tests -v
New-Item -ItemType Directory -Force build | Out-Null
python -m market_making.desktop.app --self-test build/source_gui_check.json
```

The packaged executable accepts the same `--self-test OUTPUT_JSON` option.
It creates hidden real Tk widgets, checks all four synthetic states and
Start/Stop, and writes a small success report. No live requests are made.
The check also verifies that live refresh defaults to 5 seconds and accepts
1 second. On 2026-10-09 the full suite passed 249 tests and both the source
and executable passed these widget checks.

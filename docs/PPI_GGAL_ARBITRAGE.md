# GGAL price and carry checker

The desktop and console monitor share the same read-only provider adapters,
book parsing and carry calculations. Spot uses PPI production GGAL shares with
immediate settlement; futures use the simulated REMARKETS books for the shared
configured GGAL contracts. Both sources are identified in the output.

## Ordinary price comparison

```powershell
ppi-readonly watch --live --interval 5
ppi-readonly watch --live --caucion-tna 30 --iterations 2
```

No JSON is needed. The monitor uses `market_making/contracts.py` for symbols,
expiry assumptions and the day basis. With spot ask S, futures bid F and
calendar days to assumed expiry D, gross cash-carry implied TNA is
`(F / S - 1) * basis / D * 100`.

Caucion is a manual borrowing TNA percentage, never a fetched funding rate.
Blank means unknown. The price comparison excludes fees and does not require
position sizes or short-sale inputs. Manual desktop spot uses a single reference
price in place of the PPI spread, with that distinction shown in the output.

The October and December default dates are now shared with Position scenarios:
2026-10-30 and 2026-12-30, end of day in Buenos Aires. They are user assumptions.
The previous last-weekday-of-month rule has been retired. Edit the shared
settings and rebuild the executable when rolling contracts.

## Optional full assessment

Copy `config/ppi_ggal_watch.example.json` to a local working file and replace
every placeholder with reviewed instrument details, aware maturity timestamps,
units, explicit borrowing/lending rates and all-in per-share costs. The template
is deliberately incomplete. Its dates and units override the shared defaults
for that assessment, so review any difference deliberately.

```powershell
ppi-readonly watch --live --watch-config config/ppi_ggal_watch.local.json --iterations 2
```

The assessment compares both cash carry and reverse carry using their respective
bid/ask sides. It reports theoretical prices, signed differences, net theoretical
edges and quoted capacity. It does not place orders. Rates and costs are static
inputs; funding rollover and simultaneous fillability are not established.

Strict mode requires usable source times as well as valid books and identities.
REMARKETS book timestamp scope remains unresolved. For a deliberate manual
comparison, add `--manual-check`: timestamp and last-trade diagnostics become
visible caveats, while malformed, wrong-identity and crossed/locked books still
block the affected comparison. Receipt times never substitute for source times.

## Offline preview and optional utilities

```powershell
ppi-readonly watch --demo --interval 0 --iterations 4 --json
ppi-readonly credentials-status
ppi-readonly discover --live
ppi-readonly assess --spot 100 --tna 30 --days 20 --basis 365
```

Demo cycles through no-edge, cash-carry, reverse-carry and unavailable synthetic
inputs. It does not read credentials or contact providers. `credentials-status`
prints statuses, not credential values. Discovery is an optional bounded data
utility; instrument names alone do not verify contract semantics. `assess` is a
hypothetical calculation, with live discovery only if explicitly requested.

## Output and connection behavior

`--json` emits structured records; `--verbose` adds full books, timing and
calculation diagnostics. `--env-file` selects a credentials file. Without
`--iterations`, monitoring continues until Ctrl+C. Live polling defaults to five
seconds and accepts one second or longer. Authentication is reused and provider
errors trigger backoff; see [refresh behavior](REFRESH_PERFORMANCE.md).

Empty books are reported as unavailable, not inferred market closure. Zero trade
volume is distinct from available quoted depth. The data and probability
limitations are summarized in [data limitations](DATA_LIMITATIONS.md); original
investigations and protocol sources remain in the preserved archive.

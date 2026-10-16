# Interpreting the helper

- **Mixed sources:** PPI spot reads are production data; REMARKETS futures are
  simulated. The Real vs reMarkets tab separately reads real futures from A3's
  anonymous viewer feed; the carry checker retains its original sources.
- **A3 viewer protocol:** the public viewer's current quote messages are decoded
  directly. This is a website feed, not a contracted public API; format changes
  can stop the comparison. Exact symbols, currency, expiries and contract units
  are validated before comparisons.
- **Competition differences:** buy/sell highlights compare a simulated quote
  with a real futures reference. They do not establish an offsetting executable
  trade or the competition's official valuation. A3 refresh snapshots can carry
  end-of-session prices outside market hours. Receipt ages are local observations,
  not exchange latency or proof of source freshness.
- **Position sources:** manual simulation uses your contract counts and prices.
  The optional reMarkets import reads a simulated account and includes only
  outright GGAL futures in P/L. Cash, collateral and margin cover all instruments
  in that account. Imports are explicit snapshots, not continuously updated holdings.
  Average entries are the API's basis, which daily settlement may reset; account
  market prices are risk valuations with separately displayed calculation times.
- **Quote quality:** missing, malformed, duplicate, wrong-identity and
  crossed/locked books remain unavailable. Source time and local receipt time
  are kept separate. An HTTP response does not certify exchange freshness.
- **Timestamp scope:** REMARKETS last-trade dates and numeric timestamps do not
  establish the timestamp of the bid/ask book. Clock and session evidence from
  the original investigations remains historical. Strict full assessment stays
  unavailable when required evidence is missing; ordinary/manual comparisons
  display the limitation explicitly.
- **Contract assumptions:** shared dates, end-of-day time, multiplier and rate
  basis are user assumptions. Scenario expiry edits and full-assessment files
  are explicit overrides. Imported GGAL expiry dates and contract units are checked
  against reMarkets instrument metadata. Expired monitor contracts require updated shared settings.
- **P/L costs:** projections include only the closing cost per contract you
  enter (0 = gross). Taxes, margin financing and cashflow adjustments are
  excluded. Since-entry P/L may include differences already settled by the broker.
- **Historical estimates:** probabilities depend on the historical sample,
  horizon, return model and TNA assumptions. Missing sessions are not filled,
  today's unfinished observation is excluded, and sample coverage is shown.
  Bootstrap ranges quantify sampling noise from overlapping observations, not
  regime changes or model error. Drift and EWMA adjustments are modelling
  choices, not forecasts.
- **Rate risk:** no GGAL futures history is read, so Monte Carlo TNA volatility
  and stock/TNA correlation are user assumptions, and shocks move all legs'
  rates in parallel (no term-structure twists). Margin-call estimates compare
  daily mark-to-market loss with a fixed buffer; they ignore changing margin
  requirements and intraday moves.
- **Corporate actions:** the jump flag only catches extreme daily moves. PPI's
  dividend/split adjustment remains unverified.

Book labels and diagnostics remain in the app and exported snapshots. The old
bot readiness and replay procedures are archived; they are not part of routine
manual use. Detailed historical timestamp investigations are preserved in
`archive/market_making/docs/` and local evidence in its ignored `data/` directory.

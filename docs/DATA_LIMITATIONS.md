# Interpreting the helper

- **Mixed sources:** PPI spot reads are production data; REMARKETS futures are
  simulated. The current app does not connect to a production futures feed.
- **Manual positions:** contract counts, entry prices and current prices are
  supplied by you. Broker positions, cash, collateral and margin are not fetched.
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
  are explicit overrides. Expired contracts require updated shared settings.
- **Gross P/L:** projections exclude commissions, bid/ask execution costs,
  taxes, margin financing and cashflow adjustments. Since-entry P/L may include
  differences already settled by the broker.
- **Historical estimates:** probabilities depend on the historical sample,
  horizon and TNA assumptions. Missing sessions are not filled, today's
  unfinished observation is excluded, and sample coverage is shown. Overlapping
  observations and changing market conditions limit interpretation.

Book labels and diagnostics remain in the app and exported snapshots. The old
bot readiness and replay procedures are archived; they are not part of routine
manual use. Detailed historical timestamp investigations are preserved in
`archive/market_making/docs/` and local evidence in its ignored `data/` directory.

# Remarkets book timestamp investigation — 2026-10-13

## Result

**Implementation follow-up:** the receiver now supports `--clock-check`, which
measures an estimated UTC offset against Cloudflare and Windows time servers and
anchors the result to monotonic time. A fresh 20-second RFX20 run at about
11:33–11:34 Buenos Aires time recorded four Md messages. The measured local
offset was +1.53581 seconds; their corrected candidate message ages were
17.7–20.4 ms. Estimated clock uncertainty was about 77–78 ms, so these are
estimates, not precise exchange latency measurements. The source-age intervals
overlapped zero; negative ages are never silently clamped.

The new path rejects malformed/unsynchronized time-server replies, excessive
round-trip uncertainty, disagreeing sources and changes in local wall time. Its
300-second calibration expires and its uncertainty grows using an explicitly
assumed 100 ppm local drift allowance. NTP is unauthenticated; no provider clock
guarantee or whole-book timestamp authority is inferred from this calibration.
These diagnostics do not enable order sending.

Long recordings renew the calibration in a background worker at 75% of its
lifetime, away from the WebSocket receive loop. A failed renewal leaves the
existing estimate valid only until its original expiry, then corrected age
becomes unavailable. Failed renewals retry after 30 seconds. Shutdown signals
the worker and bounds the join to five seconds; slow OS DNS resolution can
outlive that join in the daemon worker. An old estimate is never extended by
changing its issue time.

```powershell
.\.venv\Scripts\python.exe -m market_making.market_data.stream_market_data --symbol "RFX20/OCT26" --output data/new_clock_checked_stream.jsonl --duration 20 --clock-check
```

Calibration adds two public time-server requests. Without `--clock-check` no
NTP requests are made. Raw server values and local receipt timestamps remain
unchanged; corrected estimates are separate `timestamp_diagnostics` fields.
Evidence: `data/timestamp_calibrated_rfx20_20261013.jsonl`.

A further official source was found:
[Primary's .NET MarketData model](https://github.com/matbarofex/Primary.Net/blob/master/Primary/Data/MarketData.cs)
explicitly describes its JSON `timestamp` property as server time in epoch
format. Its [Date helper](https://github.com/matbarofex/Primary.Net/blob/master/Primary/Data/Date.cs)
converts epoch dates using UTC 1970-01-01 plus milliseconds. This establishes
the SDK's epoch convention and supports the observed interpretation, but the
MarketData property is retained as a raw long; the helper is not explicitly
called on that property. The client does not specify matching-engine event
scope, server clock error, or unchanged-side coverage. Those remain unresolved.
Exact downloaded source is preserved in
`data/timestamp_research_20261013/official_dotnet_timestamp_sources.json`.

Clock calculation basis: [RFC 5905](https://www.rfc-editor.org/rfc/rfc5905),
offset/round-trip equations and root delay/dispersion; independent sample
agreement and expiry are application checks. Source selection does not reduce
the best source's estimated uncertainty merely because intervals overlap.

The best existing candidate is the **top-level `timestamp` in WebSocket `Md`
messages**. A fresh, read-only RFX20/OCT26 capture returned three two-sided books
with that field. Public documentation does not specify enough of its semantics
to label it authoritative book time yet. No timestamp gate or authority flag was
changed by this investigation.

There is also a concrete local clock discrepancy: six read-only SNTP measurements
against two public time services estimate this machine is about **1.53 seconds
behind** their clocks. This explains the apparent future timestamps under an
epoch-millisecond interpretation. The system clock was not changed.

## Fresh live evidence

Read-only capture: around 11:13 Buenos Aires time, 20 seconds, exact ROFX
RFX20/OCT26, entries BI/OF/LA/TV, depth 2. All three messages had both BI and OF;
BI price/quantity changed, while OF stayed unchanged and was included again.

| Raw Md.timestamp | Receipt UTC | If interpreted as epoch milliseconds | Receipt minus candidate |
| --- | --- | --- | --- |
| 1791900801998 | 14:13:20.483759 | 14:13:21.998 | -1.514241 seconds |
| 1791900804874 | 14:13:23.360407 | 14:13:24.874 | -1.513593 seconds |
| 1791900812360 | 14:13:30.846087 | 14:13:32.360 | -1.513913 seconds |

These date conversions are explicitly **a diagnostic hypothesis**. The numeric
format and correspondence to current time are strong clues, not a provider
specification of units or event scope. Increasing timestamps do not establish
sequence/gap guarantees or verify the unchanged offer's recertification.

Raw sanitized evidence: `data/timestamp_rfx20_20261013_1413.jsonl`.

## Clock measurements

Around 11:18 Buenos Aires time, a read-only SNTP probe made three requests to
each of `time.windows.com` and `time.cloudflare.com`. It validated packet length,
echoed originate timestamp, server mode, leap indicator and stratum. It did not
adjust Windows time or change the bot's clock policy.

| Time source | Estimated source minus local clock | Network round trip |
| --- | --- | --- |
| time.windows.com | +1.5283 to +1.5316 seconds | 166.5 to 169.1 ms |
| time.cloudflare.com | +1.5327 to +1.5328 seconds | 4.9 to 5.8 ms |

The agreement supports the inference that the local clock is slow. The samples
were taken several minutes after the market-data capture, use unauthenticated
NTP, and do not establish Primary's own clock error or a permanent uncertainty
bound. They must not become a hard-coded offset or authority attestation.
`w32tm /query /status` was access denied; Windows synchronization state was not
established. Probe script and results are in `data/timestamp_research_20261013/`.

## Official sources checked

1. [Primary API reference](https://apihub.primary.com.ar/assets/docs/Primary-API.pdf),
   revision 1.21: REST BI/OF examples have no whole-book timestamp; the dictionary
   on printed page 55 defines `timestamp` generically as a particular record's
   time. It does not establish the WebSocket field's units, clock, event or
   bid/offer coverage. `LA.date` and historical `serverTime` concern other records.
2. [Primary interactive API reference](https://apihub.primary.com.ar/assets/apidoc/trading/index.html):
   its linked `api_project.js` describes REST version 1.9.0, generated in 2020.
   The linked `api_data.js` omits a book timestamp from the REST example and
   supplies `servertime` only in historical-trade examples. Saved original files
   are in `data/timestamp_research_20261013/`.
3. [Primary consumption guidance](https://apihub.primary.com.ar/assets/docs/TRD-Buenas_practicas.pdf),
   printed page 3: recommends WebSocket subscriptions for real-time market data.
   It identifies MPI support at `mpi@primary.com.ar` on printed page 5.
4. [Primary FIX specification](https://apihub.primary.com.ar/assets/docs/PrimaryAPI-FIX.pdf),
   older V2.0.20, printed pages 62–63: Market Data Snapshot/Full Refresh `W` has
   optional `MDEntryDate` (272), UTC YYYYMMDD, and `MDEntryTime` (273), UTC
   HH:MM:SS.sss. This is a more explicit timestamp contract than REST/WS, but
   does not guarantee the fields are supplied for both bid and offer entries in
   today's Remarkets service. FIX access and current applicability must be
   confirmed; a protocol switch by itself is not proof of freshness.
5. The [current API hub](https://apihub.primary.com.ar/) links an A3 FIX V2.0.53
   PDF. During this investigation that URL redirected to the A3 homepage, so
   the current full PDF was not verified. Search-index excerpts are not used
   as implementation authority.

## Practical path to an authoritative timestamp

Use the existing WebSocket field if Primary confirms its event and clock
semantics for the **Remarkets execution venue**. This is not a requirement that
simulated execution books have real A3 exchange-origin timestamps. Real-market
reference/valuation feeds are separate inputs and need their own timestamps.

Obtain the following narrow clarification from Primary, then pair that
specification with measured, synchronized local time:

> For Remarkets ROFX RFX20/OCT26, WebSocket Md messages contain a top-level
> timestamp, e.g. 1791900801998, alongside BI and OF. Please confirm:
> (1) whether it is milliseconds since the Unix epoch in UTC;
> (2) whether it timestamps a Remarkets matching-engine/book event, gateway
> observation, or message generation, and which component's clock is used;
> (3) whether the BI and OF arrays in each message are complete current depth
> snapshots, including unchanged sides, and what omission/empty means;
> (4) the clock synchronization accuracy and any known cache/forwarding delay.
> If this field does not support current-book age, is there a supported
> timestamped snapshot or FIX feed available in Remarkets? Are FIX tags 272/273
> supplied for bids and offers? Please provide the current specification.

This request is prepared locally and has not been sent. With a documented
book-event timestamp, implement that exact mapping and its uncertainty budget.
With a documented message-generation timestamp only, use it for message-age
health; do not relabel it as the last quote-change time. If neither is available,
retain the bounded snapshot acquisition/receipt policy and unknown source age.

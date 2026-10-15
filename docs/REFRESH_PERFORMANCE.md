# Refresh and connection behavior

Live monitoring defaults to five seconds and accepts intervals of at least one
second. Demo refreshes every two seconds. Request duration counts toward the
chosen interval; slow cycles do not overlap. This setting is not a measured or
verified broker rate allowance.

The recurring monitor keeps authenticated HTTP sessions until Stop. PPI renewal
uses the returned expiry and refresh token when available. The REMARKETS adapter
uses its existing 24-hour token-lifetime assumption with a renewal margin. Each
provider permits one renewal/retry after a quote HTTP 401; repeated authentication
failure stops the run. Independent providers are read concurrently, and each
provider's requests are sequential.

Transient failures and throttling trigger 30/60/120/240/300-second backoff. The
normal interval resumes after a successful acquisition cycle. Failed reads
publish unavailable prices, with diagnostics retained. Source and receipt ages
stay separate; receiving data faster does not establish source freshness.

Stop cancels active sockets and waiting. A restart waits for worker cleanup;
late results are discarded. Position spot/history requests are separately
cancellable, bounded one-shot reads. Recomputing a horizon uses already loaded
history without another provider request.

The carry checker polls REST data. The Real vs reMarkets tab reads the public A3
WebSocket and refreshes its two contract subscriptions every five seconds.
Its reMarkets reader independently requests both futures every one second;
request duration counts toward that interval and slow cycles do not overlap.
Source failures clear that source immediately and use bounded 1–30-second
reconnection backoff. Unknown exchange timestamp semantics remain advisory for
this manual comparison; they are never relabeled as verified freshness.

The desktop checks for newly received data every 100 milliseconds. The new tab
retains a bounded local sample of receipt-to-display delays for diagnostics.
This metric excludes upstream exchange and viewer delays. Open-market accuracy
and timing still require a side-by-side check during a trading session.

An 18-second after-hours check on 2026-10-14 received 41 observations across
both contracts and sources. Local receipt-to-display delay had a 54 ms median
and 107 ms maximum; individual reMarkets quote requests had a 73 ms median
and 441 ms maximum. Both sources returned usable book structures and Stop
cleared highlights and finished cleanup. These small-sample, after-hours
measurements do not establish market-hours performance or exchange latency.

The original refresh
investigation and cited provider documentation are preserved in
`archive/market_making/docs/REFRESH_PERFORMANCE.md`. The current cleanup and
verification use offline checks, not a credentialed refresh benchmark.

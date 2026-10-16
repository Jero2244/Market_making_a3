# Refresh and connection behavior

## Shared reMarkets stream (2026-10-15)

The desktop carry monitor and the Real vs reMarkets tab share one reMarkets
WebSocket (`ppi/remarkets_stream.py`) per set of Primary credentials: one
login, one `smd` subscription covering every symbol either screen needs. Each
`Md` frame replaces the BI/OF/LA entries it carries. A ping every two seconds
confirms the connection; because frames and pongs share one ordered
connection, an unchanged book is treated as current as of the latest pong.
That is receipt confirmation, not exchange freshness.

Without a frame or pong for five seconds the stream is unhealthy: its books
are dropped, both screens fall back to the REST polling described below, and
they return to the stream once it reconnects (1–30-second backoff, fresh login
every 23 hours). With manual spot, the carry monitor recalculates on each
futures change, at most five times per second; with PPI spot it polls spot at
the chosen interval (default two seconds) and reads futures from the stream.

reMarkets answered HTTP 401 to a login 0.5 seconds after another and accepted
one 3 seconds later (2026-10-15). All logins in the process are therefore spaced
at least two seconds apart, so concurrent screens, restarts and account
refreshes are not reported as bad credentials.

After hours on 2026-10-15, the stream delivered all four books about 50 ms
after subscribing and pongs returned in about 70 ms. Update behavior during
trading still needs a market-hours check.

## REST polling (fallback and console)

Live monitoring polls at the chosen interval and accepts intervals of at least one
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
WebSocket and refreshes its four contract subscriptions every five seconds:
`GGAL/OCT26`, `GGAL/DIC26`, `RFX20/OCT26` and `WTI/NOV26`.
Its reMarkets reader independently requests all four futures every one second;
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

A read-only feed check on 2026-10-15 received valid bid/ask structures for all
four contracts from both A3 and reMarkets. This confirms instrument access and
decoding; it does not validate exchange freshness or measure timing.

The original refresh
investigation and cited provider documentation are preserved in
`archive/market_making/docs/REFRESH_PERFORMANCE.md`. The current cleanup and
verification use offline checks, not a credentialed refresh benchmark.

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

The app currently polls REST data. Streaming subscriptions, delta reconstruction
and real futures data are separate potential enhancements. The original refresh
investigation and cited provider documentation are preserved in
`archive/market_making/docs/REFRESH_PERFORMANCE.md`. The current cleanup and
verification use offline checks, not a credentialed refresh benchmark.

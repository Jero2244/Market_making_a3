# GGAL refresh investigation — 2026-10-09

Neither provider requires a login before every price read. The prior recurring
checker constructed and closed both clients on every cycle, giving five
requests per cycle: two logins, one spot book and two futures books. Its
30-second floor was an application choice, not a verified broker rule.

## Provider evidence

- [Primary API documentation](https://apihub.primary.com.ar/assets/docs/Primary-API.pdf),
  authentication section, documents a **24-hour** token lifetime. The app
  retains that token and renews one minute before the documented lifetime.
  HTTP 401 can indicate earlier invalidation and permits one renewal/retry.
- [PPI REST documentation](https://itatppi.github.io/ppi-official-api-docs/api/documentacionRest/#account)
  returns `expirationDate`, `accessToken` and `refreshToken`, and documents POST
  `Account/RefreshToken`. The app uses the actual returned expiration with a
  30-second renewal margin; it does not assume a fixed PPI token lifetime.
  A response without a refresh token can reuse the access token and log in
  again only when needed. Refresh rejection stops; it does not cause a login loop.
- [Primary's streaming market-data section](https://apihub.primary.com.ar/assets/docs/Primary-API.pdf)
  documents subscriptions to instrument updates as prices change.
- [PPI's Python streaming examples](https://itatppi.github.io/ppi-official-api-docs/api/documentacionPython/)
  demonstrate instrument subscriptions and separate book/trade events.

The inspected documents do not establish a numerical rate allowance for this
user's PPI/REMARKETS accounts. The application's 1-second minimum is therefore
not a claim that every account can sustain that rate.

## Implemented improvements

The GUI and recurring CLI share persistent, in-memory sessions. After initial
authentication, a normal cycle requires **three price requests**. HTTP pooling
also avoids opening a new TCP/TLS connection for every cycle when the server
keeps the connection alive. Tokens are not written to disk or reports.

PPI spot reads run alongside the Primary futures reads. The two futures reads
remain sequential so a `requests.Session` and its deadline/cancellation state
are never used concurrently. All books still come from the current cycle;
book age is rechecked at completion. No old price fills a failed request.

Each cycle has an explicit bounded request budget (PPI: four; Primary: five,
including any renewal/recovery), the existing elapsed deadlines, TLS
verification, response-size limits and endpoint restrictions. Each provider
can renew/retry a rejected quote token at most once per cycle. Forbidden access,
bad authentication, repeated rejection and unknown errors stop polling.

Both interfaces default to **5 seconds** and permit **1 second or longer**.
Successful acquisition time counts toward that target interval; a slower
cycle finishes before the next starts. Transient provider failures back off
30, 60, 120, 240, then 300 seconds; transport success resets the backoff.
A futures HTTP 429 prevents the other futures request that cycle. Valid but
empty/unchanged books do not trigger aggressive retries or authentication.

The GUI reports the last acquisition duration so actual latency can be measured
on the user's connection. Stop/close interrupts the wait and cancels active
transport sockets, then discards late responses and closes the sessions.

## Further options

Streaming is the next substantial latency improvement: subscribe once and
recalculate when a spot or futures book changes. This would remove recurring
REST polling from the normal path. The current executable still uses REST.

Before wiring streaming into this checker, verify GGAL `INMEDIATA` access,
the exact futures subscriptions, full-book versus delta behavior, trade-only
events, reconnect/resubscription, token renewal, source timestamps and missing
data rules. A hybrid (stream futures, poll spot) is possible if only one feed
is validated first. Receiving events faster still does not establish exchange
freshness, simultaneous fillability, or executable arbitrage.

This investigation and its tests used public documentation, synthetic transports
and local test servers. No credentialed live benchmark was run, so no millisecond
speed or sustainable live polling rate is claimed.

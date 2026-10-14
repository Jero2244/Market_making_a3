# Market-data receiver repairs — 2026-10-13

The first competition data path is a complete REST snapshot receiver for read-only
and shadow consumers. It is available as `snapshot-market-data` or the module below.
The order harness and desktop market-making placeholder are not connected to it.

```powershell
.\.venv\Scripts\python.exe -m market_making.market_data.snapshot_feed --live --symbol "RFX20/OCT26" --iterations 5 --interval 5 --output data/new_snapshot_session.jsonl
```

Use a new output filename. The exact catalog future, detail identity,
`minPriceIncrement` and `roundLot` are required. Other catalogued futures are
supported; variable tick schedules require another adapter. Defaults: 2 levels,
5-second maximum acquisition age, 2-second maximum request duration, 5 reads.
Use `--max-request 5` when measuring a slower connection; this changes the
acquisition budget, not the unknown exchange quote age.

## Validity contract

Each REST response replaces the whole observation. Both BI and OF must be lists;
missing or null sides are malformed, while explicit empty lists mean no quoted
depth. Levels are sorted and positive, finite, on-grid and unique by price.
Crossed/locked, one-sided and empty books are not usable for shadow decisions.
Present wrong identity is rejected. Identity-free responses are bound to the exact
catalog/detail-verified request; no symbol substitution occurs.

`SnapshotFeed.snapshot()` evaluates age at consumption time using a monotonic
clock, including the request duration in the age budget. Refresh begins by
discarding old depth. An exception, malformed reply or over-budget acquisition
discards the book and disconnects the feed. Recovery requires a new complete
snapshot and increments the connection generation. Sequence numbers identify
accepted acquisitions within the process; they are not exchange sequences.

`usable_for_shadow=true` means the response passed these checks. It does not
prove exchange freshness or authorize orders. `exchange_quote_age_seconds`
remains null. Account capacity, market sessions, real references and execution
integration remain separate work.

The [official Primary API reference](https://apihub.primary.com.ar/assets/docs/Primary-API.pdf)
documents REST depth requests and shows BI/OF without whole-book timestamps
(Market Data, pp. 37–40). Its WebSocket examples and descriptions do not establish
enough ordering/deletion guarantees for this implementation to merge partial
messages into a trading book. The snapshot baseline avoids that dependency and
does not promise streaming latency.

Requests use connect/read timeouts and the existing socket deadline watchdog to
interrupt active sockets at the acquisition limit, including slow body reads.
Returned acquisitions are also rejected if elapsed time exceeds the budget.
DNS resolution and operating-system scheduling are not guaranteed hard deadlines.
This CLI stops on refresh failure; it does not retry authentication indefinitely.

## Streaming recorder repairs

- Any exact, uniquely catalogued future is accepted, instead of only RFX20.
- Invalid UTF-8, duplicate JSON keys and nonfinite JSON constants are withheld.
- Invalid/control text does not refresh market-data receipt health; pongs remain
  transport evidence only. Selected-instrument and side ages remain independent.
- Receipt time is captured before durable storage; messages include monotonic
  receipt time, local sequence and connection generation.
- The end record includes accumulated durable-write time and maximum write time.
  Recording remains synchronous; no unmeasured asynchronous queue is introduced.
- The older RFX snapshot parser now rejects duplicate price levels too.

## Read-only live check

On 2026-10-13 around 09:35–09:36 Buenos Aires time, three snapshots each for
`ROFX RFX20/OCT26` and `ROFX GGAL/OCT26` authenticated and returned explicit
empty BI/OF. Request times were approximately 0.067–0.299 seconds for RFX20
and 0.049–0.053 seconds for GGAL. All six observations correctly reported
`empty`, with shadow usability false. This is connectivity/empty-book evidence,
not active-session liquidity, exchange freshness or trading readiness evidence.
Local ignored evidence: `data/receiver_rfx20_20261013.jsonl` and
`data/receiver_ggal_20261013.jsonl`.

A ten-second read-only GGAL WebSocket check also received its initial Md with
empty BI/OF, LA and TV, and stayed connected through pongs. Instrument/side
ages became stale even while transport was alive. Saved evidence:
`data/receiver_stream_ggal_20261013.jsonl`. No orders were sent.

Use the project `.venv` for tests; the old desktop build environment points to a
missing interpreter, and the system Python lacks the project dependencies.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
```

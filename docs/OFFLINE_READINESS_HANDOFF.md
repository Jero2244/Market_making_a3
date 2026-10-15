# Offline hardening handoff — 2026-10-05

**BLOCKED. Research suite + deliberately blocked single-order demo harness,
not a production market maker. Independent review follows; this is not approval.**
No broker requests, credential file reads, orders, cancels or send-enabling changes
were performed during this work. Existing timestamp send gate is unchanged and
unconditional. Existing October-only, BUY LIMIT DAY, quantity-one, price-grid,
immediate exact-ID cancellation and uncertain-submit lock restrictions remain.

## Measured local commands

PowerShell, repository root, Python with `PYTHONPATH=src`:

| Command | Observed result |
| --- | --- |
| `python -m unittest discover -s tests -q` before changes | 200 tests, OK, 11.477s |
| `python tests/run_offline.py` after review-cycle fixes | 225 tests, OK, 11.667s |
| `python -m compileall -q src tests` | Exit 0 |
| `python -m market_making.validation.offline_readiness` | JSON BLOCKED, 10 blockers, exit 2 |
| `git diff --check` | Exit 0; Windows line-ending warnings only |

The strengthened test runner forbids actual HTTP requests, socket connections,
and `.env` text reads. Fixtures use synthetic identifiers and injected clients.
It also tests an isolated child process exiting while holding a local OS lease;
the guard file remains and the OS releases the lease. CI has been added for
Windows/Linux and Python 3.10/3.12, but remote CI has **not** been run here.

## Completed local controls

- `offline-readiness` is the authoritative **repository inventory**, not a broker
  readiness probe. It defaults to no environment loading, network or filesystem
  state, separates implemented controls, missing code, missing broker evidence
  and operational approval, and never reports READY.
- `check-connection` now also requires `--live` before credentials, prompts or
  network. Old network examples without `--live` no longer execute requests.
  Other explicitly credentialed capture modes remain opt-in; none were executed.
- REST clients share a fixed HTTPS demo host, verified TLS, no redirects, no
  ambient proxy/netrc routing, empty proxy configuration, bounded connect/read
  waits, and zero adapter retries. Read and mutation paths are separate allowlists.
  New/cancel mutations remain reachable only through the existing gated harness.
- REST bodies stream into a 2 MiB bound. Review cycle 1 found that checking elapsed
  time only after a 64 KiB application chunk yielded did **not** bound slow-drip
  buffering; the former claim of a 10-second blocking bound was incorrect.
  The revised per-request HTTPS adapter installs an absolute 30-second deadline
  before acquisition and enforces remaining time at **each raw socket read below
  HTTP buffering**, covering status/header lines and body reads. Timed-out reads
  shut down/close the socket; HTTP/response cleanup closes the reader. Headers and
  bodies share the same deadline, with no new body budget. Socket-free regressions
  exercise real requests/urllib3/http.client buffering with a byte every 9 seconds:
  header and body reads terminate at simulated second 30, not second 135.
  This is not a hard real-time scheduling guarantee: OS DNS resolution before
  socket acquisition remains outside Python socket timeout control, and TCP/TLS
  setup uses the existing connect timeout and pre/post deadline checks. Remote
  transport behavior has not been tested or authorized here.
  JSON rejects duplicate keys, nonfinite numeric
  literals, excess depth/nodes and excessive decimal exponents. Fractional JSON
  numbers decode as exact `Decimal`; artifact serialization writes these as
  decimal **strings**, not approximate floats. These are normalized artifacts,
  not byte-for-byte wire evidence. Secret echoes/keys are withheld, not partly
  printed or retained. Responses close on body/HTTP failures.
- Newly created demo intents carry schema v1 and explicit demo host, market,
  account hash and evidence-directory context. Recovery validates version,
  context, phase, exact intent and both returned IDs before cancellation. Legacy,
  malformed, unknown-submit or wrong-context journals remain blocked and intact.
- Cooperating CLI send/recovery processes hold a per-evidence-directory OS lease.
  Guard files are never deleted/truncated. Lock release checks unchanged ownership
  evidence and still requires durable no-send or existing terminal proof. Atomic
  update failures preserve old journals and `.tmp` evidence, which blocks further
  updates. Invalid serialization happens before opening an evidence file.
- Quantity conservation now uses an explicit precision-128 Decimal context,
  independent of caller precision, rounding, exponent limits and traps. Bounded
  input strings/adjusted exponents need at most 102 sum digits, including carry.
  Tests cover valid and invalid quantities beyond 28 digits under altered contexts.
- A separate offline normalized execution reducer handles duplicates, reordering,
  sequence gaps/conflicts, cumulative partial fills, rejected states, cancel/fill
  races and terminal regressions without subtracting observed exposure. Fixture
  ledger reloads replay events, require restart reconciliation, and fail closed
  on interrupted writes/concurrent cooperating writers. Reconnect never clears
  uncertainty. This is **not** connected to Primary execution reports or sending;
  `order_reports` remains an observational, non-authoritative tool.
- Separate offline envelope contracts cover active orders, history, positions,
  balances and margin under a common capture context. Completeness assertions
  alone never establish broker authority. Only existing documented demo order
  read paths (`id`, `actives`, `all`) are listed. No portfolio endpoint was guessed,
  and reconciliation never cancels any order, related or unrelated.

## Deferred / external blockers (not solved by passing fixtures)

1. **Freshness:** written Primary timestamp/BI+OF coverage, update/sequence scope,
   atomicity and reconnect semantics plus defensible independently referenced
   clock uncertainty. See [FRESHNESS_SPEC_REQUEST.md](FRESHNESS_SPEC_REQUEST.md).
   No timestamp mapping, clock bound or updated placeholder replay date invented.
2. **Broker execution authority:** verified report IDs, cumulative fills, event
   ordering, conflicts, snapshot/history completeness and reconnect reconciliation
   contracts. Offline event sequences are synthetic contract inputs, not a claim
   about Primary fields. No automatic recovery of unknown submit outcomes.
3. **Account reconciliation:** positions/balances/margin API specs, units, coverage,
   pagination, valuation and completeness evidence; broker-bound adapters absent.
   Current order reads/history-seeded monitoring cannot establish full readiness.
4. **Coordination/storage:** account-wide shared coordinator across hosts/tools/UI;
   production append-only audit/ledger, fencing, tamper resistance, filesystem
   directory durability/power-loss guarantees and controlled recovery procedures.
   Local cooperating leases do not prevent uncooperative writers, symlink races,
   network-filesystem lock weaknesses or external changes between ownership check
   and unlink. No stale-lock deletion or `.tmp` cleanup is automated.
5. **Risk/proposals:** portfolio metadata, multipliers/FX, authoritative account
   exposure, margin/loss measurement and independently approved limits. A risk
   proposal interface was deferred rather than inventing metadata or a fake
   approval signal; no risk engine or quoting engine exists.
6. **Production/operations:** no production endpoints, production adapter or
   approval; no reviewed account authority/operational safety approval. Offline
   ledger/envelopes and fixture tests cannot supply these requirements.

These limitations are intentionally machine-readable blockers. Review changes
independently before any subsequent authorized read-only capture. Nothing here
authorizes a capture, demo send, cancellation or production deployment.

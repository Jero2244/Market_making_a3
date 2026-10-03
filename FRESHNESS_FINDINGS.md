# Quote freshness investigation (2026-10-02; offline follow-up 2026-10-03)

## Current status — BLOCKED, independent review pending

The offline follow-up did not establish any new authoritative Primary mapping or
clock bound. It did not enable an adapter, alter runtime source, load credentials,
or perform broker capture, submission or cancellation. The unconditional
`timestamp_gate` remains unchanged. Specification/provenance, clock method,
conditional October capture and deferred adapter acceptance requirements are in
[FRESHNESS_SPEC_REQUEST.md](FRESHNESS_SPEC_REQUEST.md). This supersedes historical
instructions treating a timestamp-verification boolean or parseable timestamp as
authority. No offline result implies freshness readiness or permission to send.

Follow-up artifacts: `data/freshness_followup_20261003T171340082973Z/`.
Baseline source/test/doc backups and SHA-256 manifest were made before edits;
all 141 existing data files were hashed separately without rewriting them.
Lock/journal inventory found zero candidates; none were deleted or reconciled.
Baseline unittest discovery: 114 tests, exit 0. Follow-up validation results are
recorded below and in the artifact command logs/manifest.

Only three justified offline regression tests were added: cumulative nonzero
age/elapsed/uncertainty budget, either missing/empty side despite synthetic
authority, and the unchanged hard timestamp block despite synthetic authority
and review booleans. Expiring clock-bound logic and WS reconstruction remain
deferred pending specification, not silently implemented or claimed tested.

### Follow-up validation actually run (offline)

- Final `python -m unittest discover -s tests -v`: 117 tests, exit 0
  (5.214 seconds; baseline 114 tests, 6.413 seconds).
- Default `python smoke_demo_order.py` and `python view_order_book.py`:
  both NOT_RUN, exit 2. Their default paths return before dotenv/credential
  loading or networking; no live/preflight/send/cancel flag was used.
- `python -m py_compile` for all 11 root and 5 test Python files: exit 0.
- Historical integrity comparison: 141 files, zero changed/missing/new outside
  the exclusive follow-up directory. Runtime source hashes remain unchanged.

`validation_results.json` records exact commands, Python version, UTC start/end,
exits and log paths; `run_offline_validation.py` records the offline procedure.
`baseline_manifest.json`, `final_source_manifest.json`, `artifact_manifest.json`
and historical before/after manifests provide SHA-256 review inputs (artifact
manifest excludes itself). Independent review has not been performed here.

## Evidence and provenance

- Official Primary API v1.21 (document revision 2022-12-02), public download:
  https://apihub.primary.com.ar/assets/docs/Primary-API.pdf . Saved unchanged in
  `data/freshness_247_20261002/Primary-API.pdf`; SHA-256 in artifact manifest.
  Pages 38-39 REST example supplies BI/OF price/size with no whole-book timestamp;
  LA/SE/OI/CL dates refer to separate entries, not bid/offer freshness.
  Pages 39-41 describe asynchronous WS updates on change, but their Md example
  does not specify a book timestamp or snapshot/delta merge contract.
  Page 55 defines `timestamp` only as "Marca de tiempo de un registro en particular";
  no explicit units/timezone/exchange-origin/whole-BI-and-OF scope guarantee.
  Page 54 `serverTime` discusses a record, e.g. historical trade; not current book.
- Loaded Primary skill and its local `references/REFERENCE.md`, section 4,
  independently shows entry-level dates and no authoritative book mapping.
  These are supplementary examples, not exchange provenance certification.
- Saved sanitized REST diagnostics:
  `data/demo_acceptance_initial_20261002T2334574668944Z/read_only_preflight/preflight.json`
  and `data/demo_diagnostic_review1_20261002_01/preflight.json` record no server
  snapshot timestamp even though the snapshot was two-sided.
- Saved `data/live_exploratory_20261002_01/stream.jsonl` line 4 contains numeric
  Md `timestamp=1790958890363`, on **December**, not the mandated October contract.
  The saved stream summary calculates a millisecond-assumed receipt difference;
  that historical diagnostic is not authoritative exchange freshness. Numeric
  shape/close correspondence to receipt cannot prove units, clock, origin or
  scope. No WS timestamp adapter or book reconstruction was implemented.
- Primary FAQ https://apihub.primary.com.ar/#faq12 , saved in
  `data/session_sources_20261002_03/source_evidence.json` source `primary_faq`:
  "probar 24x7 los escenarios mas comunes de la API de Trading y de la API Risk".
  The same saved evidence contains A3's production hours and REMARKETS maintenance
  banner caution. Production hours do not restrict this demo's request policy;
  24/7 testing is not a guarantee of uninterrupted service or fresh liquidity.

## Decision and remaining solution

No supported authoritative timestamp mapping was established in these sources.
Real snapshots retain `quote_age_seconds=null`, `timestamp_authoritative=false`.
Timezone-aware ISO parsing yields only `timestamp_derived_age_seconds`; numeric
timestamps remain supplied values, without guessed units. Local receipt age is
monotonic elapsed time, never exchange quote age. HTTP Date, heartbeat, last trade
and local receipt must never be substituted. Boolean review assertions cannot
override the missing mapping. The send path remains BLOCKED, at any hour.

To remove that blocker, obtain Primary's written specification for the precise
current whole-book field (exchange versus gateway timestamp, units, timezone,
which sides/depth it certifies, update/snapshot semantics), collect fresh sanitized
October read-only evidence and establish a bounded local-versus-source clock
uncertainty. Implement that specific adapter, not field-name heuristics, then
independently review exact source hashes and tests. WS may be the appropriate
real-time transport (official page 37), but switching alone is not freshness proof.

Gate mechanics preserve age + elapsed monotonic duration + uncertainty <= 5 s;
negative/future/nonfinite ages, rollback/divergent clocks, unknown uncertainty,
missing/crossed/stale books fail closed. Synthetic test construction does not
constitute supported production provenance or an operator bypass option.

Baseline and changed-file manifests, unittest logs, compilation/default checks
and historical integrity comparison are in `data/freshness_247_20261002/`.
No credentials were read by the implementation investigation; only the public
documentation URL was downloaded. No broker calls or orders were made.

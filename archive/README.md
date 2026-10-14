# Project archive

`market_making/` preserves the original project before the 2026-10-14 cleanup:
source, tests, configuration, build instructions and historical documentation.
The retired execution, validation and separate market-data packages live here.
The active package installer discovers only the top-level `src/` directory.
Archived code and tests are not included in the app or its normal test run.

`market_making/SOURCE_MANIFEST.json` lists the original tracked files and SHA-256
hashes. All files were checked against this manifest during the migration.
The historical research data was moved to `market_making/data/`; it stays
git-ignored, as before. No credentials were copied into this archive.

`generated/2026-10-14/` holds previous executable builds, reports and caches
that could be moved. It is local and git-ignored. Active virtual environments and
locked caches stay in their existing ignored locations.

The original README and plans describe historical behavior. They do not define
the current app's features or authorize network reads or orders. To recover the
old project, copy this historical snapshot into a separate checkout and follow
its installation instructions with credentials supplied separately. Do not add
its source directory to the current app's Python path.

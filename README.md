# Job Harvester

Job Harvester will be a small, local-first CLI that collects public job offers,
normalizes them, stores them in SQLite, and reports newly discovered listings.
It will not require Career-Ops or any hosted service.

> Status: V1 implemented with Greenhouse collection and local SQLite storage.

## First milestone

The first milestone deliberately supports one path: one or more configured
Greenhouse job boards fetched by one CLI command.

```console
cp config.example.toml config.toml
# Set one company's name and Greenhouse board token in config.toml.
job-harvester collect --config config.toml --database jobs.sqlite3
Found 42 jobs; 42 new.
```

Running the same command again keeps the existing rows and should report zero
new jobs when the board has not changed. Network or configuration failures must
produce a non-zero exit status and must not discard previously collected jobs.

## Install and run

Python 3.11 or newer is required.

```console
python3 -m venv .venv
.venv/bin/pip install -e .
cp config.example.toml config.toml
# Edit config.toml with a real company name and Greenhouse board token.
.venv/bin/job-harvester collect --config config.toml --database jobs.sqlite3
```

Run the dependency-free test suite from a source checkout with:

```console
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## V1 structure

```text
job-harvester/
├── pyproject.toml
├── README.md
├── config.example.toml
├── src/job_harvester/
│   ├── cli.py             # argument parsing and run summary
│   ├── config.py          # TOML loading and validation
│   ├── models.py          # normalized Job value object
│   ├── storage.py         # SQLite schema and upserts
│   └── collectors/
│       ├── base.py        # small Collector protocol
│       └── greenhouse.py  # Greenhouse HTTP mapping
└── tests/
    ├── fixtures/          # saved, sanitized API responses
    ├── test_greenhouse.py
    └── test_storage.py
```

The execution flow stays linear:

```text
TOML config -> collector -> normalized Job records -> SQLite -> CLI counts
```

Each collector accepts source-specific configuration and returns normalized
`Job` records. It does not know about SQLite or Career-Ops. The CLI coordinates
collectors and storage. A Lever collector can therefore be added later without
introducing a plugin framework, service layer, or dependency injection system.

## Normalized record and persistence

The initial normalized record contains:

- `source`
- `external_id`
- `company`
- `title`
- `location`
- `remote_status` (nullable/unknown)
- `published_at` (nullable)
- `url`
- `collected_at` (UTC time first discovered)

SQLite enforces `UNIQUE (source, external_id)`. For Greenhouse,
`external_id` is the public job-post `id`, not `internal_job_id`. An upsert may
refresh mutable fields such as title, location, and URL, but it must preserve
the original `collected_at`. Whether an upsert inserted a new key determines
the CLI's `new` count. Rows absent from a later response remain stored so that
an old listing cannot be reported as new if it reappears.

## Configuration boundary

The committed example should contain non-secret source settings only:

```toml
[[sources]]
type = "greenhouse"
company = "Example Company"
board_token = "examplecompany"
```

`config.toml` and SQLite files will be ignored by Git. Future authenticated
sources, such as France Travail, must read credentials from environment
variables; credentials do not belong in TOML.

## Greenhouse assumptions and risks

- A board token is the organization-specific segment used by Greenhouse's job
  board URL/API, not necessarily the company's display name or domain. A wrong,
  renamed, private, or retired token may return an HTTP error or no jobs, so it
  must be validated with a real request.
- Public list requests use
  `GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs` and require
  no authentication. The response is currently a single `jobs` array with a
  total in `meta`; the collector should still use timeouts, check status codes,
  and validate the response shape.
- The list response exposes the unique job-post `id` and may expose an
  `internal_job_id`. The post ID is the correct deduplication key because one
  internal job can have distinct posts and prospect posts can have no internal
  ID.
- The list response provides `updated_at`, not `first_published`. The first
  milestone leaves `published_at` null. Fetching every job detail solely to
  populate it would add latency and failure modes and is not needed to prove the
  collection path.
- Greenhouse has no stable, universal remote-status field in the list response.
  The first milestone records unknown rather than guessing from free-text
  locations. Explicit source metadata can improve this later.
- Company name is supplied by local configuration because it is not part of
  each list item. Duplicate or shared boards are therefore a configuration
  concern; `(source, external_id)` remains the identity key.
- Listings can change or disappear between runs. Upserts retain first discovery
  while refreshing current fields; removal/closure tracking is outside this
  milestone.

## Incremental implementation plan

1. **Project skeleton:** add packaging, console entry point, `.gitignore`, an
   example TOML file, config validation, and CLI help.
2. **Greenhouse normalization:** implement the HTTP client and mapping with a
   saved response fixture and tests for missing optional fields and bad payloads.
3. **SQLite persistence:** create the jobs table and tested idempotent upsert
   behavior that preserves `collected_at` and returns the new-row count.
4. **End-to-end command:** connect config, collector, and storage; print
   `found`/`new`; cover failure exit codes; perform one documented live smoke
   test against a known public board.

Lever, France Travail, URL export, filtering, closed-listing detection, and
Career-Ops integration follow only after this milestone works end to end.

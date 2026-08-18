# Job Harvester

Job Harvester will be a small, local-first CLI that collects public job offers,
normalizes them, stores them in SQLite, and reports newly discovered listings.
It will not require Career-Ops or any hosted service.

> Status: V2.1 implemented with deterministic work-mode normalization and
> remote-aware filtering in addition to the V2 workflow.

## Daily V2 workflow

Configure one or more Greenhouse boards and the relevance filters in
`config.toml`, then run:

```console
job-harvester collect --config config.toml --database jobs.sqlite3
job-harvester list --config config.toml --database jobs.sqlite3 --new-only
job-harvester export --config config.toml --database jobs.sqlite3 --new-only \
  --output new-relevant-jobs.json
```

`collect` fetches and stores every listing. `list` and `export` apply the
configured filters when reading SQLite; irrelevant jobs remain stored and can
become relevant after filter changes. Omit `--new-only` to inspect or export
all currently relevant stored jobs.

The collection states describe the latest successful collection:

- `new`: the identity was first discovered in that collection;
- `updated`: a previously stored job changed title, company, location, remote
  status, publication time, or URL in that collection;
- `seen`: the job existed before and its stored source fields did not change in
  that collection.

Before each successful upsert, previous `new` and `updated` states roll over to
`seen`. That rollover and all job writes happen in one SQLite transaction.
Collection fetches finish before the transaction starts, so a network or source
failure leaves the previous states intact. Consequently, `--new-only` always
means jobs first discovered by the latest successful `collect`; running
`collect` twice makes the second run's new set empty. Existing V1 databases are
migrated automatically, with their existing rows initialized as `seen` and
their original `collected_at` values preserved.

## Filtering

Matching is deterministic, case-insensitive literal substring matching. A job
must match at least one positive title keyword, must not match any negative
title keyword, and must match a location keyword when that optional list is not
empty. Negative matches take precedence. `senior` is not rejected by default.

```toml
[filters]
positive_title_keywords = [
  "devops",
  "cloud",
  "platform",
  "infrastructure",
  "linux",
  "site reliability",
  "sre",
  "systems engineer",
  "production engineer",
  "automation",
  "ci/cd",
]
negative_title_keywords = [
  "director",
  "head of",
  "vice president",
  "principal",
  "staff engineer",
]
location_keywords = ["france", "remote"] # optional; [] accepts all locations
```

An absent or empty positive list matches no jobs. This prevents an incomplete
filter configuration from exporting every stored listing accidentally.

Work mode is normalized as `remote`, `hybrid`, `onsite`, or `unknown`. Remote
scope is `france`, `europe`, `worldwide`, `restricted`, or `unknown`. Greenhouse
does not expose a standard work-mode field, so Job Harvester conservatively
checks exposed work-mode metadata, title/location, offices, and finally explicit
phrases in the job description. Matching is boundary-aware and accent-insensitive;
vague technical text such as “hybrid cloud” or “distributed systems” is ignored.

```toml
[filters]
remote_policy = "any" # any, prefer, or require
allow_hybrid = true
allow_onsite = true
```

`any` retains remote and unknown jobs and applies the two explicit allow gates.
`prefer` has the same eligibility behavior and orders output as remote, hybrid,
unknown, then onsite. `require` accepts only explicitly remote jobs. Defaults are
`any`, `true`, and `true`, preserving existing behavior when these settings are
omitted.

## JSON export

`export` writes a UTF-8 JSON array to standard output, or to the path supplied
with `--output`. Records are sorted case-insensitively by company and title,
then by external ID. Each object has this stable, intentionally flat shape:

```json
{
  "source": "greenhouse",
  "external_id": "123",
  "company": "Example Company",
  "title": "Platform Engineer",
  "location": "Paris, France",
  "work_mode": "remote",
  "remote_scope": "france",
  "published_at": null,
  "url": "https://boards.greenhouse.io/example/jobs/123",
  "collected_at": "2026-08-18T08:00:00+00:00",
  "state": "new"
}
```

Timestamps are ISO 8601 strings in UTC when present. The array can be consumed
directly by Codex or another downstream tool; no Career-Ops integration is
performed in V2.

## Collection

The first milestone deliberately supports one path: one or more configured
Greenhouse job boards fetched by one CLI command.

```console
cp config.example.toml config.toml
# Set one company's name and Greenhouse board token in config.toml.
job-harvester collect --config config.toml --database jobs.sqlite3
Found 42 jobs; 42 new; 0 updated.
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

## Project structure

```text
job-harvester/
├── pyproject.toml
├── README.md
├── config.example.toml
├── src/job_harvester/
│   ├── cli.py             # argument parsing and run summary
│   ├── config.py          # TOML loading and validation
│   ├── filters.py         # deterministic relevance matching
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

The collection flow stays linear:

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
- `work_mode` (`remote`, `hybrid`, `onsite`, or `unknown`)
- `remote_scope` (`france`, `europe`, `worldwide`, `restricted`, or `unknown`)
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

V2 intentionally does not add other sources, scraping, lifecycle/closure
tracking, scoring, an LLM, a web interface, scheduling, Docker, or Career-Ops
integration.

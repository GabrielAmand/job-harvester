# Job Harvester

Job Harvester will be a small, local-first CLI that collects public job offers,
normalizes them, stores them in SQLite, and reports newly discovered listings.
It will not require Career-Ops or any hosted service.

> Status: V6 implemented with persistent, lazily revalidated review batches on
> top of the three-source collection pipeline and ATS board registry.

## Collection workflow

Configure one or more Greenhouse boards, Lever boards, and/or France Travail
searches plus the relevance filters in `config.toml`, then run:

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
All source fetches finish before the transaction starts. A network, parsing, or
source failure aborts the entire run and leaves the database and its previous
states intact. Successful sources do not commit independently because that would
make the shared latest-collection state describe different runs. Consequently,
`--new-only` always
means jobs first discovered by the latest successful `collect`; running
`collect` twice makes the second run's new set empty. Existing V1 databases are
migrated automatically, with their existing rows initialized as `seen` and
their original `collected_at` values preserved.

## Review batches

Review state is persistent and independent from collection state. Jobs begin as
`pending`, become `in_review` when assigned to an open batch, and become
`reviewed` after an explicit decision. Offers confirmed gone are `expired`.
There is deliberately no daily quota or date-based reset.

```console
job-harvester batch --database jobs.sqlite3 start --config config.toml --limit 20
job-harvester batch --database jobs.sqlite3 current
job-harvester batch --database jobs.sqlite3 review greenhouse 123 --decision interesting
job-harvester batch --database jobs.sqlite3 review lever abc --decision skip
job-harvester batch --database jobs.sqlite3 complete
job-harvester batch --database jobs.sqlite3 list
```

Only one batch may be open. `current` resumes it after a terminal restart, and
`complete` succeeds only after every member is reviewed. `abandon` explicitly
closes an unfinished batch and returns its unreviewed members to `pending`.
Reviewed jobs never enter later batches.

Before assignment, candidates are checked lazily through their original official
API. Confirmed missing offers are marked expired and replacements are tried until
the requested size is reached or candidates run out. Any temporary network,
authentication, API, or response failure aborts batch creation atomically: no
batch, expiry, or review-state changes are committed. Greenhouse and Lever jobs
collected before V6 lack their persisted board slug and must be collected once
with V6 before they can be revalidated.

Candidate ordering is deterministic: remote, hybrid, unknown, then onsite;
within each group, reliable publication timestamps sort newest first, followed
by discovery time and stable source/company/title/ID tie-breakers. Existing
filter eligibility still applies, and no numeric scoring is involved.

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
scope is `france`, `europe`, `worldwide`, `restricted`, or `unknown`. Lever's
structured `workplaceType` is authoritative when explicit, with structured
locations used for scope; `unspecified` falls back to explicit title, location,
and description wording. France Travail structured telework data takes priority
when present; because its v2 offer schema does not guarantee that field, explicit
title/location and then description wording are conservative fallbacks.
Greenhouse does not expose a standard work-mode field, so Job Harvester
conservatively
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
performed in V6.

## Collection

One CLI command fetches every configured Greenhouse and Lever board plus France
Travail search through official JSON APIs, normalizes all jobs, and writes them
atomically.

```console
cp config.example.toml config.toml
# Set real company names and public board identifiers in config.toml.
job-harvester collect --config config.toml --database jobs.sqlite3
Greenhouse: 2 boards; Found 42; 42 new; 0 updated.
Lever: 2 boards; Found 18; 18 new; 0 updated.
France Travail: Found 120; 110 new; 4 updated.
Total: Found 180; 170 new; 4 updated.
```

Running the same command again keeps the existing rows and should report zero
new jobs when the board has not changed. Network or configuration failures must
produce a non-zero exit status and must not discard previously collected jobs.

## Board registry and discovery

Greenhouse and Lever boards can be stored in the same SQLite database as jobs,
so a large board set does not need to live in TOML. Registry rows track provider,
slug, optional company name, enabled state, discovery/check timestamps,
validation status, and provenance.

```console
job-harvester boards add greenhouse cloudflare --company Cloudflare
job-harvester boards add lever voltus --company Voltus
job-harvester boards list
job-harvester boards validate
job-harvester boards disable lever voltus
job-harvester boards enable lever voltus
```

All board commands default to `jobs.sqlite3`. Put `--database PATH` immediately
after `boards` to use another database. New boards start as `unknown`; collection
uses only registry boards that are both enabled and `valid`. Validation calls the
official public list API once per board. An empty job array is valid, HTTP 404 is
invalid, and temporary HTTP/network/JSON failures update `last_checked_at` without
overwriting the previous validation status.

Discovery imports candidate ATS URLs from UTF-8 text or JSON. JSON may contain
URLs at any nesting level. Supported patterns are:

- `boards.greenhouse.io/<slug>`
- `job-boards.greenhouse.io/<slug>`
- `jobs.lever.co/<slug>`

```console
job-harvester boards discover --input candidate-urls.txt
job-harvester boards validate
```

Candidates are normalized, deduplicated, and stored as `unknown` until validated.
A practical workflow is to save search-result URLs or known ATS career URLs into
a text/JSON file and import it. V5 deliberately does not query search engines,
automate a browser, crawl arbitrary company sites, or infer an ATS from a generic
company domain; generic domains should first be resolved manually to an ATS URL.

Existing TOML Greenhouse and Lever entries remain supported. During collection,
TOML and registry boards are deduplicated case-insensitively by `(provider, slug)`;
the TOML entry and its company display name win. France Travail remains configured
only through TOML and environment credentials. A filters-only TOML file is valid
when all Greenhouse/Lever boards come from the registry.

## Install and run

Python 3.11 or newer is required.

```console
python3 -m venv .venv
.venv/bin/pip install -e .
cp config.example.toml config.toml
# Edit config.toml with real company names and board identifiers.
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
│   ├── board_validation.py # official API board validation
│   ├── batches.py         # persistent review queue and batch lifecycle
│   ├── config.py          # TOML loading and validation
│   ├── discovery.py       # candidate URL extraction/import
│   ├── filters.py         # deterministic relevance matching
│   ├── models.py          # normalized Job value object
│   ├── registry.py        # persistent board registry
│   ├── revalidation.py    # official per-offer availability checks
│   ├── storage.py         # SQLite schema and upserts
│   └── collectors/
│       ├── base.py        # small Collector protocol
│       ├── france_travail.py # France Travail OAuth and offer mapping
│       ├── greenhouse.py  # Greenhouse HTTP mapping
│       └── lever.py       # Lever Postings API mapping
└── tests/
    ├── fixtures/          # saved, sanitized API responses
    ├── test_france_travail.py
    ├── test_greenhouse.py
    └── test_storage.py
```

The collection flow stays linear:

```text
TOML config -> source collectors -> normalized Job records -> SQLite -> CLI counts
```

Each collector accepts source-specific configuration and returns normalized
`Job` records. It does not know about SQLite or Career-Ops. The CLI coordinates
collectors and storage without a plugin framework, service layer, or dependency
injection system.

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
`external_id` is the public job-post `id`, not `internal_job_id`. For Lever, it
is the posting `id`; for France Travail, it is the offer `id`. An upsert may
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

[[sources]]
type = "lever"
company = "Example Lever Company"
company_slug = "examplelevercompany"

[[sources]]
type = "france_travail"
search_terms = ["DevOps", "Cloud", "Administrateur systèmes"]
```

France Travail search terms are examples only and are fully configurable. Set
credentials in the process environment before collection:

```console
export FRANCE_TRAVAIL_CLIENT_ID="your-client-id"
export FRANCE_TRAVAIL_CLIENT_SECRET="your-client-secret"
job-harvester collect --config config.toml --database jobs.sqlite3
```

Create an application at `francetravail.io`, subscribe it to “Offres d'emploi
v2,” and use its client ID and secret. Authentication uses OAuth2
`client_credentials` for the `/partenaire` realm with scopes
`api_offresdemploiv2 o2dsoffre`. `config.toml`, SQLite files, credentials, and
access tokens must not be committed; credentials do not belong in TOML.

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

## Lever assumptions and risks

- A company slug is the segment in `jobs.lever.co/{company_slug}`. V3 requests
  `https://api.lever.co/v0/postings/{company_slug}?mode=json`; it does not scrape
  HTML and requires no authentication.
- Structured `workplaceType`, `categories.location`, `categories.allLocations`,
  and country take priority over prose. Vague fallback evidence
  remains `unknown`, and remote alone never implies worldwide.
- Live responses may include `createdAt`, but creation is not necessarily
  publication. V3 leaves `published_at` null rather than mislabeling it.
- Lever and Greenhouse IDs may match safely because identity remains
  `UNIQUE(source, external_id)`.

## Board registry assumptions and risks

- The registry is an additive `boards` table in the job database with
  `UNIQUE(provider, slug)`. Existing databases require no destructive migration.
- Greenhouse validation uses its unauthenticated official Job Board API; Lever
  validation uses its public Postings API. Valid-empty boards remain valid.
- Discovery only identifies candidates. It never marks a board valid and never
  scrapes career-page HTML.
- Registry validation metadata may commit independently because it does not alter
  job collection state. `collect` still fetches every source before the atomic
  job-state rollover/upsert transaction.

## France Travail assumptions and risks

- V4 uses only the official authenticated API at
  `api.francetravail.io/partenaire/offresdemploi/v2`; it does not scrape HTML.
- Each configured term is searched independently. Results are paged using
  `Content-Range` within the API's result window and overlapping offer IDs are
  deduplicated before storage.
- `dateCreation` is the API's offer-creation timestamp and is stored as
  `published_at`; `dateActualisation` is not substituted for publication.
- Employer names can be absent. Such offers use `Entreprise non précisée`, and
  invalid or absent origin URLs fall back to the official offer-detail URL.
- The current v2 schema does not guarantee a dedicated telework field. When one
  is returned, total/partial/no-telework values are authoritative; otherwise V4
  uses only explicit deterministic wording and leaves vague evidence unknown.
- A missing credential, failed token request, failed search, malformed page, or
  other source failure occurs before SQLite opens and aborts the entire atomic
  collection run.

V6 intentionally stops at the review decision. It does not add application
lifecycle tracking, another source, scraping, daily quotas, scoring, an LLM, a
web interface, scheduling, Docker, or Career-Ops integration.

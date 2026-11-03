# Instagram ingestion hardening

## Baseline inspected before implementation

- `collect_instagram` selected every enabled Instagram source in PK order; it did
  not use `last_checked_at` for eligibility. Defaults were 5 posts, up to 20, with
  a fixed 2-second account delay. `collect_sources` always forwarded its own
  default limit of 5, even if Instagram defaults were changed.
- Playwright used an existing dedicated manually authenticated profile, blocked
  images/media/fonts, and waited 1 second per navigation. It closed the browser
  after each source to end Playwright's sync asyncio runtime before ORM writes.
  That lifecycle prevents Django synchronous-ORM failures and must be retained.
- Link discovery waited, then scrolled up to twice with 400ms pauses on empty
  results. It did not inspect already-stored IDs before visiting post pages.
- Instaloader disabled downloads/retries and cached broad access failures in
  memory. Browser access failures were also cached, but neither command stopped
  its account loop. Authentication, access blocks and structural timeouts were
  often reported under the same generic message.
- Raw storage already had the correct `(source_account, external_id)` unique
  constraint and `get_or_create` first-snapshot behavior. Network access was
  outside transactions; completed source commits survived subsequent failures.
- `collect_sources` already caught command errors and ran the next platform, but
  ended with a generic error instead of an explicit per-platform/partial summary.

These are confirmed local fragility/request-volume issues, not proof of why
Instagram made a particular server-side decision after inactivity. A valid
session cookie does not guarantee that subsequent collection is permitted.
No live collection or challenge interaction is used during implementation/tests.

## Implementation and operator reference

### Collection flow

1. Validate limits and state configuration; select enabled configured Instagram
   sources, optionally filtered by username/source PK.
2. Acquire a nonblocking process lock in a dedicated state directory. Load the
   durable schedule; refuse collection on unreadable/corrupt state. An overlapping
   run fails without opening a browser.
3. Honor the global safety cooldown and cross-run pacing. Order due sources by
   due time, then PK, and select at most two by default. A recent database
   `last_checked_at` also defers a source even if no local schedule entry exists.
4. Persist a one-hour source reservation **before** network activity. An interrupted
   process leaves that reservation in place. Read stored external IDs before
   starting the synchronous Playwright runtime.
5. Inspect only the first two discovered links/posts by default, at most three.
   Skip known shortcodes before post-page navigation/metadata access. There is no
   scrolling, pagination added by this change, or search for older replacement
   posts when known/pinned links consume the scan budget. The existing 14-day
   relevance cutoff and treatment of missing timestamps remain unchanged.
6. Close Playwright's synchronous runtime before Django ORM operations. Commit
   each source independently, retaining the original raw-item audit snapshot and
   the unique `(source_account, external_id)` constraint. No network operations
   run inside a database transaction.
7. Persist the next due time. On a refusal, persist a global cooldown, commit any
   already-normalized current-source items independently, and stop immediately.
   An interrupted/failed source is not marked successfully checked.

The bounded window favors request reduction over completeness: pinned posts and
long intervals can defer/miss posts. The cap describes scanned post candidates
and explicit browser navigations, **not** every HTTP request made by Instagram's
page scripts or Instaloader's profile/listing implementation. Browser image,
media and font requests remain blocked. No automatic provider fallback occurs.
Instaloader still uses one connection attempt and fatal refusal status codes;
there is no same-run retry loop or history backfill.

### Defaults and configuration

All values can be supplied as Django settings or environment variables; a Django
setting takes precedence. Configuration is validated before collection. No
credentials are added to settings or documentation.

| Setting/environment variable | Default | Meaning / supported values |
| --- | --- | --- |
| `INSTAGRAM_MAX_SOURCES_PER_RUN` | `2` | Due source budget, 1–100 |
| `INSTAGRAM_MAX_POSTS_PER_SOURCE` | `2` | Scanned post budget, 1–3 |
| `INSTAGRAM_SOURCE_DELAY_SECONDS` | `60` | Delay after a source before the next; also persisted across runs; 1–86400 |
| `INSTAGRAM_NAVIGATION_DELAY_SECONDS` | `10` | Pause before each explicit Playwright navigation; 1–3600 |
| `INSTAGRAM_CHECK_INTERVAL_SECONDS` | `21600` | Successful source with new content: 6 hours; 60–604800 |
| `INSTAGRAM_NO_NEW_INTERVAL_SECONDS` | `43200` | No-new-content/private/missing/unavailable interval: at least 12 hours; 60–604800 |
| `INSTAGRAM_FAILURE_COOLDOWN_SECONDS` | `3600` | Other failure/interruption reservation: 1 hour; 60–604800 |
| `INSTAGRAM_RATE_LIMIT_COOLDOWN_SECONDS` | `86400` | Global refusal cooldown: 24 hours; 3600–2592000 |
| `INSTAGRAM_COLLECTION_STATE_DIR` | `~/.local/state/rytm-instagram` | Dedicated absolute directory outside the Rytm workspace/Git |

No-new-content intervals are never shorter than the successful-source interval.
Other source failures back off exponentially: 1h, 2h, 4h, then 6h by default.
If the configured base failure interval exceeds 6h, it remains the minimum/cap.
No random delay, human-behavior imitation or evasion is used.

The existing `INSTAGRAM_COLLECTION_PROVIDER` selection is preserved: its default
is `instaloader`; set `playwright` explicitly for the manually authenticated
browser. Existing dedicated browser profile/headless configuration is unchanged.

### Durable state and migration decision

No migration is needed. `SourceAccount.last_checked_at` is the successful database
checkpoint; existing stable raw shortcodes provide duplicate protection. Existing
fields cannot express failed-attempt/global cooldowns, so a small owner-only
`schedule.json` stores source PKs, due times, failure counts, global cooldown reason
and pacing timestamps. It contains no post content, cookies or credentials.
The file is atomically replaced and fsynced; a separate lock serializes runs.

Use one collector host and a persistent local state directory per deployment,
shared by every invocation using that deployment/session. An ephemeral container
filesystem or separate state directories on multiple hosts would lose/split
cooldowns. Provision a writable persistent volume before enabling unattended
collection on such hosts. This is a local filesystem lock, not a distributed
database lock. New directories use mode 0700 and schedule files 0600; provision
existing directories privately too. Do not delete/edit state to bypass refusals.
Failure to read/write it stops collection rather than falling back to memory.

If the process dies after database commit but before schedule completion, the
reservation and database checkpoint remain; the unique constraint still prevents
duplicates on resumption. Failures during current-source partial storage are
reported and cannot undo earlier completed source commits.

### Refusal handling and sanitized diagnostics

Global stop reasons include `challenge` (challenge/checkpoint URL/UI),
`verification` (login/identity/security confirmation), `rate_limit` (HTTP 429,
including Instagram API responses on HTTP-200 pages, or explicit rate-limit UI),
`temporary_block` (temporary block/suspicious activity), `login_required`,
`access_blocked` (400/403), and `unexpected_redirect`.

The provider returns a typed fixed reason, closes the browser, and the command
stops the source loop, persists a 24-hour global cooldown, reports counts/partial
progress and exits nonzero. It never retries, clicks verification UI, solves a
challenge or tries another provider. Later runs during cooldown open no browser
and fail explicitly with a cooldown-until timestamp. There is no force/bypass flag.

`private_account`, `not_found`, `unavailable`, `timeout` and
`unexpected_structure` remain distinct source-level diagnoses. Other due sources
may continue within the run budget after the configured source pause. Removed or
malformed individual post pages may be skipped within the original bounded scan;
a security refusal always stops immediately.

`--debug-browser` emits counts/flags and sanitized public profile/post URLs only.
It omits page titles/content, query strings, challenge path tokens, exception
bodies, headers, cookies and passwords. HTTP status and UI hints are evidence for
a diagnosis, not proof of Instagram's internal reason for refusing requests.

`collect_sources` runs the platforms independently and prints, for example,
`instagram=FAILED (committed partial progress retained); telegram=OK ...`.
It returns a nonzero aggregate result when a platform fails. Telegram still runs
once after an Instagram failure, and neither platform's commits are rolled back
by the other. Telegram's command/provider and default five-post limit are unchanged.

### CLI and small manual test

Run commands from the backend checkout, with its normal database/session
configuration already supplied. These examples are **operator commands**; no live
Instagram collection was run while implementing this change.

Offline selection first (database reads, no Instagram requests or schedule updates;
the directory/lock may be created):

```bash
cd /home/marko/RYTM/rytm-backend
venv/bin/python manage.py collect_instagram --username festrepublic --limit-sources 1 --limit-posts 1 --plan
```

Small browser test against one **enabled configured** source:

```bash
INSTAGRAM_COLLECTION_PROVIDER=playwright venv/bin/python manage.py collect_instagram --username festrepublic --limit-sources 1 --limit-posts 1 --dry-run --debug-browser
```

`--dry-run` makes actual Instagram requests but creates no raw items and does not
update `last_checked_at`. **Safety scheduling/pacing/cooldown still persists**, so
an immediate real run may be deferred. Use `--plan` for a genuinely offline preview.
Neither mode ignores recent checks or an active safety cooldown. If using another
account, replace `festrepublic` with its configured username or use `--source-id PK`.
Selectors never create or enable accounts.

Normal production browser collection (at most two due sources, two links each):

```bash
INSTAGRAM_COLLECTION_PROVIDER=playwright venv/bin/python manage.py collect_instagram
```

Explicitly process all currently due sources in this 21-account deployment,
retaining pauses and all cooldowns:

```bash
INSTAGRAM_COLLECTION_PROVIDER=playwright venv/bin/python manage.py collect_instagram --limit-sources 21
```

For deployments with more sources, the explicit per-run ceiling is 100; repeated
scheduled invocations rotate through remaining due sources. A small default run
is preferred. To orchestrate both platforms:

```bash
INSTAGRAM_COLLECTION_PROVIDER=playwright venv/bin/python manage.py collect_sources
```

CLI additions: `--limit-sources`, `--limit-posts` (alias of existing `--limit`),
`--source-id`, `--plan`; existing `--username`, `--dry-run`, `--debug-browser` stay.
`collect_sources --instagram-limit-sources N` changes only Instagram's source
budget. Omitted `collect_sources --limit` now respects each platform's own default;
an explicit value is forwarded to both and must satisfy each selected platform's
validation (Instagram rejects values above three). Python `call_command` callers
use the destination keyword `limit=N`, or pass `--limit-posts` as a CLI argument.

### Troubleshooting

- **Cooldown or pacing:** read the printed reason/until timestamp; wait. Username,
  source-PK and dry-run options never bypass it. Zero attempts during normal
  `no_due_sources`/`pacing` runs are expected and return success.
- **Challenge/verification:** stop the scheduler and use Instagram's normal manual
  process. Do not repeatedly launch collection to test whether it disappeared.
- **Login required:** check the chosen provider. For Playwright, use the existing
  `venv/bin/python manage.py instagram_browser_login` manual headed workflow and
  the same dedicated profile/OS user. For Instaloader, repair its explicitly
  configured trusted local session. Do not import someone else's cookies. After
  manual repair, still allow the persisted cooldown to expire.
- **Private/missing/unavailable:** verify the configured public username manually;
  no automatic source disabling or schema changes occur.
- **Timeout/structure error:** wait for backoff, then run a one-source debug test.
  Inspect only sanitized counts/flags; missing authenticated navigation with a
  session cookie is no longer automatically labeled expired login. Do not upload
  HTML dumps, browser profile files or raw session material.
- **State/lock errors:** check persistent directory ownership and the scheduler's
  OS user, stop overlapping processes, and restore valid state from backup if
  necessary. Do not erase a genuine active refusal cooldown.
- **Aggregate failure:** read each platform's summary. Telegram successes remain
  valid even when the overall command returns nonzero for Instagram.

### Verification

Tests cover provider normalization, known-ID navigation avoidance, no scrolling,
refusal classification, HTTP-200/API-429, partial current-source progress, source
budgets, CLI aliases, durable intervals/locks, cross-run pacing, interruption
reservation, cooldown refusal and Telegram dispatch/isolation. Network/browser
interaction is mocked; dedicated temporary schedule directories isolate tests.
The existing real-sync-Playwright/ORM regression also retains schedule isolation.

The full backend suite must be run against a **local** PostGIS test service:

```bash
ENVIRONMENT=development DATABASE_URL= POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=5432 venv/bin/python manage.py test --noinput
```

During the initial implementation the full suite and database integration subset were attempted,
but local PostgreSQL refused the connection. Docker was not started and no
production database was contacted. All 145 database-independent backend tests
passed, including 53 focused Instagram tests. These do not replace the blocked
transaction and persistence integration checks. `makemigrations --check --dry-run` confirms
no model changes, though database-history verification also requires that service.

Follow-up verification on 2026-11-03 used the available local PostGIS service:
all 699 backend tests and 305 focused tests passed, with normal database teardown
and no remaining test sessions. The Instagram source-association fixture was made
independent of positional mock responses: never-checked sources correctly precede
previously checked sources. Both failure-first continuation and never-checked-first
ordering now explicitly verify stored source provenance. Production source matching,
scheduling and challenge/rate-limit stop logic were unchanged in that follow-up.

No migrations, extraction/schema/admin/API/Android changes, secrets changes,
commits, pushes or deployments are part of this work.

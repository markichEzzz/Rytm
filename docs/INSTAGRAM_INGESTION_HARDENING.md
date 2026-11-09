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

`INSTAGRAM_COLLECTION_PROVIDER` now defaults to `playwright`, matching manual
browser login. An explicit environment value is still honored. `--provider
playwright` or `--provider instaloader` overrides selection for a single run,
without editing environment files. Instaloader is an explicit legacy mode with
its own trusted local authentication; it cannot reuse browser login. There is no
automatic provider fallback. Existing browser profile/headless paths are unchanged.

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

Explicitly process all currently due sources in this 16-account bundle,
retaining pauses and all cooldowns:

```bash
INSTAGRAM_COLLECTION_PROVIDER=playwright venv/bin/python manage.py collect_instagram --limit-sources 16
```

For deployments with more sources, the explicit per-run ceiling is 100; repeated
scheduled invocations rotate through remaining due sources. A small default run
is preferred. To orchestrate both platforms:

```bash
INSTAGRAM_COLLECTION_PROVIDER=playwright venv/bin/python manage.py collect_sources
```

CLI additions: `--limit-sources`, `--limit-posts` (alias of existing `--limit`),
`--source-id`, `--plan`; existing `--username`, `--dry-run`, `--debug-browser` stay.
`--source` is an alias for `--username`. `--provider` selects a provider for one
Instagram run. With Playwright, `-v 2` enables the sanitized browser diagnostics,
as does `--debug-browser`. `collect_sources --instagram-limit-sources N` changes only Instagram's source
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

## Browser-session investigation (2026-11-04)

### Confirmed root cause and live evidence

The local environment did not explicitly set `INSTAGRAM_COLLECTION_PROVIDER`.
The previous code therefore selected **Instaloader**, whereas
`instagram_browser_login` authenticated **Playwright's persistent Chromium**.
Successful browser login could not authenticate the default collector. This was
a provider-selection defect, not a different directory within Playwright.

A bounded diagnostic explicitly selected Playwright, the existing profile and
the configured `festrepublic` source, with one post and `--dry-run`. The source
profile and post both returned HTTP 200. Profile post/reel links were available;
one post was normalized with caption, aware timestamp and image metadata. No
challenge/checkpoint/rate limit was observed. Raw/source database writes were
disabled: zero RawSourceItems created, no source checkpoint updated. Only local
safety scheduling and Chromium's own persistent profile state were written.

The historical reported generic error itself was not reproduced. The evidence
confirms the authentication-stack mismatch and working current Playwright selectors;
it does not establish Instagram's reason for any earlier Instaloader refusal.
No HTML dump, caption, cookie/token, auth header or screenshot was saved as a
diagnostic artifact. Live verification used only this one configured source.

### Exact browser/session architecture

Login and collection both construct `InstagramBrowser` and call the same
`profile_directory()` helper. The default and locally observed directory is:

```text
/home/marko/.local/share/rytm-instagram-profile
```

`INSTAGRAM_PLAYWRIGHT_PROFILE_DIR` can supply another dedicated absolute path;
every invocation must use the same OS user/configuration. Both launch Chromium
with `launch_persistent_context(user_data_dir=...)`. There is no separate exported
storage-state file or cookie-injection step. Login is always headed. Collection
is headed by default; explicit `INSTAGRAM_PLAYWRIGHT_HEADLESS=true` changes only
browser mode, not the user-data directory. Diagnostic `profile_id` hashes the
resolved path so login/collection can be compared without printing profile files.

Login prints its profile identity/mode and warns when collection is explicitly
configured for Instaloader. Normal collection now chooses Playwright when unset
and always identifies the selected provider. Explicit Instaloader remains supported
but is never presented as consuming the browser session.

### Failure states and observability

Browser diagnostics distinguish `login_required`, `checkpoint`, `challenge`,
`verification`, `rate_limit`, `temporary_block`, `access_blocked`,
`profile_unavailable`, `private_profile`, `post_unavailable`, `navigation_timeout`
and `unexpected_structure`. Native Instaloader retains its earlier source-level
codes (`not_found`, `private_account`, `unavailable`, `timeout`) where applicable.

Failures carry a stage: browser launch, profile navigation, profile-link discovery,
post navigation or post parsing. They include the sanitized current path, a fixed
page-state marker, and navigation HTTP status when available. An Instagram API 429
on a successful HTTP-200 document is recorded separately. Generic Instagram page
titles may be shown; arbitrary/non-generic titles are redacted because they may
contain private content. Query strings, challenge path tokens and response bodies
are never emitted. Failure diagnostics appear in the command's error summary even
without verbose mode. Diagnostic-output failures cannot mask a security refusal.

Missing DOM/navigation selectors with a session cookie mean changed/unexpected
structure or a navigation timeout, not proof of a challenge. Empty profiles require
an explicit no-posts marker; known-ID-only windows are normal no-new-content
successes. Security URL checks match actual route prefixes, so a shortcode or
username containing `challenge`/`checkpoint` is not classified as security UI.
Hidden username/password inputs alone are not evidence of a visible login form.

The live run demonstrated the existing profile/reel link, caption, timestamp and
image parsing paths work; no speculative selector rewrite was needed. Genuine
security states still stop immediately, preserve committed progress and persist
the existing 24-hour global cooldown. There is no retry/bypass flag or challenge
automation. Private/unavailable profiles/posts retain the longer source backoff.

### Operator commands and troubleshooting

Use a configured enabled Instagram username; `kudypity_lviv` in an example does
not automatically create an Instagram source. This checkout has `festrepublic`.
Run from `/home/marko/RYTM/rytm-backend` with the existing configuration.

Check selection/next due time without Instagram requests:

```bash
venv/bin/python manage.py collect_instagram --source festrepublic --limit-sources 1 --limit-posts 1 --provider playwright --plan -v 2
```

One-source live diagnostic without raw/source database writes:

```bash
venv/bin/python manage.py collect_instagram --source festrepublic --limit-sources 1 --limit-posts 1 --provider playwright --dry-run -v 2
```

To actually collect that bounded source, omit `--dry-run` once it is due. Normal
browser collection retains the default two-source/two-post due-only budget:

```bash
venv/bin/python manage.py collect_instagram --provider playwright
```

The unqualified `venv/bin/python manage.py collect_instagram` now also defaults to
Playwright unless an explicit environment setting selects another provider.
Existing `--source-id`, `--username`, `--limit`, `--debug-browser`, scheduling,
known-ID skips and no-history behavior remain available.

If the provider line says Instaloader, use `--provider playwright` when relying
on browser login. If profile IDs differ, correct the OS user/profile configuration.
If login is required, use the normal manual headed login workflow and complete
verification yourself before confirming it. If selectors fail, inspect the stage,
marker and status instead of assuming a block. If a real challenge/rate limit
appears, stop and honor the cooldown; do not repeat live probes. No secrets or
environment files need editing to select a provider for one invocation.

**Dry-run still persists safety scheduling.** The successful one-post preview
scheduled this source six hours later. A post-test invocation must honor that due
time and may correctly report zero attempts. This is not another authentication
failure. No schedule deletion, interval reduction or alternate-source probing is
used to force a second live request.

### Final verification for this change

- Full local PostGIS backend suite: **707 tests passed, zero failures/errors**;
  normal test-database teardown completed. The focused Instagram/runtime selection
  passed 89 tests before the additional diagnostic-output failure guard was added;
  that guard's test is included in the final full-suite result.
- `manage.py check`, Python syntax, whitespace and credential-pattern checks passed.
- After tests, the one-source dry-run selected Playwright **without an environment
  override**, reported `no_due_sources`, and made zero additional requests. The
  existing schedule was honored (next due 2026-11-04 at 18:26 Europe/Kyiv). Raw item
  count and the source's database checkpoint were unchanged.
- No migration, extraction/Telegram/admin/Android changes, environment or credential
  edits, production operations, commits or pushes were made.

Changed files: `ingestion/providers/instagram.py`, `instagram_browser.py`,
`instagram_playwright.py`, `instagram_safety.py`, `instagram_discovery.py` (all in
the same providers directory), `ingestion/management/commands/collect_instagram.py`,
`ingestion/management/commands/instagram_browser_login.py`,
`ingestion/instagram_schedule.py`, `ingestion/test_instagram_playwright.py`,
`ingestion/test_instagram_hardening.py`, and this guide.


## Post-layout tolerance and current sources (2026-11-08)

### Evidence and limits

Operator-supplied diagnostics show authenticated HTTP-200 profiles with discovered
post/reel links for `rumbambar.lviv` and `emily_resort`, followed by two unsupported
post parses each. Other accounts parsed successfully. This localizes the reported
failure to post parsing; it does not identify the exact affected DOM layout.
No affected HTML fixtures were supplied, so attributing those failures specifically
to reels, carousels or video markup would be speculation. No live Instagram
requests, login, production seed or production data writes were made for this change.
The live evidence described in the previous section is historical.

Code inspection found restrictive assumptions: caption lookup only used main/article
H1 elements; plural reel redirect paths were rejected; any non-post Open Graph
canonical URL rejected otherwise usable caption data; invalid optional media could
reject a captioned item; and two unsupported posts escalated to a source failure.
Synthetic offline fixtures now exercise these assumptions. Actual affected-account
compatibility remains to be confirmed later by a bounded operator run.

### Canonical source configuration

Bundled current usernames (operator-provided, without live verification):

```text
festrepublic
malevich_concert_arena
kotelnya.ruin.place
kotelnya_
cult_comedy_hall
rumbambar.lviv
emily_resort
lollipop_projectsss
lvivart.center
dzyga.gallery
les_kurbas_theatre
urbancamp.lviv
center.for.urban.history
mad.lviv
pravdabeer
zankovetska.theatre
```

Profile URLs are generated/stored as `https://www.instagram.com/<username>/`.
The canonicalizer removes query strings and fragments, including sharing parameters
`obrf`, `cplk`, `srtk`, `rpxt`, `mdxt`, `vrfl`, `psln`, `xtok` and `exln`.
It validates Instagram hosts and profile-only paths and rejects credential-bearing
URLs. Collection constructs this canonical profile URL without using tracking data.

`seed_source_accounts` adds missing bundled accounts, canonicalizes URLs on matching
accounts, and deactivates only known obsolete bundled Instagram usernames. Matching
is case-insensitive; existing enabled/disabled choices, display names, checkpoints,
record identity and raw history are preserved. Custom accounts and Telegram sources
are untouched. Thus the bundle has exactly 16 accounts; custom sources may coexist,
and an operator-disabled current account is not automatically re-enabled.

The obsolete bundled `cult.comedy.hall` is replaced by `cult_comedy_hall`.
An existing old record is disabled, not renamed/deleted; historical raw provenance
stays attached to it. The new account gets its own source record if missing.
No production seed has been run. Future operator preview only:

```bash
venv/bin/python manage.py seed_source_accounts --dry-run
```

### Supported post data and extraction

Only canonical `/p/<shortcode>/` and `/reel/<shortcode>/` URLs are opened.
Input `/reels/<shortcode>/`, optional username prefixes, missing trailing slashes,
query strings and fragments normalize to these forms. Same-shortcode post-to-reel
redirects are accepted. Different-shortcode redirects/metadata remain rejected.
Profile, explore, tagged, audio, comments and legacy `/tv/` anchors are ignored.
Collaboration username prefixes are accepted in scoped profile content; global
fallback discovery keeps its source-username filtering. Pinned/known links still
consume the existing bounded scan window; no backfill or extra scrolling is added.

Caption candidates come from semantic dialog/main/article H1/caption/description
elements, structured `articleBody`/`caption`/`description`, Open Graph description,
and standard description metadata. Arbitrary comments/body text is not a fallback.
Carousel image lists and VideoObject/reel metadata are supported. A generic profile
Open Graph URL alone is not post identity and no longer defeats a valid caption.
Timestamp selection avoids adopting unrelated comment-link timestamps.

Caption is primary grounded event evidence. Invalid/missing optional image URLs
are omitted, preserving caption text and provenance. The current RawSourceItem
contract already allows an empty image URL and missing timestamp: a useful caption
with a known permalink can be stored without OCR/media. Missing facts are not
invented. Caption collection does not guarantee the caption contains an event.

The existing collector stores the caption in `RawSourceItem.raw_text`. Existing
extractor `source_texts` reads raw text independently of OCR, so extraction code
was left unchanged. Added integration tests cover caption-only storage/extraction
and an irrelevant image whose OCR returns empty text. These database-backed tests
require the local PostGIS service before they can be verified.

### Unsupported posts and profile availability

Each non-security post error increments `skipped_posts`, emits a fixed reason and
safe candidate-presence diagnostics, then continues only within the original limit.
A good post plus a bad post is successful with skipped-post visibility. If every
inspected post is unsupported, the provider returns `no_parseable_posts`, rather
than raising a source/security failure. The command reports this separately from
ordinary no-new-content and failed-source counts. A mixed run uses
`ok_with_skipped_posts`; an all-unsupported run uses `no_parseable_posts`.

A non-dry-run completed unsupported window updates the source checkpoint and uses
the normal no-new-content interval (12 hours by default). It is not immediately
retried, does not increment source failures and does not trigger global cooldown.
Known/old posts retain existing duplicate/relevance handling. A genuine challenge,
checkpoint, login failure or rate limit still stops immediately and preserves the
existing global cooldown. Browser closure before ORM writes is unchanged.

`profile_unavailable` remains a separate source-level failure with the existing
longer backoff. For renamed accounts, update the username/source configuration;
for permanently missing accounts, disable the source. Do not retry on every run.
No availability detector or refusal behavior was weakened.

Unsupported-post diagnostics contain only content kind, shortcode-parsed flag,
caption/timestamp/media-candidate flags and a path class such as
`/reel/<shortcode>/`. They never print captions, private media URLs, HTML, query
parameters, cookies or session secrets. Candidate presence does not prove validity.

### Later operator verification (not executed during this task)

From `/home/marko/RYTM/rytm-backend`, after reviewing source configuration and
honoring existing due times/cooldowns, run one bounded preview:

```bash
venv/bin/python manage.py collect_instagram --source rumbambar.lviv --provider playwright --limit-sources 1 --limit-posts 1 --dry-run -v 2
```

This is a live request when an eligible source is selected. Dry-run avoids raw-item
and source-checkpoint writes but still persists safety scheduling. Do not bypass
cooldowns to force it. Normal due-only collection remains:

```bash
venv/bin/python manage.py collect_instagram --provider playwright
```

### Offline verification for this change

- All 180 database-independent backend tests passed (74 Instagram-focused), including the new URL,
  layout, safe-diagnostic and command-result cases. Browser/network access is mocked.
- Focused database-backed tests and the full backend suite were attempted with an
  explicit development/loopback database configuration. Both stopped at database
  creation because `127.0.0.1:5432` refused connections. The full run discovered
  726 tests before two additional offline regressions were added (728 now discovered).
  The previous 707-pass result above is historical, not a result for this change.
- Direct caption-to-extractor and empty-OCR checks passed without database access.
  Seed persistence and caption-to-extractor storage integration tests remain unverified
  until the local PostGIS service is available. Docker was not started; production
  was not contacted. No migration or extraction/Telegram/Android change is needed.

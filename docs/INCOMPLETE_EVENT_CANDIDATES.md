# Incomplete event candidates

## Root cause (recorded before implementation)

`store_batch` in `ingestion/extraction.py` required a nonblank title and a future
start time before candidate persistence. Missing/ambiguous fields were placed in
`outcome.unresolved`; `batch.unresolved` entries were never considered for storage.
Any unresolved entry kept the raw source `new`, so repeated extraction could
detect events without creating rows visible in EventCandidate admin.

This was an extraction-readiness policy, not a database requirement. EventCandidate
already permits blank title/address/category and null start/end/coordinates/price.
Its unique per-source fingerprint constraint supports multi-event posts, but the
identity helper assumed a non-null start time. Final publication separately calls
`validate_approval` and validates the resulting public Event; the draft admin form
already permits missing facts. Those publication requirements must remain strict.

Telegram currency strings already pass through the shared price-parser fallback
in `ExtractionResult.from_mapping`. Unknown price remains null/unknown, not free.

## New draft lifecycle

An independently identified event with a known title or supported start time can
now become a private `EventCandidate(status=NEW)` even when other moderation
fields are missing. Known facts, raw-source relationship, source URL/image, price
semantics and extraction provenance are retained. Unknown strings are blank and
unknown dates/coordinates/prices remain null. No placeholder date/address or free
price is invented. Invalid schema fields are discarded individually instead of
discarding independently valid, evidence-grounded facts from the whole event.

The existing fingerprint uses normalized source facts, now including a null start
when it is unknown. Array order and subsequent moderator edits do not define the
identity. Existing complete-event fingerprints are unchanged. Candidate and source
observation uniqueness constraints, source-row locks and the existing ingestion
advisory lock remain in use. Repeated identical mentions within a batch collapse
to one candidate.

One raw post can create both complete and incomplete candidates. Once all concrete
entries have been saved or linked, the raw source becomes `extracted`, even when
those candidates still need manual completion. Raw audit entries record the
candidate ID and `persisted=true`; extracted does **not** mean approved/public.
Nothing creates a public Event until an authorized moderator explicitly approves.

Generic poster warnings, unknown event counts, detections with neither a usable
title nor a supported date, known historical events, probable duplicate matches
requiring comparison, and invalid storage fields (for example an invalid source
URL) still retain diagnostics without inventing a candidate. If unresolved entries
remain without a candidate, the source remains `new` for further investigation.

## Admin moderation

The candidate list has an **Approval readiness** column and filters for **Needs
manual completion** and **Ready for approval** on new candidates. Readiness is
computed from current editable fields and publication validation; there is no new
status or stale stored readiness flag. Filtering evaluates the small moderation
queue in Python, avoiding a schema migration.

At the top of each candidate, **Needs manual completion before approval** lists
the exact current missing/invalid fields: title, start, category, address,
coordinates, photo, or model-validation errors as applicable. It also displays
extraction ambiguities. Historical reasons/evidence remain in extraction notes and
the private raw audit; the live warning disappears as the current facts are fixed.

Moderator workflow:

1. Open the draft and compare the private source/poster/evidence.
2. Fill missing facts, select valid coordinates, and upload or explicitly authorize
   a publication photo. Unknown pricing may remain unknown.
3. If extraction ambiguity warnings are present, correct/verify them and check
   **I checked the extraction ambiguities against the source**, then save. This
   records a confirmation in the existing extraction notes, without a new column.
   Ordinary missing fields alone do not require this confirmation checkbox.
4. Save the candidate, then use the existing explicit approval action. Invalid
   candidates remain private and the admin action reports useful errors.

Published candidates remain read-only through this moderation interface; moderate
their linked public Events through the existing Events workflow.

## Approval validation

`validate_approval` still performs full model/constraint validation and enforces a
nonblank title/address/category, a timezone-aware future start, valid end ordering,
finite coordinates within supported Lviv bounds, valid pricing, and a publication
photo or explicit source-image authorization. It now also blocks unresolved
extraction-ambiguity confirmations. The resulting public Event is separately
validated before storage. Staff/publication permission checks, duplicate checks,
transactions and explicit source-photo handling are unchanged.

Admin diagnostics use the same field requirements plus model validation without
database uniqueness/FK lookups. This preview does not replace full approval-time
validation. An extracted event with a good title/date can still need a photo,
coordinates or address; it correctly appears as a draft requiring completion.

## Re-extraction and cached results

Normal repeated extraction preserves existing candidates. Explicit `--reextract`
uses the existing repair workflow for incomplete rows: it fills only safely
assigned, unedited blanks while preserving the candidate ID/fingerprint and
moderator edits. Approved/rejected/duplicate rows remain protected. Existing
multi-event matching still supports distinct entries without using array position.
No `--force-prices` flag is needed or recommended for this retry.

For OpenAI/Ollama cached batches, the retained original validated provider envelope
is revalidated when available, then evidence-grounded again. This recovers valid
facts previously dropped from schema-error summaries without forcing another model
request. Older caches lacking the original envelope use their bounded historical
summaries; unsupported/missing facts still need manual completion.

`--dry-run` previews drafts, deduplication and readiness without saving candidates,
raw status or raw metadata. It may still perform configured extraction/OCR/provider
I/O; it is not an offline networking switch.

## Telegram prices

The shared primary parser already normalizes `від 300 грн`, `від 100 грн`, and
`Від 150 грн` to numeric Decimal amounts with `price_status=paid`. The partial-draft
path uses the same parser and validation. Invalid/unknown amounts become
`price=None, price_status=unknown`; only explicit zero/free evidence means free.
If a different field fails validation, independently valid price facts are retained.

## Production operator commands (after deployment only)

These commands are documented for the operator; none were run against production.
Use the deployed backend environment with its existing extraction-provider
configuration. No schema migration is required for this change.

Preview the current Telegram backlog first:

```bash
python manage.py extract_events --platform telegram --retry-failed --reextract --limit 100 --dry-run
```

Persist/repair the Telegram backlog:

```bash
python manage.py extract_events --platform telegram --retry-failed --reextract --limit 100
```

The command processes at most 100 eligible raw posts. For a smaller first pass,
use `--limit 1` or add `--id RAW_ITEM_ID`/`--source CONFIGURED_USERNAME`.
It does not collect new posts or approve/publish candidates.

Output now separates **Candidates created**, **Draft candidates needing completion**,
**Candidates ready for review**, and **Unresolved entries without candidate**, plus
ignored/failed/skipped/repair/link counts. The draft/ready counters partition newly
created candidates on a real run; previews describe proposed candidates. Reused or
linked existing rows are reported separately. `Candidate-ready` in the detailed
legacy preview means persistable, including drafts; the explicit approval-ready
counter and admin live readiness indicate publication completeness.

For example, if eight raw posts yield nine distinct incomplete but identifiable
events without duplicates/errors, expect:

```text
Processed: 8
Candidates created: 9
Draft candidates needing completion: 9
Candidates ready for review: 0
Manual review without candidate: 0
Unresolved entries without candidate: 0
Ignored: 0
Failed: 0
```

Actual counts depend on evidence, duplicate matches and historical/generic warnings.
Successfully saved drafts are printed as **Saved private draft Candidate #…**,
with field reasons, rather than **Not persisted**. Repeat runs do not create a
second draft for the same source identity.

## Verification and scope

Tests cover nullable draft fields, stable missing-date identities, mixed batches,
schema-invalid-field recovery, grounded/cached retry facts, Telegram price strings,
admin warnings/escaping/form controls, strict approval and confirmation, mocked
storage-status/reuse flow, and database integration cases for persistence,
idempotence, moderator completion/approval and read-only dry runs. Existing poster
regressions now assert that uncertain dates remain null on saved drafts, rather
than asserting the event is invisible to moderators.

Initially the local PostGIS service was unavailable, so only 158 database-independent
tests could run. The follow-up on 2026-11-03 used the available local service:
**699 full-suite tests passed, with zero failures/errors**, and all **305 focused
tests passed**. Normal test-database destruction completed; a subsequent metadata
check found neither test databases nor sessions for the local test DB or the
reported `test_postgres` name. Docker was not started. Django checks, syntax and
whitespace checks passed; `makemigrations --check --dry-run` reports no changes.
No production data, secrets, schema, collection behavior,
Android code, commits, pushes or deployments were changed for this task.

Files changed for this task:

- `ingestion/extraction.py`
- `ingestion/extractors/contracts.py`
- `ingestion/extractors/openai.py`
- `ingestion/extractors/ollama.py`
- `ingestion/management/commands/extract_events.py`
- `ingestion/services.py`
- `ingestion/forms.py`
- `ingestion/admin.py`
- `ingestion/test_incomplete_candidates.py`
- `ingestion/test_multi_extraction.py`
- `ingestion/test_poster_layout.py`
- `docs/INCOMPLETE_EVENT_CANDIDATES.md`

Earlier uncommitted Instagram collection hardening remains separate and preserved.

## Regression follow-up (2026-11-03)

- Five hybrid provider-fallback tests and the partial-image extraction test had
  outdated `review` expectations. Supported incomplete detections now preview as
  `candidate` or persist as `created`. Their updated assertions also verify private
  NEW status, null/blank missing facts, absence of public Events and strict approval
  rejection. Non-events and uncertain event-count safeguards were not changed.
- The admin save regression was real: a form-like object with `changed_data` but
  no `cleaned_data` raised AttributeError. The save path now treats absent/empty
  cleaned data as no confirmation. Only an explicit cleaned boolean `True` records
  ambiguity confirmation. Tests cover minimal and real-form-style data without
  weakening missing-field validation or venue/geocoding edit tracking.
- The Instagram association failure came from an order-dependent test double,
  not a production source-linking error. Never-checked sources have due time zero
  and precede previously checked accounts. A positional failure therefore applied
  to a different source than the fixture assumed. The test now targets failure by
  source PK and gives both sources equal due times to prove continuation after
  failure. A second test proves never-checked-first scheduling and correct stored
  source provenance. Production scheduling and refusal handling were unchanged.
- `ready` counts distinct accepted candidate-pipeline proposals, including private
  incomplete drafts; it does not mean approved or publication-complete. The location
  fixture contains **two** events (Гурт 0 and Гурт 1), one with invalid pricing.
  The recovered unknown-price draft correctly contributes a second pipeline record.
  Tests now prove two fingerprints and two real candidate rows. An unresolved audit
  entry is a diagnostic for that draft, not a third event. No count algorithm was
  changed in the follow-up; `approval_ready` remains separate.
- Three threaded tests ended workers with `close_old_connections()`, which does
  not close healthy persistent connections before their maximum age. A controlled
  local SELECT-only check reproduced the open connection with `CONN_MAX_AGE=600`.
  Workers now call `connections.close_all()` in `finally`, and tests explicitly use
  persistent connections and assert closure. This fixes a genuine test-harness
  connection leak. No sessions were terminated and teardown was not skipped.

Follow-up files: `ingestion/admin.py`, `ingestion/test_hybrid.py`,
`ingestion/test_image_text.py`, `ingestion/test_instagram.py`,
`ingestion/test_location_extraction.py`, `ingestion/test_incomplete_candidates.py`,
`ingestion/test_extraction.py`, `ingestion/tests.py`, `events/tests.py`, this guide,
and `docs/INSTAGRAM_INGESTION_HARDENING.md` (verification update).

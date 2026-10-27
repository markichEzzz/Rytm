# Multi-event poster ingestion — implementation and verification

Backend-only changes, verified 2026-10-21. No Android changes/build, commit, push, deployment, live migration, live collection, or live candidate updates.

## Audit: actual existing pipeline and weakness

The existing collector boundary is `CollectedSourceItem` → `services.store_collected_item` → one `RawSourceItem`, unique to its configured source/post identity. Instagram HTML/discovery/browser adapters can retain `raw_image_url` and an `image_urls` list. They do not create one raw record per extracted event.

Actual extraction flow:

```text
collect_instagram / collect_sources / collect_telegram
  → source adapter → stored RawSourceItem
  → extract_events → prepare_image_text
  → bounded private image download + local Tesseract OCR/cache
  → caption + plain OCR + deterministic observations/location hints
  → configured provider (OpenAI by default, optional explicitly selected Ollama)
  → strict multi-event structured output
  → literal evidence, date/time/category/location/price grounding
  → source/group association validation
  → title + future-start readiness validation
  → logical event fingerprint + existing dedup/observation transaction
  → private EventCandidates, linked source observations, or unresolved review entries
  → existing human admin moderation (never automatic approval/publication)
```

The pre-existing system already had strict multi-event output, per-event evidence, shared venue validation, source fingerprints, cross-source dedup, candidate repair, private audits and sibling links. The weakness was that OCR discarded geometry and local scope checks depended mainly on blank-line paragraphs. Flattened neighboring entries could therefore share a paragraph and pass simple evidence-presence checks. A provider failure could also fall back to a misleading single deterministic suggestion.

## OCR representation and compatibility

`TesseractImageTextExtractor.extract_layout` makes one bounded subprocess call per image using stdin/stdout PNG bytes and `-c tessedit_create_tsv=1`. The built-in option avoids depending on a separate named `tsv` configuration file, which is absent from this project's locally unpacked Tesseract runtime. Unexpected non-TSV stdout raises the safe `local_ocr_invalid_layout` code; it is not cached as successfully empty OCR.

TSV word rows are aggregated into line blocks:

- `text`
- `x`, `y`, `width`, `height` in source-image pixels
- mean `confidence` and minimum word `confidence_min` (0–100)
- `order`, Tesseract `page`, `block`, `paragraph`, `line`

Private `RawSourceItem.raw_metadata.image_text_layout` stores per-image dimensions, image index, blocks and truncation indication. Existing `image_text` remains a normalized plain string with `IMAGE n:` markers. `extract_text` remains available, and custom text-only extractors still work without an `extract_layout` method. Manual/legacy plain OCR snapshots remain usable; absent geometry is not invented.

Existing image download limits remain: at most three images per source item, 5 MiB each, 16 million pixels, allowed image MIME/origins, memory-only image processing, bounded timeouts. TSV parsing reads at most 2 MB/5,000 rows and stores at most 240 lines per image. Provider context is rebuilt through a geometry/text/confidence allowlist, with at most 240 blocks per image, 12,000 total layout text characters and 50 logical groups. Individual layout lines are capped at 500 characters. Existing caption/OCR limits remain 8,000/16,000 characters. Group block-order references avoid retransmitting duplicate block objects in the provider input.

OCR cache fingerprints include a layout-version marker, so previously engine-cached OCR can refresh into structured output. Legacy manually supplied plain OCR is preserved. Missing OCR dependencies retain the existing caption/fallback behavior.

## Explainable grouping and detection

Centralized in `ingestion/poster_layout.py`; no computer-vision model or external OCR service.

1. Validate/sort line blocks by vertical position, then horizontal position.
2. Temporal anchors are explicit dates/weekday headings and clock patterns. Ukrainian dates, numeric dates and English month names/abbreviations are recognized as grouping hints.
3. Separate likely columns only when temporal anchor x positions have a substantial gap: greater than 120 pixels or 24% of occupied image width, whichever is larger. Wide explicitly shared headers are not forced into one event column.
4. Within columns, split vertical chunks at a gap greater than 28 pixels or 2.2 times median line height, whichever is larger.
5. For continuous schedules with inline `time + title` rows, split at those rows and carry an explicit preceding day heading as date context. Date-only/time-only anchors are not treated as titles. Several standalone clocks without clear title associations are flagged.
6. Text-only captions/legacy OCR can use blank paragraphs, repeated explicit event labels, and clear day-heading/inline-time rows as logical groups. These are labelled text evidence, not manufactured spatial coordinates.
7. Significant overlapping line boxes and multiple labelled titles around one unassigned clock produce ambiguity warnings.
8. Repeated identical carousel content is collapsed as corroborating evidence, not counted as another event. Caption and OCR groups are counted separately when detecting multiplicity so their corroboration alone does not imply two events.

Multi-event suspicion uses multiple temporal groups within a source, at least three distinct clocks, or multiple dates and clocks. A single explicit date range does not automatically become separate events. This is extraction/review context, not a rule that mechanically creates an event per block or image.

## Provider instructions and schema

The existing strict `MULTI_EXTRACTION_JSON_SCHEMA` and event array are retained without new required response fields. Every non-null fact still needs literal caption/OCR evidence. No free-form prose parser was introduced.

The shared provider prompt now explicitly covers spatial/logical groups, independent events on one image, shared day headings, uncertain date ranges, caption/image complementarity, carousel repetition, and prohibitions on borrowing a title/date/price/venue from another group.

The bounded `ExtractionContext.poster_layout` is supplied with existing caption, OCR, source timestamp, safe account context, backend category choices and deterministic observations. Browser state, cookies, credentials, download URLs and arbitrary raw metadata are not copied into this input. Tests inject fake secrets and verify they are absent from the request.

OpenAI still makes one structured Responses request per raw item; there are no per-block LLM calls. Provider cache versions were advanced for the changed input/instructions (OpenAI 4, Ollama 2). OpenAI remains the default; Ollama is still optional and is never selected automatically after an OpenAI failure.

## Field association and ambiguity

The final local association gate identifies the event's group from its grounded title and temporal evidence, preferring an exact labelled title and the evidence's source. A quote must belong to that group's content; explicit date-heading context is usable only for temporal fields. Complementary caption facts need that event's logical caption section, or clear shared wording.

Cross-group fields are removed. Unsupported price becomes `None`/`unknown`; unsupported venue/address remains empty; no address is invented. An uncertain title/start prevents candidate creation and produces an unresolved event for moderation. Overlap, multiple unassigned time anchors, or a low-confidence critical OCR word/day heading similarly require review. Minimum word confidence below 45 is not hidden by a high line average. Reliable caption evidence can still support events when image OCR is poor.

A day range such as `25–27 жовтня` does not assign an individual date to each independent row. Bare explicit Ukrainian weekday headings use the existing source-publication-relative Kyiv resolver; they are not resolved against an arbitrary future year. English month aliases are normalized only for local temporal evidence checking. Existing timezone/DST and past-event policy remain intact.

If a model omits a visible temporal group, local diagnostics flag that group rather than fabricating its event. Truncated input also produces an unresolved review entry. Valid sibling events may still become candidates, while the source remains pending review for incomplete sections.

All semantic results continue to require human moderation, even when associations are confident.

## Shared fields and prices

The existing `validated_shared_context` remains authoritative for inherited venue/address: explicit post-wide/program wording is required. A nearby venue, isolated logo, account name, or event-specific venue is insufficient. An individual venue overrides shared venue/address context. The final group gate also blocks neighboring venue/price quotes from being reused as if they belonged to every row.

Price semantics remain:

- explicit paid price → numeric + `paid`;
- explicit free entry → zero + `free`;
- missing/unsupported/ambiguous price → null + `unknown`;
- explicit `від 300 грн` → 300 with the existing minimum/base-price note.

Moderator-owned pricing remains protected by the existing re-extraction mechanisms.

## Candidate identity, provenance, dedup and repair

A new source with N ready, independently grounded events produces N logical candidates attached to the same RawSourceItem. No additional raw records are created for split events. Candidates retain original post/image links and private `[Poster] group=… image=… source=…` notes. Raw metadata holds safe layout, OCR confidence, per-event group IDs and ambiguity diagnostics.

Logical event fingerprints remain based on source facts: title, start time and location, never array/group/image position. Same-source siblings are not merged merely for sharing a venue, image or post. Repeated identical mentions still deduplicate. Existing cross-source matching and source-observation linking are unchanged and tested.

Re-extraction is intentionally conservative:

- Existing complete, reviewed, approved/linked or legacy-identity candidates are preserved.
- A suspected split behind a preserved single candidate gets private `poster_split_review` diagnostics and an explicit command reason. No provider call is made just to overwrite a complete candidate.
- Incomplete rows may receive the existing safe blank-only repair when a unique matching event is supported; IDs and moderator edits remain protected.
- Additional/unassignable split events from an incomplete legacy row are surfaced as separate manual-review suggestions. They are not silently assigned to that old row or automatically published.
- Existing multi-candidate calendar re-extraction can still add clearly distinct valid entries through the unchanged fingerprinted batch path.
- No approved Event is deleted or changed automatically.

## Admin and command diagnostics

Existing source links, original caption, plain OCR and sibling-candidate links are reused. RawSourceItem and EventCandidate admin now also show a private, escaped **Poster groups and ambiguity** diagnostic panel. No admin redesign or public API fields were added.

`extract_events` keeps its existing workflow/options. Dry-run output adds OCR block count, multi-event suspicion, logical group count and layout warnings. `--debug-json` includes safe poster context and per-event group metadata alongside existing extracted count, readiness, unresolved reasons, decisions and dedup results. No secrets are logged.

## Verification

**All 370 ingestion tests passed** (`Ran 370 tests in 9.334s`, `OK`).

**The full backend suite passed: 545 tests** (`Ran 545 tests in 42.950s`, `OK`; no failures/errors/skips). This includes 39 new poster regressions beyond the prior 506-test baseline. Django system checks reported no issues.

Final test evidence: `/tmp/rytm-poster-ingestion-tests.log`, `/tmp/rytm-poster-backend-tests.log`, and `/tmp/rytm-poster-ocr-smoke.log`. These logs are temporary; the verified results are recorded here. `git diff --check` also passed.

Relevant suite:

```bash
cd /home/marko/RYTM/rytm-backend
venv/bin/python manage.py test ingestion
```

Full suite:

```bash
venv/bin/python manage.py test
```

`makemigrations --check --dry-run` returned **No changes detected**. No migration was created or applied to the live database.

Real local OCR smoke: the configured `/home/marko/RYTM/.local-tools/ocr/usr/bin/tesseract` processed an in-memory two-event synthetic image using `ukr+eng`. Result: **6 OCR blocks, 2 logical groups, multi-event=True**, mean confidence 96.2 and minimum word confidence approximately 95.1. Recognized text retained both names, dates, times and prices. No Instagram requests, LLM calls or database writes occurred in that smoke check.

The deterministic fixtures cover A–J: one event; two dated events; three events sharing an explicit venue; one date/multiple times; multiple days/several events; ambiguous titles/time; strong multi-event caption/weak OCR; independent/from/free/unknown prices; past+future events; similar names that must remain distinct. Additional tests cover two columns, overlap, low-confidence line/word/day headings, shared ranges, missing model groups, English months, weekday context, carousel repetition, dry-run/no writes, strict input secrecy, source provenance/admin links, cross-source dedup, fallback, and protected legacy repairs.

## Safe verification commands

Use stored raw items first; these commands do not collect Instagram posts, approve events, or persist candidate/OCR metadata in dry-run mode. They may perform the normal bounded image download/provider extraction when needed.

```bash
cd /home/marko/RYTM/rytm-backend
venv/bin/python manage.py extract_events \
  --source festrepublic --platform instagram --limit 3 --dry-run
```

For already extracted records, explicitly preview the existing safe re-extraction path:

```bash
venv/bin/python manage.py extract_events \
  --source festrepublic --platform instagram --limit 3 --reextract --dry-run
```

Inspect a chosen stored post without broad scanning:

```bash
venv/bin/python manage.py extract_events --id <raw_item_id> --reextract --dry-run --debug-json
```

`--id` is the RawSourceItem ID, not an EventCandidate ID. Existing complete candidates can correctly report preserved/manual split review instead of being rewritten. No live-source dry-run or collection was needed for this implementation verification; automated fixtures and the real local in-memory OCR smoke were used. No live candidate persistence was performed.

## Changed files

Under `/home/marko/RYTM/rytm-backend/`:

- `ingestion/image_text.py` — TSV OCR, line geometry/confidence, compatible plain text and versioned image cache.
- `ingestion/poster_layout.py` — new bounded grouping, detection and per-event association gate.
- `ingestion/extractors/contracts.py` — optional layout context only; no response/database schema change.
- `ingestion/extractors/structured.py` — shared prompt and allowlisted structured input.
- `ingestion/extractors/hybrid.py` — explicit day/month temporal context and hand-off to stronger group scoping.
- `ingestion/extractors/openai.py` — provider cache version.
- `ingestion/extractors/ollama.py` — optional provider cache version.
- `ingestion/extraction.py` — layout preparation, safe fallback, field validation, omission diagnostics, protected legacy split review.
- `ingestion/repair.py` — preserve layout audit and surface unassigned additional events during repair.
- `ingestion/admin.py` — private poster diagnostic panel alongside existing provenance/siblings.
- `ingestion/management/commands/extract_events.py` — private debug/preview diagnostics.
- `ingestion/test_poster_layout.py` — new deterministic poster/OCR/extraction regressions.
- `ingestion/MULTI_POSTER_VERIFICATION.md` — this report.

## Remaining limits

- Grouping is a conservative heuristic, not a guarantee of semantic understanding. Dense artistic layouts, nonlinear text, overlapping columns and ambiguous shared information can require manual review.
- OCR recognition errors remain possible; geometry/confidence is evidence, not truth. Missing/expired images can still leave caption-only context.
- Carousel handling remains limited to the existing first three retained image URLs. This task does not build exhaustive carousel discovery/pagination.
- A completely omitted event without any recognizable temporal/layout evidence cannot be recovered deterministically.
- Unsafe legacy one-to-many repairs are deliberately flagged for manual moderation; no old candidate/Event is automatically removed.
- Real Instagram/OpenAI extraction quality on a specific live poster was not measured in this run. Strict mocked-provider tests and real local Tesseract smoke verify the integration without source traffic or live writes.

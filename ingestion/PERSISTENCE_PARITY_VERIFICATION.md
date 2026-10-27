# Telegram persistence discrepancy — 2026-10-09

## Verified database facts

The assumption that Крик любові and Наталка Полтавка have no candidate is incorrect. There are three persisted rows: one complete upcoming candidate and two incomplete fallback drafts. All are unpublished, status `new`, with reviewed_at/reviewed_by unset. No live extraction or record repair was run during this investigation.

| RawSourceItem | Intended event | Candidate | Raw status | Review/duplicate state | Persisted facts |
|---|---|---|---|---|---|
| #2 | Крик любові | #11 | extracted | new/unreviewed; observation unique | title blank, start null, venue blank; performance; paid/300 |
| #3 | Наталка Полтавка | #12 | extracted | new/unreviewed; observation unique | title blank, start null, venue blank; performance; paid/100 |
| #5 | НАБУККО | #13 | extracted | new/unreviewed; observation unique | 2026-10-10 17:00 Europe/Kyiv (+03), opera theatre; performance; paid/100 |

Candidate #11/#12 source_fingerprint is the empty string. Their observations #1/#2 have fingerprint `39623ca591e0c65aedf53f332da7505aec1330ee8d66b88be88fcd96b3d6ca57`; each belongs to a different raw item, so this is not a uniqueness conflict. There are no confirmed/probable duplicate links or matched_candidate/event targets.

Candidate #13 and observation #3 fingerprint: `84c3b0a0291b4bc468945cdcfc97b041d07c765eca0deab6603b3ea5e0897610`. It has no public Event yet.

Creation timestamps (UTC): #11 13:36:53.979042, #12 13:37:04.064205, #13 13:37:31.906874.

The actual persisted manual-review entries are Raw #4 (Містична органна музика…) and Raw #6 (Ланч-концерт…), both raw status `new`, no candidate, with exact stored reason `start_time: event start is in the past`. These are correct rejections.

There is no per-invocation processing history in the database. Therefore the reported Processed:3 invocation cannot be reconstructed conclusively from its aggregate summary alone. The default command selects only raw status `new`, excluding #2/#3 once their incomplete drafts were marked extracted. Processing #4/#5/#6 produces the reported one complete candidate/two reviews; this explains the summary shape without treating #2/#3 as duplicate-suppressed events. The stored row facts and review reasons above are verified, not inferred.

## Root cause and exact branches

Earlier dry-runs successfully used OpenAI batch output for the three upcoming events. During the stored real extraction of Raw #2/#3, the provider became unavailable or invalid and the pipeline switched to deterministic fallback (`audit.provider=deterministic`, `audit.status=fallback`). The stored deterministic result has `is_event=true`, title/start/venue null, confidence 0.2, and notes: multiple event dates/manual selection required; title unresolved; semantic provider unavailable or invalid. Raw #2 retains price 300 and Raw #3 price 100.

The historic audit does not record the original sanitized exception code. It is impossible to determine retrospectively whether that provider failure was timeout, HTTP failure, malformed output, or another provider error. No new provider call was made to guess the old failure.

The old single-result path computed multiple=false because event_count was None and no matching ambiguity flag was present. It then constructed a candidate whenever is_event=true, ran full_clean(), and stored it via store_observation(). The model intentionally permits blank title and null start for manually edited/admin drafts, so full_clean() passed. These incomplete rows got blank source fingerprints and their raws were marked extracted. Later default runs excluded them.

The batch path already required title plus a valid future start before creating a candidate. Independent provider calls changed which path/facts were used; there was no demonstrated dry-run-only address, coordinate, price, category, FK, uniqueness, or database constraint rejection for the three original upcoming facts. Correct deduplication did not suppress Raw #2/#3. The permissive fallback insertion and missing diagnostics were bugs.

The branch generating manual-review counters is: batch has event signal, zero ready entries after title/future-start/model checks, and no created/linked entries → ExtractionOutcome(review). For the stored #4/#6 audits, that check rejects explicit past dates.

## Fixes

- Single-result/deterministic fallback now uses the existing batch readiness and storage path. Missing title/start produces field-level unresolved entries and preserves the raw for review; no new blank candidates are created.
- Both modes use one read-only persistence planner: model validation, conditional fingerprint uniqueness for new rows, exact per-source reuse, source observation reuse, cross-source matching, and create/link/review decision. The real path recomputes under the existing advisory lock and transaction. Dry-run performs queries/validation only; it never writes then rolls back.
- Confident duplicates link existing logical candidates/Events and report Duplicates linked separately. Existing local candidates are preserved and reported as reused. Probable/ambiguous extraction matches remain raw manual review with target/reason, rather than creating a potentially duplicate candidate automatically. The lower-level observation utility retains its existing direct-call review behavior.
- Real command output lists each created/linked/preserved target and each not-persisted field reason. Debug preview exposes the same decisions, linkage counts, and sanitized provider fallback code.
- Sanitized provider failure codes are stored prospectively. Database constraint failures store bounded field/code diagnostics without SQL, responses, credentials, or arbitrary exception text. Concurrent status/legacy candidate changes return explicit skip reasons.
- Existing incomplete legacy #11/#12 and good #13 are protected. No repair/recreation/deletion, moderator overwrite, or live source linkage occurred.

Readiness parity applies to the same extracted facts and the same database state. A later independent provider request may still fail or return different facts; dry-run does not freeze the next remote response. The code now makes that change explicit and refuses incomplete fallback drafts.

## Validation

Full backend suite: **331 tests passed**, Django system check clean. New regressions cover ready→successful persistence, confident duplicate linkage/counters, ambiguous review/reasons, field-level model failure, single/batch decision parity, incomplete provider fallback, rerun preservation, existing blank legacy preservation, sanitized DB constraint failure, and concurrent status changes. Existing fallback/OCR conflict expectations were updated to require review instead of accepting incomplete candidates.

Live verification after tests was database inspection only. SHA-256 hashes of every row/field, including geometry, remained identical for SourceAccount, RawSourceItem, EventCandidate, EventSourceObservation, LocationLookup and Event.

## Files changed in this task

- ingestion/extraction.py
- ingestion/management/commands/extract_events.py
- ingestion/test_persistence_parity.py
- ingestion/test_hybrid.py
- ingestion/test_image_text.py
- ingestion/PERSISTENCE_PARITY_VERIFICATION.md

## Safe next command for Marko

```bash
cd /home/marko/RYTM/rytm-backend
venv/bin/python manage.py extract_events \
  --source kudypity_lviv \
  --platform telegram \
  --limit 5 \
  --reextract \
  --dry-run
```

This includes already-extracted rows in the preview and explains that #11/#12 are preserved incomplete legacy drafts requiring moderator completion. It does not repair them automatically or overwrite #13. No force flag is required or recommended.

No commits, pushes, deployment, Android changes, or migrations.

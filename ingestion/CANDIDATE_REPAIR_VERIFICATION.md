# Incomplete candidate re-extraction verification

Verified locally on 2026-10-09. No live rows were persisted, and no commit,
push, deployment, or Android change was made for this task.

## Behavior

Previously, an existing candidate could short-circuit re-extraction even when
its title and start were missing. Explicit `--reextract` now retries the current
provider for incomplete, unreviewed candidates, bypassing historical extraction
caches, including old deterministic fallbacks. Without that flag the row is
preserved. Complete single-event candidates remain untouched; the existing
multi-event calendar workflow continues to support distinct entries.

Repairs fill missing fields only after validating the merged candidate's title,
future start, model constraints, location evidence, and duplicate/source identity.
Ambiguous assignments require review. Nonblank values and moderator-owned fields
(including intentionally cleared fields) are preserved. Moderator ownership is
recognized from provenance markers and historical Django admin change entries.

A real repair updates the same candidate ID in a transaction, rechecking its
current moderation state. Source links, old observations, moderation data, and
provenance remain; the prior extraction audit is archived and the new attempt
recorded. Repairs have a separate `Candidates repaired` counter. Provider failure
leaves candidate fields unchanged and records the new reason in real mode.
Dry-run performs the same proposal validation without saving rows or audits.

Provider diagnostics distinguish the attempted configured provider from a
fallback result and include a sanitized HTTP status, without logging response
bodies, keys, or request headers.

## Tests

Full backend suite: **343 tests passed**, with no system-check issues.

Twelve repair regressions cover preserved incomplete rows without re-extraction,
fresh provider retry, unchanged identity/history/source observations, successful
missing-field updates, moderator protection, complete-candidate preservation,
failure audit preservation, dry-run immutability, real repaired counters,
successful-cache bypass, ambiguous multiple-event rejection, and HTTP diagnostics.
Existing parity and pricing tests were updated for complete-row preservation.

## Live dry-run

Executed the requested command after tests:

```sh
python manage.py extract_events \
  --source kudypity_lviv \
  --platform telegram \
  --limit 5 \
  --reextract \
  --dry-run
```

The configured provider was OpenAI (`gpt-6-luna`). Transport instrumentation
confirmed fresh HTTP requests for raw rows #2 and #3, belonging to candidates
#11 and #12. Both returned **HTTP 429** (`provider_http_failure`). The response
body was not inspected, so a more specific API error cause is not established.

| Row | Provider retried? | Recovered title | Recovered start | Result |
| --- | --- | --- | --- | --- |
| Candidate #11 / raw #2 | Yes, OpenAI HTTP request | No | No | Incomplete; retry failed with HTTP 429 |
| Candidate #12 / raw #3 | Yes, OpenAI HTTP request | No | No | Incomplete; retry failed with HTTP 429 |
| Candidate #13 / raw #5 | No | Existing НАБУККО retained | Existing 2026-10-10 17:00 Europe/Kyiv retained | Complete candidate preserved |
| Raw #4 | No fresh request | Existing extracted facts | 2026-08-28 19:00 Europe/Kyiv | Rejected: start is in the past |
| Raw #6 | No fresh request | Existing extracted facts | 2026-09-05 14:00 Europe/Kyiv | Rejected: start is in the past |

Summary: processed 5; candidates created 0; repaired preview 0; candidate repairs
needing review 2; manual review without a candidate 2; skipped 1. No repair was
reported ready following a provider failure. Live recovery of #11/#12 remains
blocked by the HTTP failures; expected titles/dates were never hardcoded.

Before/after fingerprints of all SourceAccount, RawSourceItem, EventCandidate,
EventSourceObservation, LocationLookup, and Event rows were identical, including
comparison with the baseline captured before the test run. Candidate #11/#12
still have blank titles/null starts; candidate #13 and historical audits remain
unchanged. Real persistence was exercised only in isolated test databases.

## Files changed for this task

- `ingestion/repair.py`
- `ingestion/extraction.py`
- `ingestion/management/commands/extract_events.py`
- `ingestion/provenance.py`
- `ingestion/admin.py`
- `ingestion/extractors/openai.py`
- `ingestion/test_candidate_repair.py`
- `ingestion/test_persistence_parity.py`
- `ingestion/test_unknown_price.py`
- `ingestion/CANDIDATE_REPAIR_VERIFICATION.md`

The workspace contains prior unrelated development changes; this list describes
the current repair task, rather than every dirty file in the repository.

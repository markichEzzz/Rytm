# Location extraction verification — 2026-10-08

Verified the existing @festrepublic RawSourceItem #1 and both requested commands in dry-run mode.
No candidate, raw-source metadata, lookup-cache record, or public Event changed; before/after row
fingerprints matched exactly. No Android changes, migration, publication, deployment, commit, or push.

## Diagnosis before integration

The caption explicitly establishes a shared October program at !FESTrepublic, and separately identifies
the theatre for the Zhadan entry. The old private audit records “venue_name evidence belongs to another
program entry; removed” followed by “Unsupported venue_name removed by local evidence validation” for
the ordinary program entries. The old paragraph-scoping rule recognized only narrow “all events” wording;
it rejected this post's shared introduction. The stored batch had already discarded those original quotes,
so the exact initial model quotes cannot be recovered. New provider batches are retained alongside
grounded output to preserve that diagnostic evidence in future.

Candidates #1–#7 and #10 all share this cause. Candidate #8 retained its explicit theatre override.
Candidate #9 is already published and has moderator changes; it is excluded from location enrichment.
No street address is supplied by this caption. The saved OCR is garbled and does not establish a venue
or street. Account identity is not used to fill these missing facts.

## Final live extraction preview

```bash
python manage.py extract_events --source festrepublic --limit 1 --reextract --dry-run
```

Configured provider: OpenAI, configured model `gpt-6-luna`. Detected 17 events, 8 candidate-ready,
9 requiring date/past-event review, 0 duplicates, 0 persisted. Eight existing unpublished candidates
could receive safe missing-location enrichment. Location evidence is usable for all 17 entries.

The following table reports venue/address/provenance for every extracted event. `F` means the validated
shared venue **FESTrepublic**, provenance `shared_post_context`. `T` means the event-specific venue
**Драмтеатр ім. Марії Заньковецької**, provenance `event_specific`. Every address remains absent.

| Title | Venue | Address | Provenance | Location can resolve | Extraction readiness |
| --- | --- | --- | --- | --- | --- |
| Wellboy | F | — | shared_post_context | Yes, F point below | Past event |
| MC Петя. Головний мотиватор України | F | — | shared_post_context | Yes, F point below | Past event |
| Sick Solution | F | — | shared_post_context | Yes, F point below | Candidate-ready |
| ODYSSAY | F | — | shared_post_context | Yes, F point below | Candidate-ready |
| Курган: сольна програма | F | — | shared_post_context | Yes, F point below | Candidate-ready |
| KARNA | F | — | shared_post_context | Yes, F point below | Candidate-ready |
| Jamala | F | — | shared_post_context | Yes, F point below | Candidate-ready |
| Один в каное | F | — | shared_post_context | Yes, F point below | Date/range unresolved |
| Tayanna | F | — | shared_post_context | Yes, F point below | Candidate-ready; existing approved row unchanged |
| Phil it | F | — | shared_post_context | Yes, F point below | Start-time evidence unresolved |
| Дайс Кон 2026: Vivienne Mort, Лея | F | — | shared_post_context | Yes, F point below | Past event |
| Дайс Кон 2026: діти інженерів, Omana, Робаний Йот | F | — | shared_post_context | Yes, F point below | Past event |
| Дизаріум 2026. Самозванці | F | — | shared_post_context | Yes, F point below | Date/range unresolved |
| Сергій Жадан. Презентація книжки «Гімни барокових війн» | T | — | event_specific | Yes, T point below | Candidate-ready |
| Squirt: The Las Vegas Show | F | — | shared_post_context | Yes, F point below | Past event; existing location can be enriched |
| For Fit Day 2026 | F | — | shared_post_context | Yes, F point below | Candidate-ready |
| Tutor’s Forum 2026 | F | — | shared_post_context | Yes, F point below | New suggested time unresolved; existing date preserved |

Prospective lookup checks used the same resolver with unsaved preview candidates and no persistence:

- F: Nominatim resolved `FESTrepublic, Львів, Україна` to latitude **49.8508107**, longitude
  **24.0524873**, source `geocoded`, within supported bounds. One shared lookup verifies this venue;
  no per-event repeated external requests were necessary.
- T: strict known-venue reuse returned latitude **49.8439958**, longitude **24.0275425**, source
  `known_venue`, from candidate #8's existing reliable venue point, without external requests.

The unresolved creation/readiness cases are date-related, not missing-location cases. No unsupported
street address or house number was added. A future persisted extraction keeps existing candidate dates,
prices, identity, status, moderator edits and coordinates, even where the fresh model date is unresolved.

## Stored-candidate resolver preview

```bash
python manage.py resolve_candidate_locations --source festrepublic --dry-run
```

```text
Processed: 9
Resolved: 0
Already had coordinates: 1
Skipped no evidence: 8
Ambiguous: 0
Failed: 0
```

This command correctly reads saved rows, not the previous extraction preview. Candidate #8 already has
coordinates. Candidates #1–#7 and #10 still have empty venue/address because enrichment was not saved.
Their proposed validated shared venue would make all eight eligible for resolution after explicit
persistence. The actual stored rows currently contain usable location evidence on only #8 among the
nine unpublished candidates. Candidate #9 is excluded because it is published.

## Implementation and checks

- Added conservative location matching/hints and validated post-wide context; individual venue overrides win.
- Extended the strict multi-event prompt/contract while retaining compatibility with older cached envelopes.
- Classified venue/address provenance privately and retained original provider output for diagnosis.
- Enriched safe blanks during re-extraction under candidate locks; protected moderator edits, including
  deliberately cleared fields recorded in current notes or historical Django admin change logs.
- Preserved grounded locations in schema-invalid private review entries without allowing invalid candidates.
- Added explicit `extract_events --resolve-locations` integration with the existing resolver. Dry-run
  integration uses unsaved previews and no persistent cache writes. Manual coordinates remain authoritative.
- A moderator venue/address edit invalidates old geocoding provenance for reuse while preserving coordinates.
- Backend suite: **244 tests passed**. JavaScript price-toggle suite: **4 tests passed**. Django checks
  passed, migration check detected no changes, and `git diff --check` passed.

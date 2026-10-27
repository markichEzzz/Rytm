# OpenAI primary provider restoration

Local verification on 2026-10-13.

## Configuration and behavior

The local `.env` contained two `EVENT_EXTRACTION_PROVIDER` assignments. The later
`ollama` assignment was overriding `openai`. It now has exactly one provider
assignment and the preferred model:

```dotenv
EVENT_EXTRACTION_PROVIDER=openai
EVENT_EXTRACTION_MODEL=gpt-6-luna
OPENAI_API_KEY=<existing secret, preserved>
```

Optional `OLLAMA_BASE_URL` / `OLLAMA_MODEL` settings remain intact. Ollama's
adapter and tests are retained; it is selected only by explicit
`EVENT_EXTRACTION_PROVIDER=ollama`. OpenAI selection ignores Ollama settings and
never automatically switches to it. Explicit deterministic selection remains
available. With absent provider/model settings, the project now defaults to
OpenAI / `gpt-6-luna`; an explicitly empty model still fails safely.

The existing shared prompt, bounded caption/OCR input, structured multi-event
conversion, evidence validation, Ukrainian time handling, pricing, categories,
deduplication, repair flow, and moderator protection are preserved.

Command output prints the configured provider/model before processing rows.
OpenAI HTTP failures show sanitized reason `provider_http_failure` and status
(including 401/429); timeout and invalid-response codes remain visible. Fallback
uses deterministic/manual review. No response bodies, prompts, or keys are logged.

## Verification results

- Full backend suite: **369 tests passed**; Django system checks passed.
- Added four regressions covering default/explicit selection, no Ollama initialization
  when OpenAI is selected, identity-preserving OpenAI retries with moderator protection,
  and HTTP 401/429, timeout, and invalid-response fallback without any Ollama call.
- Existing Telegram/Instagram, pricing, multi-event, repair, and optional Ollama tests pass.
- Runtime configuration confirms `Provider: openai`, `Model: gpt-6-luna`, and an existing
  API key, without exposing the key.

The full suite and live dry-run were initially blocked by a stopped database. The user
started Docker/PostgreSQL, after which both verifications completed successfully.

## Live OpenAI dry-run

Executed only:

```sh
python manage.py extract_events \
  --source kudypity_lviv --platform telegram --limit 5 --reextract --dry-run
```

Output confirmed `Provider: openai | Model: gpt-6-luna`. Fresh transport requests
went only to the OpenAI Responses endpoint for raw items #2/#3/#4/#6; no Ollama
request occurred. Candidate #13 needed no inference because it was already complete.

| Candidate | Title recovered | Start, Europe/Kyiv | Category / price | Repair readiness |
| --- | --- | --- | --- | --- |
| #11 | Крик любові | 2026-11-18 19:00 (+02:00) | performance / paid 300 | Would become complete |
| #12 | Наталка Полтавка | 2026-10-25 12:00 (+02:00) | performance / paid 100 | Would become complete |
| #13 | Existing НАБУККО retained | Existing 2026-10-10 17:00 retained | Existing performance / paid 100 retained | Complete row preserved |

#11 preview fills title, start, and Палац культури залізничників «Рокс» venue.
#12 preview fills title, start, and Львівський національний академічний театр опери
та балету venue. Existing category/pricing values already agree and are preserved.
Minimum-price notes and title/date ambiguities remain private review evidence;
readiness never means approval or publication.

The historical August occurrence in #12's post was excluded from repair. Raw #4
(2026-08-28) and raw #6 (2026-09-05) remain rejected with `event start is in the past`.

Summary: processed 5; created 0; repaired preview 2; candidate repairs needing review
0; manual review without candidate 2; skipped 1. There were no provider failures.
Before/after fingerprints of every SourceAccount, RawSourceItem, EventCandidate,
EventSourceObservation, LocationLookup, and Event row were identical. #11/#12 still
have their original blank titles/null starts in the live database; repair proposals
were not persisted and old audits remained untouched.

No real extraction, commit, push, deployment, or Android change was performed.

## Next command

The local `.env` is already restored. To repeat the preview:

```sh
cd /home/marko/RYTM/rytm-backend
venv/bin/python manage.py extract_events \
  --source kudypity_lviv --platform telegram --limit 5 --reextract --dry-run
```

## Files changed for this restoration

- `.env` (provider/model assignments only; existing secret retained)
- `ingestion/extraction.py` (OpenAI selection default)
- `ingestion/extractors/openai.py` (preferred model default)
- `ingestion/management/commands/extract_events.py` (provider header and HTTP diagnostics)
- `ingestion/test_primary_provider.py` (selection and isolation regressions)
- `README.md` (preferred configuration and explicit fallback behavior)
- `ingestion/OPENAI_PRIMARY_VERIFICATION.md` (this report)

Prior Ollama and other unrelated workspace changes were retained.

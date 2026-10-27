# Public discovery and search verification — 2026-10-09

## Architecture and behavior

SourceAccount now supports instagram and telegram. Existing Instagram records/settings remain intact;
`seed_telegram_sources` idempotently adds kudypity_lviv and lvivgo_afisha without changing existing settings.
Both collectors persist the same CollectedSourceItem contract through RawSourceItem. Telegram parses
observed public HTML `data-post` identifiers, message text, aware publication times, and poster CSS URLs;
IDs/canonical URLs come from channel/message IDs, never DOM position. HTTP requests are bounded,
redirects disabled, and inaccessible/non-parseable pages fail safely. No authentication/session data is
collected. There is no separate Telegram extraction pipeline.

Telegram poster URLs on the observed telesco.pe CDN reuse bounded private downloads and existing
Tesseract OCR. Images are not automatically published. OpenAI still receives only bounded caption/OCR,
safe source context, and deterministic observations, not browser/session contents.

`collect_telegram` supports --username, --limit (1–20), and --dry-run. Without a username it uses all enabled
Telegram records. An explicit unseeded username may be previewed without creating a live source row.
The existing Instagram command already processes all enabled Instagram sources; its compatibility and
per-source isolation remain, with expanded summaries. `collect_sources` dispatches by optional platform
and continues to the other platform after a failure. `extract_events --platform telegram` uses the shared
extractor, retaining multi-event, categories, price, moderation, and location semantics.

Private EventSourceObservation links raw items to a canonical candidate or Event and records extracted
per-event facts. Reliable event fingerprints exclude platform. Matching also compares normalized/fuzzy
titles (minimum 0.85 similarity), Kyiv day, start times, and exact venue/address or points within 75 m.
Equal start times with strong agreement confirm links; differences up to 30 minutes or competing
matches remain probable-review candidates. Same venue/date alone and generic concert titles do not
establish a duplicate. Stable fingerprints preserve links despite moderator-corrected titles.

Confirmed observations can fill protected-safe location blanks and untouched unknown pricing with
stronger explicit evidence. Existing/manual price values, including manual Unknown, remain protected.
Approval links source observations to the public Event and retains duplicate protection under the same
PostgreSQL transaction lock used for extraction matching. Admin displays duplicate states, source
observations/facts, and links to matching candidates/Events for comparison and existing moderation actions.

Public search accepts q, category, date_from, date_to, price_status, and existing bbox. Search only covers
public title/description/address; generated descriptions include venue names. Kyiv start-date boundaries
are inclusive; approved/upcoming visibility remains. Invalid/reversed dates or invalid price statuses
return 400. Unknown never matches free. A single composite (status, start_time) index supports public
queries without adding multiple low-selectivity indexes. Convenience date presets are handled in Android
and translated into canonical date ranges.

The Android main feed now queries the backend with a 350 ms debounce, combined category/date/price
filters, custom date-range picker, visible active state, and reset. Older requests are cancelled and
cannot overwrite newer results. Empty filtered results use «Подій за цими фільтрами не знайдено»;
existing loading/error/retry design remains. Feed filters do not propagate to map queries.

## Tests and build

- 300 backend tests passed; Telegram tests use fixtures/mocks and an explicit live-network guard.
- 4 existing JavaScript tests passed.
- 42 Android unit tests passed, including query mapping, Kyiv presets, combined filters, debounce,
  reset, and protection against a late cancelled response. Debug APK built successfully.
- Android used the installed Java 17 and existing Gradle 9.7.1 distribution with an isolated temporary
  project linked to the actual workspace sources. No JDK was downloaded; Java configuration was unchanged.
  The APK uses the existing configured debug backend URL with the required trailing slash supplied as
  a build override. APK: `../rytm-android/app/build/outputs/apk/debug/app-debug.apk`.
- Django makemigrations --check --dry-run reports no changes; diff whitespace checks pass.
- Read-only GET checks against live local data returned 200 for plain list, Ukrainian q, price_status,
  combined q/category, and date range. Positive filtering and submission data use isolated API tests.

## Live collection dry runs

| Platform/source | Result | Details |
|---|---|---|
| Telegram kudypity_lviv | Working | 5 posts parsed; newest publication 2026-10-06 |
| Telegram lvivgo_afisha | Working, older activity | 5 posts parsed; newest publication 2026-07-24 |
| Instagram jamfactory.artcenter | Parsing failed | Profile discovery found a post; bounded post read had no usable structure |
| Instagram malevich_club | No discoverable links / parsing failed | Profile loaded but no post/reel links after existing bounded waits/scrolls; not asserted to be an empty account |
| Instagram dzyga.gallery | Working | 1 recent post collected in preview |

Telegram checks used limit 5. Instagram checked three real enabled accounts beyond festrepublic with
limit 1 and the existing Playwright profile in headless mode. No blockers were bypassed or repeatedly
retried. The actual DB has 21 enabled Instagram sources. SHA-256 fingerprints before/after collection
confirmed no changes to SourceAccount, RawSourceItem, EventCandidate, LocationLookup, or Event.
No live discoveries were persisted and no live migrations were applied.

## Pending migrations and next commands

Live migration planning found exactly:

- events.0005_event_event_public_start_idx
- ingestion.0006_alter_sourceaccount_platform_eventsourceobservation

Previous category-choice migrations are already applied in this local DB. Other installations should
also apply any pending predecessors normally; no data migration reclassifies existing events.

```bash
cd /home/marko/RYTM/rytm-backend
venv/bin/python manage.py migrate
venv/bin/python manage.py seed_telegram_sources
venv/bin/python manage.py collect_telegram --username kudypity_lviv --limit 5 --dry-run
venv/bin/python manage.py collect_telegram --username lvivgo_afisha --limit 5 --dry-run
venv/bin/python manage.py collect_sources --platform telegram --limit 5 --dry-run
venv/bin/python manage.py collect_instagram --limit 3 --dry-run
venv/bin/python manage.py extract_events --platform telegram --limit 50 --dry-run
```

Once reviewed, running a collection command without --dry-run persists raw discoveries only; extraction
without --dry-run creates/links private candidates. Public publication still requires moderator approval.
No collection schedules were created. Nothing was committed, pushed, or deployed.

## Files changed for this work

Backend:
- events/models.py, events/views.py, events/search.py, events/test_search.py
- events/migrations/0005_event_event_public_start_idx.py
- ingestion/models.py (existing ingestion/collectors.py contract reused unchanged)
- ingestion/providers/telegram.py, ingestion/image_text.py
- ingestion/deduplication.py, ingestion/extraction.py, ingestion/services.py, ingestion/admin.py
- ingestion/management/commands/collect_telegram.py
- ingestion/management/commands/collect_sources.py
- ingestion/management/commands/seed_telegram_sources.py
- ingestion/management/commands/collect_instagram.py
- ingestion/management/commands/extract_events.py
- ingestion/migrations/0006_alter_sourceaccount_platform_eventsourceobservation.py
- ingestion/test_discovery.py, ingestion/test_extraction.py, ingestion/test_instagram.py
- README.md, this report

Android:
- app/src/main/java/ua/rytm/app/domain/model/EventFilters.kt
- app/src/main/java/ua/rytm/app/data/remote/RytmApi.kt
- app/src/main/java/ua/rytm/app/data/repository/EventRepository.kt and EventRepositoryImpl.kt
- app/src/main/java/ua/rytm/app/data/fake/FakeEventRepository.kt
- app/src/main/java/ua/rytm/app/ui/feed/FeedViewModel.kt, FeedScreen.kt, FeedFilters.kt
- app/src/test/java/ua/rytm/app/domain/model/EventFiltersTest.kt
- app/src/test/java/ua/rytm/app/ui/feed/FeedSearchTest.kt
- app/src/test/java/ua/rytm/app/data/repository/EventRepositoryImplTest.kt
- app/src/test/java/ua/rytm/app/ui/submit/SubmitViewModelTest.kt (API stub signature compatibility)
- README.md and debug build output

Existing unrelated workspace changes were preserved.

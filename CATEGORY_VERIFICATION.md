# Category extension verification — 2026-10-09

Added education (Навчання / Майстер-класи), performance (Вистави / Кіно), and concert (Концерти).
All five previous category values remain valid; existing records are not reclassified.

Backend choices are shared with EventCandidate. DRF choice validation, admin selectors/filters,
and submission moderation automatically use the expanded choices. Extraction contracts derive
allowed values from Event choices, and both prompts describe the new semantics. Shared cue checks
support workshops/masterclasses/lectures/classes, theatre/plays/screenings, and live music/bands/
jazz/orchestras. Bare live does not prove a concert. DJ nights, raves, and parties retain party;
concert plus ancillary afterparty retains concert. Mixed cues use the advertised heading or an
explicit primary format; unresolved formats stay null. Local LLM evidence checks no longer accept
concert-only text as evidence for party.

Android maps all eight backend values, retains OTHER for unfamiliar values, and gives the three
new categories Ukrainian labels and colors. The feed and map have shared category filter chips
(default: all). Submission choices and event detail labels use the same category label mapping.
There was no existing category icon mapping to extend.

## Checks

- Full backend suite: 277 tests passed.
- API tests exercise GET /api/events/, all three new category filters, organizer submissions,
  model validation, and serializer round trips using isolated test records.
- Django makemigrations --check --dry-run: No changes detected.
- Generated choice-only migrations: events/0004_alter_event_category.py and
  ingestion/0005_alter_eventcandidate_category.py. Applied by tests, not applied to the live local DB.
- Android: 36 unit tests passed; debug APK assembled successfully.
- The normal Gradle wrapper could not obtain the configured Java 25 daemon (download returned HTTP
  400). Build/unit checks used the same source tree linked into /tmp/rytm-android-category-check,
  the existing Gradle 9.7.1 distribution, installed Java 17, and cached dependencies. The workspace's
  Java configuration was preserved. A valid debug base URL override was supplied for the build.
- git diff --check passed in both repositories.

## Files changed

Backend:
- events/models.py
- events/migrations/0004_alter_event_category.py
- events/test_categories.py
- ingestion/migrations/0005_alter_eventcandidate_category.py
- ingestion/extractors/categories.py
- ingestion/extractors/parsers.py
- ingestion/extractors/contracts.py
- ingestion/extractors/openai.py
- ingestion/extractors/hybrid.py
- ingestion/test_categories.py
- ingestion/test_extraction.py
- ingestion/test_hybrid.py
- ingestion/test_multi_extraction.py
- README.md
- CATEGORY_VERIFICATION.md

Android:
- app/src/main/java/ua/rytm/app/domain/model/Category.kt
- app/src/main/java/ua/rytm/app/ui/theme/CategoryColors.kt
- app/src/main/java/ua/rytm/app/ui/components/CategoryFilters.kt
- app/src/main/java/ua/rytm/app/ui/feed/FeedScreen.kt
- app/src/main/java/ua/rytm/app/ui/map/MapRoute.kt
- app/src/main/java/ua/rytm/app/ui/submit/SubmitRoute.kt
- app/src/main/java/ua/rytm/app/ui/eventdetail/EventDetailScreen.kt
- app/src/test/java/ua/rytm/app/domain/model/CategoryTest.kt
- README.md

Existing unrelated workspace changes were preserved. No commit, push, or deployment was performed.

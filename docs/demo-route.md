# One real Lviv route for end-to-end testing

## Existing architecture

`TourRoute` / `TourRouteStop` in `tour_routes.models` already implement this feature.
A route has title, short/full description, theme (`route_type`), estimated integer duration,
difficulty, optional cover, publication flag, nullable submitting user and unique `demo_key`.
Stops have order, title/description, WGS84 latitude/longitude floats, optional address/image
and a recommended stop duration. `(route, order)` is unique; ordering is `(order, id)`.

There is **no transport-mode field or `walking` type**. The selected seed is a walking
itinerary described in its text, with existing `history` theme, `easy` difficulty and a
60-minute estimate. This adds no schema/API enum. The 45 minutes of suggested landmark
pauses are estimates, not opening hours or a walking-time guarantee.

The existing API exposes published valid routes:
- `GET /api/routes/` (array; route summaries, search/filter support).
- `GET /api/routes/<id>/` (description and ordered stops).
- User submissions remain separate: `POST /api/routes/submit/` creates pending drafts.

Distance is computed by existing PostGIS geography `ST_Distance` over consecutive stops,
not stored and not a routed pedestrian length. The seed returns 771.0 m with
`distance_kind: straight_line`. Images are absent and existing placeholders are used.

Existing Django Admin already includes `TourRouteAdmin` and ordered editable `RouteStopInline`,
with validation and publication-after-inlines. No admin or API changes were necessary.

Android uses TourRouteApi -> TourRouteRepository -> route list/detail ViewModels and existing
Compose screens. DTOs sort stops by order/id. Feed/Map's top `Маршрути` selector shows routes;
Feed opens detail, Map opens map mode (`Routes.TourRouteDetail(routeId, startInMap=true)`).
Start route shows numbered markers, selected stop and next-stop controls. The polyline is
explicitly schematic. This is internal typed navigation; no external route deep link is
currently registered in the manifest/navigation for this feature.

## Reviewed landmark coordinates

Verified on 2026-11-10 against the following geographic records; WGS84, latitude first.
Coordinates identify the landmark footprint, not a guaranteed entrance/access point.

| Order | Landmark | Latitude | Longitude | Geographic source |
|---|---|---|---|---|
| 1 | Львівська опера | 49.843989 | 24.026178 | [Wikidata Q1246092](https://www.wikidata.org/wiki/Q1246092) |
| 2 | Площа Ринок / Львівська ратуша | 49.841835 | 24.031326 | [Wikidata Q4271056](https://www.wikidata.org/wiki/Q4271056) |
| 3 | Домініканський собор | 49.842778 | 24.034167 | [Wikidata Q2994596](https://www.wikidata.org/wiki/Q2994596) |
| 4 | Порохова вежа | 49.842781 | 24.035561 | [Wikidata Q4373143](https://www.wikidata.org/wiki/Q4373143) |

Converted from DMS and rounded to six decimals:
1. 49°50′38.36″N, 24°1′34.24″E.
2. 49°50′30.6074″N, 24°1′52.7740″E.
3. 49°50′34″N, 24°2′3″E.
4. 49°50′34.01″N, 24°2′8.02″E.

Opera cross-check: [Outdooractive](https://www.outdooractive.com/en/poi/%D0%BB%D1%8C%D0%B2%D1%96%D0%B2%D1%81%D1%8C%D0%BA%D0%B0-%D0%BC%D1%96%D1%81%D1%8C%D0%BA%D0%B0-%D1%80%D0%B0%D0%B4%D0%B0/lviv-theatre-of-opera-and-ballet/807651273/)
and [Discover](https://discover.ua/en/en/locations/lvovskiy-opernyy-teatr) place it within
about 7 m of the selected point. Powder Tower cross-check:
[FindWay](https://find-way.com.ua/en/regions/lviv-region/lviv/powder-tower-lviv) is within
about 12 m. The Discover Dominican entry currently gives a conflicting point outside the
historic centre, so it was rejected rather than copied. No fabricated businesses/events,
images, opening hours, accessibility or exact pedestrian directions were added.

## Command and safety

```sh
python manage.py seed_demo_route
```

Stable key: `lviv-first-acquaintance-v1`. Creates/updates only this seed-owned record and
orders 1..4. Runs atomically, validates before publication, preserves route/stop IDs on reruns,
never deletes data and never modifies events/users. Existing curated media on this same
record is preserved; supplied seed text/coordinates/duration are restored on rerun.
Unexpected extra/out-of-range stops or a user-owned record cause a rollback/refusal instead
of deleting/adopting data. The unrelated legacy `seed_demo_routes` (four drafts) is unchanged.

The command refuses a non-local DB host **before querying** unless an operator explicitly
passes `--allow-nonlocal-database`. The repository's default DATABASE_URL is non-local;
it was never used for this task. Docker was not started.

Local verification used `/tmp/rytm-local-route.py`, a temporary wrapper that reads existing
local compose credentials, forces DATABASE_URL to `127.0.0.1:5432`, sets development mode
and never prints credentials. Exact executed commands (from the backend repo):

```sh
venv/bin/python /tmp/rytm-local-route.py test tour_routes --noinput
venv/bin/python /tmp/rytm-local-route.py check
venv/bin/python /tmp/rytm-local-route.py migrate tour_routes 0002 --plan
venv/bin/python /tmp/rytm-local-route.py migrate tour_routes 0002 --noinput
venv/bin/python /tmp/rytm-local-route.py seed_demo_route
venv/bin/python /tmp/rytm-local-route.py seed_demo_route
venv/bin/python /tmp/rytm-local-route.py makemigrations tour_routes --check --dry-run
venv/bin/python /tmp/rytm-local-route.py runserver 127.0.0.1:8765 --noreload
```

The existing local database lacked `tour_routes.0002_tourroute_submitted_by`; its already
existing nullable FK migration was applied **locally only**. No new migration was created.
For later reproducible local use without the temporary wrapper, first set
`RYTM_LOCAL_DATABASE_URL` securely to your loopback PostGIS URL, then:

```sh
ENVIRONMENT=development DATABASE_SSL_REQUIRE=false \
DATABASE_URL="${RYTM_LOCAL_DATABASE_URL:?Set a loopback local PostGIS URL first}" \
venv/bin/python manage.py seed_demo_route
```

## Actual local result / API example

Created route id **5**; the second run updated id 5 without duplicates. It is published and
has exactly 4 stops. The pre-existing four legacy draft routes were preserved.
Live localhost HTTP list/search and detail were fetched successfully:
`http://127.0.0.1:8765/api/routes/` and `/api/routes/5/` (the exported list is filtered by title).
[Full captured list and detail JSON](first-lviv-route-api.json) includes all descriptions,
coordinates, stop IDs and metadata. The same capture is in the Android test resources.

A compact excerpt of the actual detail response (other fields omitted here):

```json
{
  "id": 5,
  "title": "Перше знайомство зі Львовом",
  "route_type": "history",
  "estimated_duration_minutes": 60,
  "difficulty": "easy",
  "cover_image": null,
  "stop_count": 4,
  "distance_meters": 771.0,
  "distance_kind": "straight_line",
  "stops": [
    {"order": 1, "title": "Львівська опера", "latitude": 49.843989, "longitude": 24.026178},
    {"order": 2, "title": "Площа Ринок / Львівська ратуша", "latitude": 49.841835, "longitude": 24.031326},
    {"order": 3, "title": "Домініканський собор", "latitude": 49.842778, "longitude": 24.034167},
    {"order": 4, "title": "Порохова вежа", "latitude": 49.842781, "longitude": 24.035561}
  ]
}
```

## Android verification and limitations

Only test source/resources were added; **no Android product code changed**:
- `app/src/test/java/ua/rytm/app/ui/routes/DemoLvivRouteTest.kt`.
- `app/src/test/resources/routes/first-lviv-route-api.json` (captured actual local API response).

23 focused route JVM/Compose tests passed (0 failures/errors/skips), including 4 new seed
fixture tests. Confirmed DTO/repository compatibility, list click identity, title/description,
no-cover fallback, four ordered stop cards, coordinate/marker bindings, all four native numbered
marker bitmaps, start/next/last-stop behavior and light/dark rendering without a crash.
The map slot in native UI tests is a fixture, **not Google tiles or a running GoogleMap**.
No phone/emulator was attached (`adb devices -l` empty), so live Google map rendering and
phone interaction remain unverified. No live Android-to-server network session was claimed.

Build verification (existing production endpoint unchanged):

```sh
JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64 ./gradlew :app:testDebugUnitTest \
  --tests '*DemoLvivRouteTest' --tests '*TourRouteTest' --tests '*TourRouteUiTest' \
  --tests '*TourRouteVisualTest' :app:testReleaseConfiguration :app:assembleProductionCheck \
  -PDEBUG_BASE_URL=https://rytm-fhw0.onrender.com/ --no-parallel --max-workers=2 --console=plain
```

Build exited 0; original 21 release-origin plus 8 existing Google OAuth assertions passed.
Production-connected APK remains at the standard path; it will not see this **local-only**
seed until the operator seeds production after review. Native screenshots:
`app/build/outputs/demo-route-review/` (Android repo), including light/dark list, detail,
all four stop cards and fixture marker mode. Existing unrelated Google-auth work is preserved.

Backend: all **42 tour_routes tests passed**, process exited 0 after test DB destruction;
`check` passed, route migration consistency passed. No migrations were authored.

## Manual production action (NOT performed)

After reviewing/deploying the command yourself, with the intended production environment
and existing route migrations already applied, run exactly:

```sh
python manage.py seed_demo_route --allow-nonlocal-database
```

The command prints its actual production ID; do not assume it will be 5.
Verify `/api/routes/` and `/api/routes/<printed-id>/`, then open Feed > Маршрути in the
production-connected Android app, select the route, inspect the description/four stops,
start it and check four correctly positioned numbered pins, selection, next/last stop,
back navigation and no-image fallback. Map > Маршрути should open map mode directly.
This task does not seed or connect to Supabase, commit, push or deploy anything.

For additional routes later, use existing Django Admin > Tour routes > Add, fill metadata,
add ordered valid stops and publish. Use a new stable demo_key in a separate reviewed seed
only for additional repeatable fixtures; do not change this seed's identity to repurpose it.

## Final working tree / cleanup

The temporary API server was stopped after HTTP verification; the seeded local route remains. No Docker start/stop was performed. Intended-source credential-pattern audit passed; the captured backend/Android JSON fixtures match byte-for-byte. No secrets, APKs, keystores or local.properties changes were added/staged.

Backend status (4 new files):

```text
?? docs/demo-route.md
?? docs/first-lviv-route-api.json
?? tour_routes/management/commands/seed_demo_route.py
?? tour_routes/test_demo_route.py
```

Android status (2 route-test additions plus the 7 pre-existing Google-auth changes, preserved):

```text
 M app/build.gradle.kts
 M app/src/main/java/ua/rytm/app/ui/profile/GoogleSignIn.kt
 M app/src/main/java/ua/rytm/app/ui/profile/ProfileViewModel.kt
 M app/src/test/java/ua/rytm/app/ui/profile/ProfileViewModelTest.kt
?? GOOGLE_SIGN_IN_DIAGNOSTICS.md
?? app/src/main/java/ua/rytm/app/core/config/GoogleOAuthConfiguration.kt
?? app/src/test/java/ua/rytm/app/ui/profile/GoogleSignInTest.kt
?? app/src/test/java/ua/rytm/app/ui/routes/DemoLvivRouteTest.kt
?? app/src/test/resources/
```

Both `git diff --check` checks passed, both indexes are empty. Nothing committed/pushed/deployed; no production database connection or seed was performed.

## Tourist description update

The seed now supplies fuller stop descriptions in the existing `description` field (no new fields/API/migration).
Source checks: [Opera house](https://lia.lvivcenter.org/en/objects/opera-house/),
[Rynok Square](https://lviv.travel/en/places/attractions/ploshcha-rinok),
[Dominican Church](https://lviv.travel/en/places/attractions/dominikanskii-sobor),
[Powder Tower](https://www.touristinfo.lviv.ua/en/plan-trip/what_to_see/).
Descriptions avoid promises about entry, opening hours, access, ticket prices or live conditions.
Refresh existing seed data only by manually running the same reviewed command; no production write occurs automatically.

## Route source links correction — 2026-11-10

`TourRouteStop.source_url` is now an optional HTTP(S) URL exposed by route-detail serialization
and editable in the existing stop inline. Migration: `0003_tourroutestop_source_url` (not applied
to production). Old stops default to an empty source. No submission fields were added.
The seed supplies the following verified, query-free source pages:

1. Opera: https://lia.lvivcenter.org/en/objects/opera-house/
2. Rynok / Town Hall: https://lviv.travel/en/places/attractions/ploshcha-rinok
3. Dominican: https://lia.lvivcenter.org/en/objects/dominican-church/
4. Powder Tower: https://www.lvivcenter.org/en/spatial/powder-tower-2/

All four pages resolved and matched the landmark on review. The Powder Tower source is the
Center for Urban History's historical interpretation of the tower, not a booking promise.
The captured JSON fixture was extended with these reviewed URL values; these additions are
an expected post-migration/seed example, **not** a newly fetched database response. The local
PostGIS server is currently unavailable, so the migration and new seed were not applied.


## Source review — 2026-11-11

All four existing source URLs were verified again. Opera, Rynok and Dominican stay unchanged.
The Powder Tower's dated walk-event page was replaced with a dedicated official tourism
article: https://lviv.travel/ua/places/attractions/porokhova-vezha .
This reuses the existing source_url field and update_or_create seeding; no new migration.
The fixtures represent the expected seed result, not a new live API capture. No seed was
run against production. Tests now explicitly check four HTTP(S) links and restoration of
source URLs on idempotent reruns.

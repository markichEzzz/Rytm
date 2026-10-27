# Address and unknown-price verification — 2026-10-09

The location-enrichment command now fills untouched empty addresses even when both coordinates
already exist. It requests structured Nominatim address details and stores only a usable road and
optional house number. It does not parse `display_name`, invent house numbers, or overwrite
existing/moderator-cleared addresses. Ukrainian street types normalize conservatively to
`вул.`, `просп.`, `пл.`, and `пров.`; valid ranges such as `24-26` remain intact.

Strict known-venue reuse copies reliable concise addresses from approved Events or candidates with
private geocoded/known-venue address provenance. Unverified pending addresses are excluded.
Address-only enrichment keeps existing coordinates and rejects results more than 75 metres away.
Ambiguous or out-of-bounds results do not enrich the address. Address provenance stays private;
admin lists and forms retain editable addresses and the existing map marker. Safely enriched
addresses satisfy the existing approval requirement.

Missing, unreadable, ambiguous, or unsupported pricing explicitly becomes `unknown` with null
price. Explicit free entry remains `free`/0; reliable numeric prices remain `paid` with the numeric
amount, including existing minimum-price notes. Re-extraction normalizes untouched pricing on
matched unpublished rows and safely repairs legacy null prices with inconsistent statuses.
Moderator edits to either pricing field protect both values, using private markers and historical
Django admin logs. `--force-prices` permits explicit replacement on matched re-extracted entries.
No new migration was needed.

## Validation

- Full backend suite: 272 tests passed.
- JavaScript suite: 4 tests passed (`node ingestion/js_tests/candidate.test.cjs`).
- Existing admin coordinate/marker tests passed in the backend suite.
- Public serializer coverage verifies `price: null`, `price_status: "unknown"`, and no private provenance.
- Live inconsistent null-price statuses: 0; safe untouched pricing repairs: 0.

## Live preview

Command: `python manage.py resolve_candidate_locations --source festrepublic --dry-run`.
All listed candidates had empty addresses before preview. Address provenance for all proposed
addresses is **geocoded**, from structured road/house-number data. Existing coordinate pairs were
preserved; the FESTrepublic coordinate source also benefited from strict known-venue reuse.

| ID | Title | Venue | Proposed address | Latitude | Longitude |
|---|---|---|---|---|---|
| 1 | Squirt: The Las Vegas Show | FESTrepublic | вул. Старознесенська, 24-26 | 49.8508107 | 24.0524873 |
| 2 | Sick Solution | FESTrepublic | вул. Старознесенська, 24-26 | 49.8508107 | 24.0524873 |
| 3 | For Fit Day 2026 | FESTrepublic | вул. Старознесенська, 24-26 | 49.8508107 | 24.0524873 |
| 4 | ODYSSAY | FESTrepublic | вул. Старознесенська, 24-26 | 49.8508107 | 24.0524873 |
| 5 | Курган: сольна програма | FESTrepublic | вул. Старознесенська, 24-26 | 49.8508107 | 24.0524873 |
| 6 | KARNA | FESTrepublic | вул. Старознесенська, 24-26 | 49.8508107 | 24.0524873 |
| 7 | Jamala | FESTrepublic | вул. Старознесенська, 24-26 | 49.8508107 | 24.0524873 |
| 8 | Сергій Жадан. Презентація книжки «Гімни барокових війн» | Драмтеатр ім. Марії Заньковецької | вул. Лесі Українки, 1 | 49.8439958 | 24.0275425 |
| 10 | Tutor’s Forum 2026 | FESTrepublic | вул. Старознесенська, 24-26 | 49.8508107 | 24.0524873 |

Summary: processed 9; resolved 9; coordinates enriched 0; addresses enriched 9;
already complete 0; skipped 0; ambiguous 0; failed 0. No eligible candidates remained unresolved.
Candidate #9 was excluded because it is already approved.

Before/after SHA-256 fingerprints of every row confirmed **no changes** to EventCandidate,
RawSourceItem, LocationLookup, or Event. No live enrichment or pricing repair was persisted.
Android was untouched. Nothing was committed, pushed, or deployed.

## Files changed for this request

- `ingestion/addresses.py`: structured canonical-address normalization.
- `ingestion/provenance.py`: shared private provenance and moderator edit protection.
- `ingestion/geocoding.py`: structured components, legacy cache upgrades, safe address enrichment/reuse.
- `ingestion/management/commands/resolve_candidate_locations.py`: address-only enrichment and diagnostics.
- `ingestion/extraction.py`: explicit address provenance, unknown pricing, safe pricing normalization.
- `ingestion/management/commands/extract_events.py`: pricing counts and explicit `--force-prices`.
- `ingestion/admin.py`: private address source and pricing/address edit markers.
- `ingestion/test_address_enrichment.py`: address safety, cache, command, admin, and approval tests.
- `ingestion/test_unknown_price.py`: extraction and re-extraction pricing regression tests.
- `ingestion/test_resolve_locations_command.py`: verified known-address reuse fixture.
- `README.md`: updated command and price behavior.
- This verification report.

The workspace already contained unrelated changes; these were preserved.

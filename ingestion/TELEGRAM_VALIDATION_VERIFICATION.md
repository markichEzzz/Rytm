# Telegram validation and multisource verification — 2026-10-09

## Root cause

Before normalization, actual configured OpenAI responses for RawSourceItems #2–#6 returned currency-text price strings: `від 300 грн`, `від 100 грн`, `Від 150 грн`, `від 100 грн`, and `від 100 грн`. The schema permitted strings, but `Decimal(price)` rejected these strings. All five failed in ExtractionBatch/ExtractionResult validation, before EventCandidate.full_clean. This was not an address, coordinate, fingerprint, or required price_status failure. Price status is derived locally, rather than returned by this extraction contract.

Currency text now uses the existing price parser, then the existing source-evidence grounding. Unknown/unreadable prices stay unknown/null; explicit free stays free/0. Minimum pricing retains the existing review note. Unreliable/ambiguous or unsupported facts are never invented.

Additional observed failures addressed: curly versus straight quotation marks in title evidence; incorrect Kyiv offset after the October DST change, corrected only when quoted evidence supports the same local wall time; explicitly labelled former dates `(було …)` competing with the new rescheduled date; and single-event paragraphs wrongly triggering multi-entry evidence isolation. Bare weekday dates resolve against publication with an explicit review note. Unlabelled date conflicts remain unresolved.

## Changes in this task

- `ingestion/extractors/contracts.py`: existing price parser reuse, source-backed Kyiv offset normalization, bounded field validation reasons.
- `ingestion/extractors/openai.py`: explicit decimal-price instructions and retention of historical events for local validation.
- `ingestion/extractors/hybrid.py`: quotation normalization and explicit former-date handling.
- `ingestion/extractors/categories.py`: generic opera/comedy performance evidence.
- `ingestion/extractors/parsers.py`: bare weekday support with existing conservative review semantics.
- `ingestion/extraction.py`: explicit missing/past/model-field diagnostics; historical diagnostic fallback when provider omits an event; single-event paragraph handling; validation of unsaved collection previews without writes.
- `ingestion/deduplication.py`: theatre prefix and token-order title matching, plus private management diagnostics for similarity, date, time, venue, geographic distance and target source.
- `ingestion/management/commands/extract_events.py`: source platform, price status, dedup and field reasons in previews.
- `ingestion/test_validation_diagnostics.py` and `ingestion/test_discovery.py`: regression coverage.

## Final requested Telegram extraction dry-run

Command: `python manage.py extract_events --source kudypity_lviv --limit 5 --dry-run`.
Provider: configured OpenAI `gpt-6-luna`. Three candidate-ready upcoming events; no candidates persisted; two raw items have only past event dates.

| Raw item | Title | Normalized start (Kyiv ISO) | Category | Pricing | Candidate-ready | Result |
|---|---|---|---|---|---|---|
| #2 | Крик любові | 2026-11-18T19:00:00+02:00 | performance | paid / 300 грн minimum | yes | valid upcoming candidate |
| #3 | Наталка Полтавка | 2026-10-25T12:00:00+02:00 | performance | paid / 100 грн minimum | yes | valid upcoming candidate |
| #4 | Містична органна музика, від якої мурашки по шкірі | 2026-08-28T19:00:00+03:00 | concert | paid / 150 грн minimum | no | start_time: event start is in the past |
| #5 | НАБУККО | 2026-10-10T17:00:00+03:00 | performance | paid / 100 грн minimum | yes | valid upcoming candidate |
| #6 | Ланч-концерт. Орган та віолончель | 2026-09-05T14:00:00+03:00 | concert | paid / 100 грн minimum | no | start_time: event start is in the past |

All five have no extracted street address, no latitude/longitude, and no end time. These missing fields do not block initial suggestions; approval safety remains unchanged. Candidate identity is generated locally after valid title/start.

- #2 venue: Палац культури залізничників "Рокс". Location preview: failed — No unique match found; select location manually.
- #3 venue: Львівський національний академічний театр опери та балету. Location preview: failed — No unique match found; select location manually.
- #4 venue: Львівський органний зал. Location preview: ambiguous — Ambiguous results; select location/address manually.
- #5 venue: Львівський національний академічний театр опери та балету. Location preview: failed — No unique match found; select location manually.
- #6 venue: Львівський органний зал. Location preview: ambiguous — Ambiguous results; select location/address manually.

#3 also yielded an explicitly historical August 30 occurrence; it receives the past-start reason while the October 25 rescheduled occurrence remains candidate-ready. #4 and #6 mention additional dates without listing them, so no replacements were invented.

## Collection and representative extraction

Requested dispatcher previews ran for both Telegram channels (limit 5) and all 21 enabled Instagram accounts (limit 3). Default Instaloader was blocked and returned safe failures. The existing manually authenticated Playwright adapter was then selected explicitly; no access controls were bypassed. It fetched 20 posts from 10 accounts, with 11 independently reported failures.

| Source | Collection | Representative extraction | Candidate-ready previews | Dedup/location |
|---|---|---|---|---|
| telegram @kudypity_lviv | 5 posts | 5 raw items; dates/prices/categories retained | 3 | Unique against current DB; venues failed/ambiguous location previews |
| telegram @lvivgo_afisha | 5 posts | 2 posts: PET FEST July 25 and DREVO July 26, both past | 0 | Precise past-start rejection; location ambiguous/failed |
| instagram @festrepublic | 3 posts | 17-event calendar plus recruitment non-event | 10 | 9 confirmed existing candidate matches; verified FEST venue resolves |
| instagram @dzyga.gallery | 2 of 3 inspected posts readable | 2 posts: exhibition lacks opening date; concert lacks date/time | 0 | Manual completion; explicit free/paid pricing retained; no location evidence |
| instagram @jamfactory.artcenter | 1 of 3 inspected posts readable | Seminar application announcement; event start/venue not provided | 0 | Review; application deadlines not used as event starts |
| instagram @kotelnya.ruin.place | 2 posts | 2 concerts: missing reliable title/start; second provider timestamp naive | 0 | Review; venue ambiguous |
| instagram @malevich_club | No post/reel links after bounded waits/scrolls | Unavailable | — | Profile header visible, no login/checkpoint UI; no bypass or extra crawling |

Other successful browser sources: @rumbambar.lviv (1), @lollipop_projectsss (1), @lvivart.center (3), @pravdabeer (2), @les_kurbas_theatre (2), @pershyiteatr (3).

Other failures: cult.comedy.hall, leskurbastheatre, urbanhistorycenter, mad_barshouse, streetfood_sogood, zankovetskatheatre, dobre_party were explicitly unavailable according to the page. emily_resort, g.zone_project, split.night.club exposed links but none of the three inspected posts had usable structures. Jam Factory’s two unreadable individual posts were skipped without retries; its remaining post succeeded. No source-specific selectors were added.

## Cross-source deduplication

No actual Instagram/Telegram overlapping event was found in this bounded sample: current Telegram observations concern different or historical events. Live FESTrepublic re-observations nevertheless produced nine confirmed matches to existing candidates, including Sick Solution. These were diagnostics only; no observation links were persisted.

Realistic reconstructed cases in the isolated test database verify Sick Solution / Концерт Sick Solution у Львові; Наталка Полтавка / Вистава «Наталка Полтавка»; and the reordered organ/cello lunch-concert title. For each, an Instagram pending candidate is reused by the Telegram observation, then approval creates one Event, and a subsequent Telegram observation links the same candidate/Event. The three scenarios finish with three logical candidates and three public Events. These are reproductions, not claims of discovered live cross-platform overlaps.

Existing regression cases verify different dates and different titles stay separate; same-title/time disagreement becomes probable for moderator review; ambiguous multiple matches are not automatically linked; generic concert titles are insufficient; moderator-edited pricing survives, while reliable unknown-to-paid enrichment is allowed. Strong matching still requires title + same date + exact start + venue/address/proximity; title formatting alone never triggers a merge.

No false-positive link was observed in the covered live/reconstructed cases. No duplicate public Event was created: verification did not publish anything. This bounded run does not certify that every existing historical public event is duplicate-free.

## Tests and persistence audit

Final full backend suite: **321 tests passed**, Django system check clean. Includes existing Instagram extraction, collection idempotency, source failure isolation, approval safety, moderator protection, unknown pricing and map behavior.

Before/after SHA-256 fingerprints over every row/field (including spatial points) match for SourceAccount, RawSourceItem, EventCandidate, EventSourceObservation, LocationLookup and Event across the entire verification. Final counts: {"ingestion.SourceAccount": 23, "ingestion.RawSourceItem": 6, "ingestion.EventCandidate": 10, "ingestion.EventSourceObservation": 0, "ingestion.LocationLookup": 3, "events.Event": 7}.

No candidate/source/raw/observation/cache/public-event writes during live verification. No Android changes, commits, pushes, deployment, or deferred account/auth/profile migrations/UI.

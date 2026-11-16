# Catalog read endpoints the labelling mode needs (draft issue for EvilFlowersCatalog)

Requested by evilflowers-books-digitalizer#7: the studio labelling mode that produces the hand labels for
knowledge#65. Not filed yet: the agent's attempt to open it in EvilFlowersCatalog was refused by the permission
gate (2026-11-16), so this file holds the text to file.

The studio samples items from the dev catalog read-only (API key with manage rights, GET only). Read on `develop`
709b712 (2026-11-16). Figures, tables and subjects are covered by existing routes (`entries?extraction_status=
available`, `entries/{id}/extraction/regions` with band/p/decision, `.../regions/{id}/image`, `categories`). Three
things have no read route.

## 1. Section text by key (`section_relation`)
The decider edge sweep (EvilFlowersCatalog#190) asks about "Section A: <summary or first paragraphs>\n\nSection B:
…". A labeller must see the same text. Today `GET /api/v1/sections/{key}/related` and `/entries/{id}/chapters`
return the heading only; the `section` locator returns the heading as its quote; `SectionSummary` (apps.assistant)
has no route. The studio's workaround is `entries/{id}/extraction/paragraphs` matched on `Paragraph.section` ==
heading inside `first_page..last_page`: fragile when a heading repeats, and not the text the sweep sends.

**Ask:** `GET /api/v1/sections/{key}/text` (manage, or reader visibility) →
`{key, entry_id, title, first_page, last_page, summary, summary_language, first_paragraphs}`, where
`first_paragraphs` is exactly the string the edge sweep would send (≤ 1500 characters).

## 2. The pairs the edge sweep asks, in its orientation (`section_relation`)
`/sections/{key}/related` is cross-book only (the sweep also types intra-book pairs), drops `hide` pairs, and
renames the relation to the reader's side (`prerequisite` / `next` / `general`). Calibration needs the pairs as
asked (`a_key`, `b_key`, the sweep's direction), unfiltered; otherwise "unrelated" is under-sampled.

**Ask:** `GET /api/v1/decider/section-pairs?typed=any|untyped&band=keep,review,hide,none&limit&page` (manage) →
`{a_key, b_key, a_entry_id, b_entry_id, score}` (plus `relation`, `p`, `band` for stratified sampling; never shown
to a labeller).

## 3. Region key in the admin serializer
`Region.locator_key` exists but `AdminRegion` does not return it. Until it does, the studio builds
`p{page:03d}-{fig|tab}{seq:02d}` (e.g. `p012-fig01`) and stores the region UUID beside it.

**Ask:** add `locator_key` to `AdminRegion` (`entries/{id}/extraction/regions`) so labels join back to regions
across runs.

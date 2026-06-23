# Metadata catalog & cover generator

## Metadata catalog

An Excel sheet provides bibliographic metadata, matched to each book by its
**exact directory name** via a `directory_id` column. This is the reliable join:
of 880 books, 176 have title-slug directories with **no ISBN**, so ISBN alone
can't match everything. The catalog also keeps a secondary ISBN match
(normalizing punctuation and the `_X` check digit, so `978-80-227-…`,
`9788022…` and `807095020_X` all compare equal).

Unmatched books are **not** fatal: they get a de-slugged stub title.

### Draft the sheet for librarians

Don't hand-create the spreadsheet — generate a pre-filled, styled draft with
**notebook 10** (`10_metadata_catalog_draft.ipynb`) or `metadata.draft`. It
seeds one row per book with `directory_id` (the locked join key), `fakulta`,
page count, a derived `ISBN` (for the ~80% of dirs that are ISBNs), and a
guessed `názov` for slug dirs — leaving the rest highlighted for the librarian,
with a Slovak instructions sheet. Send `configs/catalog_template.xlsx` out;
save the completed file back as `configs/catalog.xlsx`.

**ISBN auto-fill.** When the directory name is a valid ISBN, the draft pre-fills
title / authors / year / publisher / place / edition via
`metadata.isbn_lookup.IsbnEnricher`. Providers, tried in order:

1. **STU IPAC** — the university's own ARL/Cosmotron catalogue
   (`kis.cvt.stuba.sk/arl-stu`), queried through its RSS search feed
   (`field=ISBN`). This is authoritative for the corpus (the `OPACID` dir ids
   came from it) and covers the Slovak academic books that global catalogs miss
   — and many foreign titles too.
2. **Open Library** — fallback that fills author/title gaps for foreign books.
3. **Google Books** — available but not in the default list (rate-limits hard
   without an API key).

No API key, stdlib only, cached on disk under `.cache/isbn_lookup`, fully
fail-safe (a genuine miss is cached; a transient/throttle error is not, so it
retries next run). The draft colour-codes the result: **grey** = pre-filled
(don't edit `directory_id`), **green** = auto-filled from ISBN (please verify),
**yellow** = empty (please fill). The `zdroj_metadat` column records the
provenance (`STU IPAC` / `Open Library`) of each auto-filled row.

### Configure (`[metadata]`)

The column headers below match `metadata.draft.HEADERS` (the draft the librarians
fill), so the defaults work out of the box:

```toml
[metadata]
enabled = true
excel_path = "configs/catalog.xlsx"
sheet = "katalog"
key_field = "isbn"           # secondary match; the primary join is `directory`

[metadata.columns]           # model field -> spreadsheet column header
directory = "directory_id"   # the exact book directory name (primary join key)
title = "názov"
subtitle = "podnázov"
authors = "autori"           # split on ; / & and Slovak "a"
year = "rok_vydania"         # a 4-digit year is extracted from any text
publisher = "vydavateľ"
isbn = "ISBN"
faculty = "fakulta"
language = "jazyk"

[metadata.faculty_names]     # source key -> human faculty name (covers + stubs)
fad = "Fakulta architektúry a dizajnu STU"
```

Validate the mapping against the real directories before a batch:

```bash
python -m evilflowers_books_digitalizer validate-catalog
```

The matched fields flow into the PDF XMP (`EnrichPdfMetadata`) and the cover.

## Covers

`GenerateCover` writes a standalone `<slug>.cover.jpg` per book (the PDF is left
untouched). It is **fail-safe** — any error is logged, never fatal.

Two sources, controlled by `[cover].source`:

1. **Real OPAC cover** (default first) — fetched by ISBN from
   `obalkyknih.cz/api/cover` (the cover database the STU OPAC itself uses),
   `covers.opac.fetch_opac_cover`. ~70 % of ISBN-bearing books have a real cover
   (≈170×240, same ~0.71 aspect as the generated ones, so a catalog grid stays
   consistent). "No cover" placeholders are rejected by a `min_px` size gate.
2. **Generated cover** (fallback) — a styled cover rendered from metadata for
   books with no ISBN or no real cover, so every book ends up with a thumbnail.

`ctx.metadata["cover_source"]` records which was used (`opac` / `generated`).

### Configure (`[cover]`)

```toml
[cover]
enabled = true
source = "opac_then_generated"  # | "opac" (real only) | "generated" (styled only)
min_px = 80                      # reject obalkyknih "no cover" placeholders
template = "banner"             # generated style: "banner" (default) | "minimal"
format = "JPEG"                 # or "PNG" (generated covers)
quality = 88
# size = [1200, 1697]           # generated cover size (~ISO 1:√2)

[cover.palette.fad]             # optional per-faculty overrides (hex)
accent = "#A8442A"
bg = "#F5F0EA"
text = "#2B1E1A"
```

Fonts are bundled (DejaVu Serif/Sans — full Slovak diacritic coverage,
deterministic on the Mac dev box and the Linux VM). Point `[cover].fonts` at
other TTFs to rebrand.

### Add a template

Subclass `CoverTemplate` in `covers/templates.py`, implement `render(spec,
palette, size, fonts) -> PIL.Image`, and register it in `TEMPLATES`. No other
code changes — select it via `[cover].template`.

### Preview

```bash
python -m evilflowers_books_digitalizer preview-cover svf <book_id> \
  --template banner --out /tmp/cover.jpg
```

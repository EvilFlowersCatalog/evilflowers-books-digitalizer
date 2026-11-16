# Metadata

A book's bibliographic metadata (IA `_meta.xml` fields: title, creator, publisher, date, ISBN,
language, …) comes from three places, in this order of trust:

1. what an operator saved in the book's metadata form (always wins);
2. the project's **metadata providers** (Excel, OPAC, OCR colophon), consulted when the book is
   rendered;
3. the **title-page prefill**, which only suggests and is never imported unconfirmed
   ([studio guide](studio.md#title-page-prefill-15)).

The model is `ItemMetadata` (`studio/metadata.py`): IA field names, repeatable fields as lists,
MARC language codes (`slo`, not `slk`), custom fields prefixed `evilflowers_`.

The covers generator of the batch pipeline (`covers/`, `[cover]`, `preview-cover`) was removed
in evilflowers-books-digitalizer#6. The old page is in
[Archive](archive/metadata_and_covers.md).

## When the lookup runs

`render/prepare` (the orchestrator's step before render-service) calls the project's
`CompositeMetadata` with the book and its OCR text. It runs after OCR because two providers need
the text. The stored record is merged **over** the lookup: `ItemMetadata.merge` fills only empty
fields, so an operator's edit is never replaced. The result is saved to `books.item_metadata`
and goes into the IA `_meta.xml` and the catalog entry.

## Configure the providers

```toml
[projects.stu.metadata]
providers = ["excel", "opac", "ocr"]     # tried in order; earlier = more trusted
excel_path = "catalog.xlsx"              # relative to the config file's directory
key_field = "isbn"                       # secondary match; the primary join is the directory
opac_providers = ["stu_opac", "openlibrary"]   # ISBN lookup order; "google" is also available

[projects.stu.metadata.columns]          # canonical field -> spreadsheet column header
directory = "directory_id"
title = "title"
authors = "authors"
year = "year"
publisher = "publisher"
isbn = "isbn"
faculty = "faculty"
language = "language"
```

`providers` defaults to `["excel", "opac"]` when `excel_path` is set, else `["opac"]`. The names
are `excel`, `opac` and `ocr` (alias `ocr-extract`); more are registered in
`studio/adapters/registry.py`. Providers are merged in order and a field is never overwritten.
A provider that fails is logged and skipped. If no provider gives a language, the project's
`language` is used. The shipped k8s config uses only `opac`: the Excel file needs an extra
mount.

### Excel (`excel`)

A spreadsheet matched to each book by its **exact directory name** through the `directory`
column. This is the reliable join: many books are filed under title slugs with no ISBN. A
secondary match by `key_field` (ISBN, normalised for punctuation and a trailing `_X` check
digit) exists. The first sheet is read. The canonical fields a column can map to are `title`,
`subtitle`, `authors`, `year`, `publisher`, `isbn`, `faculty`, `language`, `catalog`
(`metadata/catalog.py`, `DEFAULT_COLUMNS`). An unmatched book yields nothing: a de-slugged stub
title never outranks a real source.

The `metadata.draft` generator and notebook 10 that produced the librarians' draft sheet were
part of the batch pipeline and are gone.

### OPAC and Open Library (`opac`)

The ISBN comes from the directory name, or else from the OCR text of the front and back matter.
It is looked up (`metadata/isbn_lookup.py`, `IsbnEnricher`) in the providers of
`opac_providers`, in order:

1. **STU IPAC** (`stu_opac`): the university's ARL/Cosmotron catalogue, through its RSS search
   feed. Authoritative for the corpus.
2. **Open Library** (`openlibrary`): fills foreign titles.
3. **Google Books** (`google`): available, not in the default list (it rate-limits hard
   without an API key).

Stdlib only, no API key, cached on disk under `<cache_dir>/isbn`, fail-safe: a genuine miss is
cached, a transient error is not.

### OCR colophon (`ocr`)

Many books have no Excel row, yet their colophon on the title-page verso or the last page prints
the ISBN, year and publisher. `metadata/ocr_extract.py` (stdlib only) reads them from the OCR
text of the first 8 and last 4 pages:

- **ISBN**-10 or -13 in tolerant forms, tolerating OCR noise (`O` for `0`, `l` or `I` for `1`
  inside digit runs); every ISBN is checksum-validated, and an ISBN-10 becomes an ISBN-13.
- **Year**: a `©`, `(c)` or `Copyright` year first, then an imprint such as `Vydanie` or
  `Vydavateľstvo … YYYY`.
- **Publisher** (best effort): `Vydavateľstvo …`, `Nakladateľstvo …`, `Edičné stredisko …`,
  `Vydal …`.

The `opac` provider uses the same extraction on its own to find an ISBN when the directory name
is not one.

## What the catalog entry gets

At publication, `catalog/mapping.py` and `studio/adapters/catalog/payload.py` map the metadata
to a catalog entry: title, `language_code` (ISO 639-1, from the MARC code), authors (split into
name and surname), ISBN, `published_at` (the year), publisher, and the distribution PDF and EPUB
as acquisitions. See [studio guide, section 6](studio.md#6-approve-and-publish).

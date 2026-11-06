# Importing into EvilFlowers Catalog

Digitized books are published into an
[EvilFlowers Catalog](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog)
instance (OPDS 1.2 / 2.0 server). The `catalog` layer is the bridge: it turns a
produced book into a catalog **Entry** with a **PDF acquisition** (the
distribution copy) and a cover image.

It mirrors the `metadata` and `covers` layers and adds two decoupled stages:

1. **Manifest** — digitization writes a `<slug>.entry.json` sidecar next to each
   distribution PDF (`catalog/mapping.py` + the `WriteCatalogManifest` pipeline
   step). This is a durable, reviewable, re-runnable description of the entry,
   including where each metadata field came from (`provenance`).
2. **Publish** — `publish-book`, `publish-catalog` or `run-book … --publish` push
   manifests to a running catalog (`catalog/publisher.py`). All HTTP goes through
   [`evilflowers-protocol`](https://github.com/EvilFlowersCatalog/evilflowers-protocol-python)'s
   `CatalogClient` (auth, retries, typed errors, streamed uploads).

Keeping them separate means an import can be replayed, audited, or handed to a
different tool without re-running OCR.

## Where the files are

Paths come from `pipeline/layout.py` and follow `[render] layout`:

| Layout | Distribution PDF (uploaded) | Sidecars (`.entry.json`, `.cover.jpg`, `.txt`) | Archival PDF (never uploaded) |
| --- | --- | --- | --- |
| `split` | `<output>/distribution/<src>/<slug>.pdf` | next to it | `<output>/archival/<src>/<slug>.pdf` |
| `flat` (code default) | `<output>/<src>/<slug>.distribution.pdf` | next to it | `<output>/<src>/<slug>.archival.pdf` |

`<slug>` is `<src>_<book_id>`.

## What maps to what

| Digitizer (`BookMetadata` / artifact) | Catalog `Entry` field |
| --- | --- |
| `title`                        | `title` *(required, cut to 255 chars)* |
| `language` / detected OCR lang | `language_code` — **ISO 639-1** (`sk`, `cs`, `en`, …; alpha3 only when no alpha2 exists). `slk`/`slo` and Tesseract combos like `slk+eng` become `sk`. Falls back to `[catalog].default_language` |
| `authors` (`"Surname Given"`)  | `authors` → `[{name, surname}]` (split per `[catalog].author_name_order`). The catalog requires both parts, so a one-word author (e.g. a corporate "STU") is left out of the entry with a warning |
| `isbn`                         | `identifiers.isbn` (compacted: no hyphens) |
| `year`                         | `published_at` *(partial date — year alone)* |
| `publisher`                    | `publisher` |
| the distribution PDF           | an `Acquisition` (`application/pdf`, `[catalog].relation`) |
| the cover image                | the entry `image` (a `data:` URI; skipped over 5 MB) |

## Which catalog

One catalog per faculty (`url_name` = `svf`, `sjf`, `fad`, `fei`, `mtf`). The
target is chosen per book, first match wins:

1. the per-book `cieľový_katalog` column in `catalog.xlsx` (`[metadata.columns].catalog`);
2. the **source key**, when `[catalog].by_source = true`;
3. `[catalog].catalog` (a UUID or `url_name`).

The target is re-resolved at publish time from the current config, so changing
`[catalog]` needs no manifest rebuild.

## Configuration

```toml
[catalog]
enabled      = true
base_url     = "https://dev.evilflowers.elvira.stuba.sk/api/v1"  # no trailing slash
by_source    = true                     # svf books -> catalog "svf", …
catalog      = ""                       # fallback target (UUID or url_name)
api_key_env  = "EVILFLOWERS_API_KEY"    # env var holding the Bearer token
relation     = "open-access"
default_language  = "sk"
author_name_order = "given_first"       # "surname_first" for "Novák Ján"

[catalog.entry_config]                  # merged into the entry's EntryConfig
evilflowers_ocr_enabled    = false
evilflowers_share_enabled  = true
evilflowers_metadata_fetch = true       # the catalog fetches a BibTeX citation by ISBN
```

The API key is a long-lived **Bearer JWT** issued by the catalog
(`POST /api/v1/api_keys`). Keep it out of the repo — set it in the environment
(`EVILFLOWERS_API_KEY`, or the name in `api_key_env`). Check it before a run:

```bash
evilflowers-digitalizer check --catalog   # token set, ping OK, every target catalog resolves
```

## The publish flow (per book)

1. **Target** — resolve the catalog (above).
2. **Resume first** — if the report's last row for the book holds an `entry_id`
   with status `entry_created` (the run stopped before uploading) or `error` (the
   upload failed), fetch that entry: if it already has a PDF (a timed-out upload
   that landed) record `published`; otherwise go to 5.
3. **Create** — `create_entry`, then **immediately** append
   `{status: "entry_created", entry_id, catalog_id}` to the report.
4. **On 409** (same title, ISBN or DOI already in that catalog) look the title up
   and fetch each match:
   - same ISBN (or neither has one and the title matches exactly) **and no
     acquisition** → it is ours from an interrupted run: adopt its `entry_id` → 5;
   - same book **with** an acquisition → `skipped` (with the `entry_id`);
   - anything else → `conflict`. A PDF is never attached to somebody else's entry.
5. **Upload** the distribution PDF → `{status: "published", entry_id, acquisition_id, bytes, duration_ms}`.

Report statuses in `<output>/publish_report_<src>.jsonl` (append-only; the last row
per book wins): `entry_created`, `published`, `skipped`, `conflict`, `error`.
`published`/`skipped` books are not touched on the next run; `conflict` and
`error` books are retried. `conflict` needs a human (rename, fix the ISBN, or
set `cieľový_katalog`) but does not fail the command; `error` exits 1.

## Usage

```bash
# 1. (optional) (re)write entry manifests from the Excel + artifacts
evilflowers-digitalizer export-manifests            # all sources
evilflowers-digitalizer export-manifests svf        # one faculty

# 2. dry run — build + validate every payload, write missing manifests; no HTTP
evilflowers-digitalizer publish-book svf <ID> <ID2> --dry-run
evilflowers-digitalizer publish-catalog --dry-run

# 3. publish (resumable; never duplicates an entry)
export EVILFLOWERS_API_KEY="…"
evilflowers-digitalizer publish-book svf CVI_OPACID_SVF_9788022750462
evilflowers-digitalizer publish-catalog svf --limit 10
evilflowers-digitalizer publish-catalog             # all configured faculties

# digitize and publish each book as soon as it is done
evilflowers-digitalizer run-book svf <ID1> <ID2> --publish
evilflowers-digitalizer run-source svf --publish

# ignore earlier published/skipped rows (the 409 policy above still applies)
evilflowers-digitalizer publish-catalog --force
```

The pipeline-written manifest is used as-is when present (it carries the
OCR/OPAC enrichment, see [metadata_and_covers.md](metadata_and_covers.md)); a
missing one is rebuilt from the Excel catalog + on-disk artifacts, so publishing
works on output produced before this layer existed. `export-manifests` always
rebuilds, which drops the enrichment.

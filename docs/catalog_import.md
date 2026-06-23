# Importing into EvilFlowers Catalog

Digitized books are published into an
[EvilFlowers Catalog](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog)
instance (OPDS 1.2 / 2.0 server). The `catalog` layer is the bridge: it turns a
produced book into a catalog **Entry** with an **open-access PDF acquisition**
and a cover image.

It mirrors the `metadata` and `covers` layers and adds two decoupled stages:

1. **Manifest** — digitization writes a `<slug>.entry.json` sidecar next to each
   PDF (`catalog/mapping.py` + the `WriteCatalogManifest` pipeline step). This is
   a durable, reviewable, re-runnable description of the catalog entry.
2. **Publish** — `publish-catalog` reads the manifests and pushes them to a
   running catalog over its REST API (`catalog/client.py`, `catalog/publisher.py`).

Keeping them separate means an import can be replayed, audited, or handed to a
different tool without re-running OCR.

## What maps to what

| Digitizer (`BookMetadata` / artifact) | Catalog `Entry` field |
| --- | --- |
| `title`                       | `title` *(required)* |
| `language` / detected OCR lang | `language_code` *(alpha2/alpha3, required; falls back to `[catalog].default_language`)* |
| `authors` (`"Surname Given"`)  | `authors` → `[{name, surname}]` (split per `[catalog].author_name_order`) |
| `isbn`                        | `identifiers.isbn` *(allowed keys: `isbn`, `google`, `doi`)* |
| `year`                        | `published_at` *(partial date — year alone)* |
| `publisher`                   | `publisher` |
| the PDF                       | an `open-access` `Acquisition` (`application/pdf`) |
| the cover image               | the entry `image` (base64, server thumbnails it) |

The catalog rejects a duplicate (same title / ISBN / DOI in the catalog) with
**HTTP 409**, which we record as `skipped` — so publishing is **idempotent**.

## Configuration

All books land in **one catalog** (`[catalog].catalog`, a UUID or `url_name`
slug). For future per-book/per-faculty routing, a `cieľový_katalog` column in
`catalog.xlsx` overrides the default when set; otherwise every faculty uses the
configured catalog.

```toml
# configs/pipeline.toml
[catalog]
enabled      = true
base_url     = "https://elvira.digital/api/v1"   # REST API root, no trailing slash
catalog      = "stu-books"                        # UUID or url_name — target for every book
api_key_env  = "EVILFLOWERS_API_KEY"              # long-lived Bearer JWT, read from env
relation     = "open-access"
default_language  = "slk"
author_name_order = "surname_first"               # catalog.xlsx uses "Novák Ján"

[catalog.entry_config]                            # merged into the entry's EntryConfig
evilflowers_ocr_enabled = false
evilflowers_share_enabled = true
```

The API key is a long-lived **Bearer JWT** issued by the catalog
(`POST /api/v1/api_keys`). Keep it out of the repo — set `EVILFLOWERS_API_KEY`
in the environment (see `deploy/.env.example`).

## Usage

```bash
# 1. (optional) (re)write entry manifests for already-produced books
evilflowers-digitalizer export-manifests            # all sources
evilflowers-digitalizer export-manifests svf        # one faculty

# 2. dry run — build manifests and report what would be sent, no network
evilflowers-digitalizer publish-catalog --dry-run

# 3. publish (idempotent: re-runs skip books already in the report / 409s)
export EVILFLOWERS_API_KEY="…"
evilflowers-digitalizer publish-catalog             # all configured faculties
evilflowers-digitalizer publish-catalog svf --limit 10
evilflowers-digitalizer publish-book svf CVI_OPACID_SVF_9788022750462

# re-publish books already recorded as done
evilflowers-digitalizer publish-catalog --force
```

Each run appends to `output/publish_report_<source>.jsonl` (one row per book:
`published` / `skipped` / `error`), the same resumable pattern as the digitizer's
`batch_report_*.jsonl`. Books already recorded as `published`/`skipped` are
skipped on the next run.

Manifests are rebuilt on the fly from the Excel catalog + on-disk artifacts when
a sidecar is missing, so `publish-catalog` works on output produced before this
layer existed.

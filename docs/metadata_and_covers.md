# Metadata catalog & cover generator

## Metadata catalog

An Excel sheet provides bibliographic metadata, matched to each book by its
**directory name**. Directory names look like `CVI_OPACID_FA_9788022750462`,
`CVI_OPACID_FA_807095020_X` (id + `X` check digit), `FEI_9788089422012`, or a
title slug. The catalog:

1. extracts the id token (strips `CVI_OPACID_` + the faculty prefix),
2. normalizes it (alphanumerics only, upper-cased — so `978-80-227-…`,
   `9788022…` and `807095020_X` all compare equal),
3. matches it against the configured key column (usually ISBN).

Unmatched books are **not** fatal: they get a de-slugged stub title.

### Configure (`[metadata]`)

```toml
[metadata]
enabled = true
excel_path = "configs/catalog.xlsx"
sheet = 0
key_field = "isbn"           # which canonical field matches the directory id

[metadata.columns]           # canonical field -> spreadsheet column header
title = "Názov"
authors = "Autor"            # split on ; / & and Slovak "a"
year = "Rok"                 # a 4-digit year is extracted from any text
publisher = "Vydavateľ"
isbn = "ISBN"
faculty = "Fakulta"
language = "Jazyk"

[metadata.faculty_names]     # source key -> human faculty name (covers + stubs)
fad = "Fakulta architektúry a dizajnu STU"
```

Validate the mapping against the real directories before a batch:

```bash
python -m evilflowers_books_digitalizer validate-catalog
```

The matched fields flow into the PDF XMP (`EnrichPdfMetadata`) and the cover.

## Cover generator

`GenerateCover` renders a standalone `<slug>.cover.jpg` per book (the PDF is left
untouched). It is **fail-safe** — a render error is logged, never fatal.

### Configure (`[cover]`)

```toml
[cover]
enabled = true
template = "banner"          # "banner" (faculty band, default) | "minimal"
format = "JPEG"              # or "PNG"
quality = 88
# size = [1200, 1697]        # ~ISO 1:√2 book proportion

[cover.palette.fad]          # optional per-faculty overrides (hex)
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

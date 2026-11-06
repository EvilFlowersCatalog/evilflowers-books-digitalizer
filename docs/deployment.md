# Deployment

The batch is a single run-to-completion process — **no orchestrator, no
database, no server**. Two ways to run it:

- **Kubernetes Job** (k3s): the image runs `evilflowers-digitalizer …` as an
  arbitrary non-root uid, configured by a ConfigMap and environment variables.
  The Ansible role `digitalizer` (evilflowers-knowledge repo) is written against
  the contract below.
- **VM** (screen / systemd / Docker Compose): see
  **[../deploy/README.md](https://github.com/EvilFlowersCatalog/evilflowers-books-digitalizer/blob/master/deploy/README.md)**.

## Runtime contract

### Environment variables

| Variable | Meaning |
| --- | --- |
| `EVILFLOWERS_DIGITALIZER_CONFIG` | Path to `pipeline.toml`. Precedence: `--config` > this variable > `configs/pipeline.toml` of a checkout. The image sets it to `/app/configs/pipeline.toml`. |
| `EVILFLOWERS_LOG_FORMAT` | `text` (default) or `json`. |
| `EVILFLOWERS_LOG_LEVEL` | `DEBUG`, `INFO` (default), `WARN`/`WARNING`, `ERROR`, `CRITICAL`. |
| `EVILFLOWERS_API_KEY` | Catalog Bearer token; the variable name is `[catalog].api_key_env`. |

**Relative paths** in the TOML (`[paths]`, `[metadata].excel_path`,
`[source].root`) resolve against the **directory of the config file**. Absolute
paths are used as given. (The directory is taken as written, not through
symlinks, so a ConfigMap mount resolves against its mount point.)

### Logs

Logs go to **stderr**; command results (JSON rows/summaries) go to **stdout**.
With `EVILFLOWERS_LOG_FORMAT=json`, every line is one JSON object:

```json
{"time": "2026-11-06T17:42:03.120Z", "level": "INFO", "logger": "evilflowers_books_digitalizer.pipeline.base",
 "msg": "step ocr              done in  812.4s (svf_CVI_OPACID_SVF_9788022750462)",
 "source": "svf", "book_id": "CVI_OPACID_SVF_9788022750462", "slug": "svf_CVI_OPACID_SVF_9788022750462",
 "step": "ocr", "duration_ms": 812431}
```

- Always: `time` (RFC 3339, UTC), `level` (`DEBUG|INFO|WARN|ERROR|CRITICAL`), `logger`, `msg`.
- When known: `source`, `book_id`, `slug`, `step`, `duration_ms`, `status`, `entry_id`.
- On exceptions: `exc_type` and `exc` (the traceback).

`source`/`book_id`/`slug` are set for everything logged while a book is
processed, `step` for everything inside a pipeline step (`publish` while
publishing). tqdm progress bars are switched off in JSON mode. Long runs also
write the same lines to `<output>/logs/digitizer.log`.

### Commands and exit codes

| Command | Exit code |
| --- | --- |
| `check [--catalog]` | 0 if every check passes, else 1 |
| `list SOURCE [--limit N]` | 0 |
| `run-book SOURCE ID [ID …] [--publish] [--jobs N] [--keep-cache]` | 1 if any book errored (digitizing or publishing) |
| `run-source SOURCE [--limit N] [--publish]` | 1 if any book errored |
| `publish-book SOURCE ID [ID …] [--dry-run] [--force]` | 1 on any error |
| `publish-catalog [SOURCE] [--limit N] [--force] [--dry-run]` | 1 on any error |

`run-book` processes the ids one after another; finished books return `skipped`
(the distribution PDF exists), so a restarted Job resumes. With `--publish`
every book that succeeded — or was already done — is published right after it
finishes. A catalog `conflict` (409 from a different book) is reported but is
not an error. See [catalog_import.md](catalog_import.md).

### Preflight: `check`

One line per check, `OK` or `FAIL`:

- every `[orchestration].sources` entry: the source directory is readable;
- `output_dir` and `cache_dir` are writable (a temp file is created and deleted);
- free space on `cache_dir` ≥ `[orchestration].min_free_gb`;
- binaries on `PATH`: `tesseract` (with `slk`), `[scantailor].binary`, `jbig2`,
  `jpegoptim`, `qpdf`, and `recode_pdf`;
- with `--catalog`: the token is set, the API answers (`GET /users/me`), and
  every target catalog (per source, plus Excel overrides) resolves.

### Kubernetes Job shape

- Runs as an arbitrary non-root uid/gid (the owner of the scans) with
  `HOME=/tmp`; `/tmp` is an emptyDir. Nothing writes under `/app` or `/opt`.
- Mounts: `/data/raw-scans` (read-only), `/data/output` and `/data/cache`
  (read-write), `/data/configs` (read-only; e.g. `catalog.xlsx`),
  `/etc/digitalizer/pipeline.toml` (ConfigMap).
- `command: ["evilflowers-digitalizer"]`, `args` such as
  `["run-book", "svf", "<ID1>", "<ID2>", "--publish"]`.

### The image

`ghcr.io/evilflowerscatalog/evilflowers-books-digitalizer` (tags `master`,
`latest`, `sha-…`), built by `.github/workflows/docker.yml`:

- `evilflowers-digitalizer` on `PATH` (`/opt/venv/bin`) for any uid;
- default `USER 10001`, `HOME=/tmp`,
  `EVILFLOWERS_DIGITALIZER_CONFIG=/app/configs/pipeline.toml`;
- the venv, DocRes, tessdata and the bundled fonts/logos are world-readable;
- `evilflowers-protocol` is installed from its public git repository at the
  commit in the `PROTOCOL_REF` build argument.

## Configure `pipeline.toml`

- `[source] backend = "filesystem"`, `root`, and the per-faculty `[source.paths]`.
- `[paths] output_dir` on durable storage, `cache_dir` on fast local scratch.
- `[metadata]` — `enabled` + `excel_path` (the librarian-completed file), or
  `enabled = false` to rely on OCR + OPAC enrichment alone.
- `[enrich]` — metadata from the OCR text + ISBN lookup; see
  [metadata_and_covers.md](metadata_and_covers.md).
- `[cover] source = "opac_then_generated"`.
- `[orchestration]` — `max_parallel_books × ocr_jobs ≤ CPU cores`,
  `min_free_gb` set to a safe floor for `cache_dir`'s disk.
- `[catalog]` (to import into the EvilFlowers Catalog) — `enabled`, `base_url`,
  `by_source` / `catalog`, and the token in the environment. See
  [catalog_import.md](catalog_import.md).

## Toolchain

The engine shells out to `scantailor-deviant-cli`, `tesseract` (+ `slk` `ces`
`eng` …), `recode_pdf`, `jbig2enc`, `jpegoptim` and `qpdf`. Either install them
natively, or use the Docker image, which bakes them all in. `check` verifies them.

## Go-live sequence

1. `check` (and `check --catalog`) — mounts, permissions, disk, binaries, token.
2. `validate-catalog` — confirm the Excel matches the real directories (if used).
3. Pilot: `run-book svf <ID> --publish` on a few ids, then `stats`, and eyeball
   a few PDFs/covers and the catalog entries.
4. Start the full run (`run-source` per faculty, or `run-corpus`).
5. Watch with `monitor` / the JSON logs; re-run any time to resume.
6. Import anything not published inline: `publish-catalog --dry-run`, then
   `publish-catalog`. Hand off results with `stats --export html` / `--export csv`.

See [operations.md](operations.md) for monitoring detail, disk handling, and the
troubleshooting table.

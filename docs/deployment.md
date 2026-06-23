# Deployment (production VM)

The batch is a single long-running process — **no orchestrator, no database, no
server**. The mechanics of running it (screen / systemd / Docker) and monitoring
it live with the `monitor` TUI to `stats` and logs, are in
**[../deploy/README.md](../deploy/README.md)**. This page is the production
configuration checklist.

## Configure `configs/pipeline.toml`

- `[source] backend = "filesystem"`, `root = "/mnt/digital-library/raw-scans"`,
  and the per-faculty `[source.paths]`.
- `[paths] output_dir` on durable storage, `cache_dir` on fast local scratch.
- `[metadata] excel_path = "configs/catalog.xlsx"` (the librarian-completed file).
- `[cover] source = "opac_then_generated"`.
- `[orchestration]` — `max_parallel_books × ocr_jobs ≤ CPU cores`,
  `min_free_gb` set to a safe floor for `cache_dir`'s disk.
- `[catalog]` (optional, to import into the EvilFlowers Catalog) — `enabled`,
  `base_url`, the target `catalog`, and `EVILFLOWERS_API_KEY` in the environment.
  See [catalog_import.md](catalog_import.md).

## Toolchain

The engine shells out to `scantailor-deviant-cli`, `tesseract` (+ `slk` `ces`
`eng` …), `recode_pdf`, and `jbig2enc`. Either install them natively, or run the
batch inside the project Docker image, which bakes them all in
(`docker compose -f deploy/docker-compose.yml`).

## Go-live sequence

1. `validate-catalog` — confirm the Excel matches the real directories.
2. Pilot one faculty: `run-source svf --limit 20`, then `stats`, and eyeball a
   few PDFs/covers.
3. Start the full run (`run-corpus`) under screen / systemd / Docker
   ([deploy/README.md](../deploy/README.md)).
4. Watch with `monitor`; offload `output/` periodically; re-run any time to
   resume (finished books are skipped).
5. Import into the catalog (if `[catalog]` is on): `publish-catalog --dry-run`,
   then `publish-catalog` (idempotent; re-run to resume). Hand off results with
   `stats --export html` / `--export csv`.

See [operations.md](operations.md) for monitoring detail, disk handling, and the
troubleshooting table.

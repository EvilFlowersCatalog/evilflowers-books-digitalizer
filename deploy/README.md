# Deploy & run the batch

No orchestrator, no database, no server — the batch is a run-to-completion
process that writes resumable JSONL reports and a log. On k3s it runs as a
Kubernetes Job (Ansible role `digitalizer` in the evilflowers-knowledge repo);
on a VM pick screen, systemd or Docker Compose below. The runtime contract
(env vars, commands, exit codes, JSON log shape) is in
[docs/deployment.md](../docs/deployment.md).

## Configuration and environment

The CLI is `evilflowers-digitalizer` (same as `python -m evilflowers_books_digitalizer`).
No `--config` is needed when the environment names the file:

| Variable | Meaning |
| --- | --- |
| `EVILFLOWERS_DIGITALIZER_CONFIG` | `pipeline.toml` to use (`--config` overrides it; the image defaults to `/app/configs/pipeline.toml`) |
| `EVILFLOWERS_LOG_FORMAT` | `text` (default) or `json` (one object per line on stderr) |
| `EVILFLOWERS_LOG_LEVEL` | `INFO` (default), `DEBUG`, `WARN`, `ERROR` |
| `EVILFLOWERS_API_KEY` | catalog token (name configurable via `[catalog].api_key_env`) |

Relative paths inside the config resolve against **the config file's
directory** — `configs/pipeline.toml` uses `../output` and `../.cache`, i.e. the
checkout's `output/` and `.cache/`. Use absolute paths on servers.

## Prerequisites on the VM

- Scans mounted at `/mnt/digital-library/raw-scans` (read-only is fine).
- The toolchain: `scantailor-deviant-cli`, `tesseract` (+ `slk` `ces` `eng` …),
  `recode_pdf`, `jbig2enc`, `jpegoptim`, `qpdf` — install natively, or use the
  Docker image (it bakes them in).
- A config (`configs/pipeline.vm.toml` is the VM template) with the filesystem
  source, `output_dir` on durable storage, `cache_dir` on fast scratch, and the
  `[enrich]` / `[catalog]` blocks set as needed.
- Tune `[orchestration] max_parallel_books × ocr_jobs ≤ CPU cores`.

Then preflight:

```bash
export EVILFLOWERS_DIGITALIZER_CONFIG=$PWD/configs/pipeline.vm.toml
evilflowers-digitalizer check              # mounts, writable dirs, disk, binaries
evilflowers-digitalizer check --catalog    # + token, API reachability, target catalogs
```

## Option A — screen / tmux (simplest)

```bash
screen -S digitize        # or: tmux new -s digitize
evilflowers-digitalizer run-corpus
#   Ctrl-A D to detach (screen) / Ctrl-B D (tmux); reattach: screen -r digitize
```

Logs stream to stderr **and** to `<output>/logs/digitizer.log`.

## Option B — systemd (survives reboots, auto-restart on crash)

```bash
sudo cp deploy/evilflowers-digitalizer.service /etc/systemd/system/
# edit WorkingDirectory / User / venv path / EVILFLOWERS_DIGITALIZER_CONFIG first
sudo systemctl daemon-reload
sudo systemctl start evilflowers-digitalizer
journalctl -u evilflowers-digitalizer -f
```

A crash auto-restarts and skips already-finished books; a clean finish stops.

## Option C — Docker Compose

```bash
cd deploy && cp .env.example .env      # RAW_SCANS_MOUNT / OUTPUT_DIR / CACHE_DIR / uid
docker compose -f docker-compose.yml build
docker compose -f docker-compose.yml run --rm digitizer evilflowers-digitalizer check
docker compose -f docker-compose.yml up -d digitizer
docker compose -f docker-compose.yml logs -f digitizer
docker compose -f docker-compose.yml run --rm publish      # one-shot catalog import
```

The container runs as `DIGITALIZER_UID:DIGITALIZER_GID` (default `10001:0`),
which must be able to write `OUTPUT_DIR` and `CACHE_DIR`.

Plain `docker run` works the same way — any non-root uid, config by env var:

```bash
docker run --rm --user 12345:12345 \
  -v /mnt/digital-library/raw-scans:/data/raw-scans:ro \
  -v /srv/digitalizer/output:/data/output -v /srv/digitalizer/cache:/data/cache \
  -v /srv/digitalizer/pipeline.toml:/etc/digitalizer/pipeline.toml:ro \
  -e EVILFLOWERS_DIGITALIZER_CONFIG=/etc/digitalizer/pipeline.toml \
  -e EVILFLOWERS_LOG_FORMAT=json -e EVILFLOWERS_API_KEY \
  ghcr.io/evilflowerscatalog/evilflowers-books-digitalizer:latest \
  evilflowers-digitalizer run-book svf <ID1> <ID2> --publish
```

## Monitoring

```bash
# live TUI dashboard (per-source progress bars, throughput, ETA, output size)
evilflowers-digitalizer monitor
#   Docker:  docker compose -f deploy/docker-compose.yml exec digitizer evilflowers-digitalizer monitor

# one-shot summary (good for cron/Slack)
evilflowers-digitalizer stats

# raw logs / reports
tail -f output/logs/digitizer.log
cat   output/batch_report_<source>.jsonl     # digitization, one row per book
cat   output/publish_report_<source>.jsonl   # catalog import, one row per step
```

With `EVILFLOWERS_LOG_FORMAT=json` every log line is a JSON object carrying
`source`, `book_id`, `step`, `duration_ms`, `status` and `entry_id` when known —
ready for Loki/Grafana.

## Resume / re-run

Re-running `run-book`, `run-source <key>` or `run-corpus` is safe and cheap: any
book whose distribution PDF already exists returns `skipped`. Publishing resumes
too: a book whose entry was created but whose upload never finished gets its PDF
uploaded into the same entry. So after an interruption, a reboot, or to pick up
newly-added books, just start it again.

Exit codes: `check` 0/1; `run-book`, `run-source`, `publish-book` and
`publish-catalog` exit 1 when any book errored.

## Scaling

`run-corpus` does the faculties in sequence (keeps disk bounded); within a
faculty, `max_parallel_books` books run at once, each in its own process. For
more throughput use a bigger VM and raise `max_parallel_books`. To split across
machines (or Jobs), prefer **one faculty per machine/Job** (`run-source <key>`).
Several `run-book <key> <IDs…>` Jobs on the *same* faculty must get disjoint id
sets and storage with atomic appends: they share `batch_report_<key>.jsonl` /
`publish_report_<key>.jsonl`, and each start clears that faculty's `monitor`
heartbeats (cosmetic). Publishing stays safe either way — a lost report row is
recovered through the 409 check.

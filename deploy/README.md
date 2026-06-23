# Deploy & run the batch

No orchestrator, no database, no server — the batch is a single long-running
process (`run-corpus`) that writes a resumable JSONL report and a log. Pick how
you keep it running; monitor it live with the `monitor` TUI or `stats`.

## Prerequisites on the VM

- Scans mounted at `/mnt/digital-library/raw-scans` (read-only is fine).
- The toolchain: `scantailor-deviant-cli`, `tesseract` (+ `slk` `ces` `eng` …),
  `recode_pdf`, `jbig2enc` — install natively, or just use the Docker image
  (it bakes them in).
- `configs/pipeline.toml` set for production (filesystem source, `output_dir` on
  durable storage, `cache_dir` on fast scratch) and `configs/catalog.xlsx` in place.
- Tune `[orchestration] max_parallel_books × ocr_jobs ≤ CPU cores`.

## Option A — screen / tmux (simplest)

```bash
screen -S digitize        # or: tmux new -s digitize
python -m evilflowers_books_digitalizer run-corpus
#   Ctrl-A D to detach (screen) / Ctrl-B D (tmux); reattach: screen -r digitize
```

Logs stream to the console **and** to `output/logs/digitizer.log`.

## Option B — systemd (survives reboots, auto-restart on crash)

```bash
sudo cp deploy/evilflowers-digitalizer.service /etc/systemd/system/
# edit WorkingDirectory / User / venv path inside the unit first
sudo systemctl daemon-reload
sudo systemctl start evilflowers-digitalizer
journalctl -u evilflowers-digitalizer -f
```

A crash auto-restarts and skips already-finished books; a clean finish stops.

## Option C — Docker

```bash
cd deploy && cp .env.example .env      # set RAW_SCANS_MOUNT / OUTPUT_DIR / CACHE_DIR
docker compose -f docker-compose.yml up -d --build digitizer
docker compose -f docker-compose.yml logs -f digitizer
```

## Monitoring

```bash
# live TUI dashboard (per-source progress bars, throughput, ETA, output size)
python -m evilflowers_books_digitalizer monitor --config configs/pipeline.toml
#   Docker:  docker compose -f deploy/docker-compose.yml exec digitizer \
#              python -m evilflowers_books_digitalizer monitor

# one-shot summary (good for cron/Slack)
python -m evilflowers_books_digitalizer stats --config configs/pipeline.toml

# raw logs / report
tail -f output/logs/digitizer.log
cat   output/batch_report_<source>.jsonl
```

## Resume / re-run

Re-running `run-corpus` (or `run-source <key>`) is safe and cheap: any book whose
final PDF already exists returns `skipped`. So after an interruption, a reboot,
or to pick up newly-added books, just start it again.

## Scaling

`run-corpus` does the faculties in sequence (keeps disk bounded); within a
faculty, `max_parallel_books` books run at once, each in its own process. For
more throughput use a bigger VM and raise `max_parallel_books`. To split across
machines, run `run-source <key>` for different faculties on different hosts
pointed at the same output store.

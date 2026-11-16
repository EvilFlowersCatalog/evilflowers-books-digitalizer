# Restoration Studio: operator guide

The studio turns a tree of scanned TIFF frames into an Internet Archive "texts" item per
book: cleaned page images, an hOCR text layer, a distribution PDF, a PDF/A-2b master, an
EPUB, and the IA metadata files. Service workers do the heavy lifting in the background. The
operator steps in only where the machine is unsure: a region it could not classify, a
scan mark that repeats across the book, a page that needs a different crop, a title page it
read, or a book the quality gate flagged.

This guide follows one book from import to publication. The screens are described in
[Studio UI](web_ui.md); the contract between the parts is in
[design/studio-spec.md](design/studio-spec.md); the parts and the job flow are in
[Architecture](architecture.md).

![Page studio: regions coloured by kind, a disputed picture selected](screenshots/page-studio.png)

## 1. Start the studio

Locally, the portal and the orchestrator run from the repository's venv; Postgres and Redis
run in Docker (full commands in
[deploy/dev/README.md](https://github.com/EvilFlowersCatalog/evilflowers-books-digitalizer/blob/master/deploy/dev/README.md)):

```bash
docker compose -f deploy/dev/compose.yml up -d                  # Postgres :55432, Redis :56379
export STUDIO_CONFIG=$PWD/configs/studio.dev.toml
export STUDIO_DATABASE_URL=postgresql+psycopg://studio:studio@127.0.0.1:55432/studio
export STUDIO_REDIS_URL=redis://127.0.0.1:56379/0
mkdir -p .cache/studio                                          # beat's schedule file
evilflowers-digitalizer studio migrate                          # schema (alias of db-upgrade)
evilflowers-digitalizer studio serve --port 8000                # the portal: API + the built SPA
evilflowers-digitalizer studio worker -- -B                     # the orchestrator, with beat
```

Build the SPA once with `pnpm --dir frontend install && pnpm --dir frontend build`; the API
serves it from `frontend/dist` at <http://127.0.0.1:8000>. For SPA development, `pnpm
--dir frontend dev` serves it on :5173 and proxies `/api` to the API; `pnpm --dir frontend
dev:mock` runs the whole UI on fixtures.

The portal and the orchestrator run no OCR, cleaning, layout, formula or render engine. A run
plans its jobs and waits until the service workers take them: start the ones you need from
their own repositories, on the same Redis, with `STORAGE_PATH` set to the studio's storage
root. Without them, a run stays `running` with its jobs `dispatched`.

There is no login: the studio is reachable only over the VPN. Click **Set your name** in
the top bar once. The name is kept in the browser and recorded on every note, review and
region edit.

## 2. Import a collection

A *project* (`[projects.<slug>]` in the studio config) says where the scans are and how
books are named:

| key | example | meaning |
|---|---|---|
| `scans_root` | `{storage_root}/raw-scans` | the read-only scan tree; it must be below the storage root |
| `book_pattern` | `{collection}/*-skeny/{book}` | where a book's directory is; `{collection}` and `{book}` are captured |
| `frames_glob` | `stream_pages_tif/*.tif` | frames inside the book directory |
| `item_id` | `stu-{collection}-{book}` | the permanent IA identifier (lowercased, never renamed) |
| `auto_publish` | `never` / `gate` / `review` | when a book goes to the catalog by itself |

On the **Dashboard**, click **Import** next to a collection (or **Import all**). Discovery
runs as the orchestrator task `digitalizer.discover` and adds one row per book in status
`discovered`; nothing is read except directory listings. From the CLI:
`evilflowers-digitalizer studio import stu-local fei`.

## 3. Run a book

Open the book and click **Full run**. The run goes through five stages, each visible live in
the run log. Each stage is sent as jobs to a service; the orchestrator's own steps are marked.

1. **clean.**
   - The orchestrator plans the leaves once (OpenCV spread detection, two leaves per spread,
     LEFT and RIGHT), so leaf numbers stay stable across batches and re-runs.
   - scan-service (`evilflowers_scan.clean`) splits, deskews, crops and binarises each
     leaf and writes previews and Deep Zoom tiles. It also reports stamp, sticker,
     bleed-through and edge candidates (`marks`) per leaf.
   - Then the orchestrator groups the marks across the whole book: see
     [Scan-mark groups](#scan-mark-groups-14).
2. **analyze.**
   - ocr-service (`evilflowers_ocr_worker.standalone.hocr`) and analyzer-service
     (`evilflowers_analyzer.standalone.layout`) read each leaf batch in parallel.
   - The orchestrator fuses their results and the scan marks with `fuse()` into regions.
     Regions that overlapping proposals disagree on, or that have a low confidence, become
     **disputed**; a stamp or sticker is always disputed until a person decides.
   - Confirmed `remove` boxes are painted out of the page by scan-service
     (`evilflowers_scan.erase`).
   - Then the orchestrator prefills the metadata: see [Title-page prefill](#title-page-prefill-15).
3. **formulas.**
   - Formula regions (and regions marked `re-OCR`) go to equation-service
     (`evilflowers_equation.standalone.formulas`).
   - The orchestrator judges every answer: LaTeX is normalised, and a "formula" that reads like
     a drawing is rejected (the region stays an image crop, with the reason recorded).
   - An answer the service itself doubts (`disputed` and `reason` in its result) keeps its text
     and becomes a disputed region with that reason.
4. **render.** The orchestrator looks up metadata and writes the item's text files
   (`render/prepare`); render-service (`evilflowers_render.book`) makes the PDF, the PDF/A-2b
   master, the EPUB and the JP2 package from the access leaves only; the orchestrator writes
   the IA XML documents.
5. **gate.** The quality signals decide `approved`, or `review` with the flags listed.

The jobs are batched (32 leaves per bulk job, one leaf per operator re-run) and admitted by
the scheduler: bulk work yields at peak hours and while the catalog's queues are busy; operator
re-runs are never held. See [Operations](operations.md#holding-and-releasing-bulk-work).

## 4. Review a flagged book

The **Review queue** lists flagged books, lowest score first. `j`/`k` move, `o` opens,
`a` approves. On the book screen, the **Quality gate** panel says why the book was
flagged and the leaf grid marks pages with disputed regions.

Open a leaf to get the **page studio**:

- **Disputed regions** are listed first, with the reason. **Accept** confirms the
  proposal; **It is …** relabels it in one click.
- Select any region (click it, or use the list) to change its kind and its action:
  `keep`, `remove` (erased from the page image before OCR), `as image` (left out of the
  text layer) or `re-OCR`. Drawing a new rectangle creates a confirmed operator region.
  Keys: `t` text, `f` formula, `b` table, `p` picture, `s` stamp, `x` reject,
  `Enter` confirm, `←`/`→` previous/next leaf.
- **Text & math** edits the OCR text of a region; formula LaTeX has a live KaTeX preview.
- **Page** holds the IA leaf fields (page type, printed page number, include in access)
  and the overrides for the next re-run (crop, deskew, split, despeckle, colour mode, OCR
  language).

Rejected regions are never deleted, so a re-run cannot bring a rejected stamp back.

On the book screen, two more panels ask for a decision: **Scan marks** and **Title page**
(below).

## 5. Re-run a page

After changing overrides or confirming `remove` regions, click **Re-run page**. It runs
clean → analyze → formulas for that one leaf in the interactive lane (the services'
`.interactive` queues, one leaf per job), bumps the page version, and the viewer swaps to the
new tiles when the `page.updated` event arrives. Operator decisions survive the re-run:
confirmed and rejected regions are kept and absorb overlapping detector proposals; notes on
replaced machine proposals move to the page. If a re-run changes a page's geometry (size or
deskew), the decided regions on it become disputed again, with the reason "page geometry
changed by a re-run".

A re-run does not rebuild the book's PDFs. Click **Re-render** on the book (or approve
it, which re-renders) to bring page edits into the outputs and into `_scandata.xml`.

## 6. Approve and publish

**Approve** on the book records the review and marks every leaf `done`. Unless the
project's `auto_publish` is `never`, approval enqueues a render run, and, when the project has a
publish target, a publish run behind it; the publish run waits until the render has finished. **Needs
work** sends the book back to `review`; **Reject** is recorded only.

Publication is optional. A project without a `[publish]` target (`target = "none"`) never
publishes: an approved book is complete, with its files on the digitized storage.

Publishing creates or updates the EvilFlowers catalog entry. A republish updates the
same entry and appends a publication row with the next version number.

Only the distribution files are published (`<id>.pdf`, `<id>.epub`; never the PDF/A
master), and they are uploaded to the catalog. A republish **replaces** them: each new
file is uploaded first, then the entry's older files of the same type are deleted, so a
failed republish never leaves the entry without its PDF, and the next one cleans up any
leftovers. Files of other types on the entry stay. When a new book collides with an
existing entry of the same ISBN, the studio adopts that entry, keeps its metadata and
files, and adds only the formats it lacks.

`publish.files_base_url` (linking the files in place instead of uploading them) is not
supported yet: the catalog API cannot create URL-backed acquisitions. A project that
sets it fails at publish time with an error saying so; remove the key to upload. The
needed server change is in [catalog-protocol-requests](design/catalog-protocol-requests.md).

## 7. Get the files

Everything lives under `<artifacts_root>/<project>/<item_id>/`, laid out like an IA item:

| file | what |
|---|---|
| `<id>_meta.xml`, `<id>_scandata.xml`, `<id>_page_numbers.json`, `<id>_files.xml` | IA metadata and leaf data |
| `<id>_jp2.zip` | cleaned pages of all leaves as JPEG 2000 |
| `<id>_hocr.html`, `<id>_djvu.txt` | text layer per leaf, plain text |
| `<id>.pdf`, `<id>_archival.pdf`, `<id>.epub` | distribution PDF, PDF/A-2b master, reflow edition |
| `<id>_studio/` | studio-only previews, tiles, `regions.json` and the job attempt directories |

The book screen's **Downloads** panel serves each file, and **IA export (.zip)** builds a
zip that unpacks into an IA item directory. From the CLI:
`evilflowers-digitalizer studio export-ia <book-id-or-item-id> --out exports/`.

`studio export-training <project>` writes the confirmed regions as COCO / DocLayNet JSON
with page crops, for training a layout model.

## 8. Label items for decider calibration

The decision services (`decider`, `decider-vision`) answer with a probability that means
something only after calibration against our own labels (knowledge#65, issue #7). The
**Labelling** screen collects those labels. Most items come from the *catalog's* extraction
output (dev); the scan-mark items come from the studio's own database.

| task | item | labels |
|---|---|---|
| `figure_kind` | figure crop + caption + the page's text | `chart` `diagram` `photo` `map` `table` `formula` `stamp` `decoration` `scan` `blank` |
| `is_data_table` | table crop + its markdown | `yes` `no` |
| `table_quality` | same screen as `is_data_table` (optional) | `correct` `minor_errors` `broken` |
| `section_relation` | section A and B: title + first paragraphs | `same` `pre` `example` `loose` `none` |
| `subject` | entry title, summary, ToC | a category id of the entry's catalog (UDC top level first) |
| `scan_mark` | one crop per book-wide mark group (the studio's, [#14](#scan-mark-groups-14)) | `stamp` `sticker` `bleed_through` `edge` `content` |
| `title_page` | a book's confirmed metadata form ([#15](#title-page-prefill-15)) | `accepted` `changed` `none`, per field; not labelled by hand |

Every hand-labelled task also has `s` = skip (unsure): stored, never exported. The keys are the
exact choices the callers send to the decider (image-service#6, text-service#20,
EvilFlowersCatalog#190/#191, scan-service#2 for `scan_mark`).

The two studio tasks:

- **`scan_mark`** is sampled from the studio database, so it needs no `[labelling]` catalog.
  One item is one mark group: the crop of its clearest occurrence, the book title, the leaf and
  how many leaves and occurrences the group has. The group's band, decision, probability and
  source are kept back and never shown. Items are stratified by the book's language and the
group's band (or probability bucket).
- **`title_page`** is not sampled and not labelled on a screen (`by_hand = False` in
  `studio/labelling.py`). Every metadata form an operator confirms is a labelled case per
  field. The Labelling screen lists its progress and export only; `sample-labels` and `label`
  refuse it, while `export-labels` and `labelling-status` include it.

1. Configure the catalog in the studio TOML (for the catalog tasks). The key needs **manage**
   rights on the sampled entries, because the region listing is an admin route. It stays on the
   server: crops are proxied.

   ```toml
   [labelling]
   base_url = "https://dev.evilflowers.elvira.stuba.sk/api/v1"
   api_key_env = "EVILFLOWERS_LABELLING_API_KEY"
   catalog_ids = []        # empty = every catalog the key sees
   max_entries = 200       # entries scanned per sample
   ```

The operator works through whichever interface suits them. All three use the same use
cases, and every label records which one it came through (`via`).

| step | SPA (**Labelling** in the menu) | CLI (`evilflowers-digitalizer studio …`) | MCP (`/mcp` on the portal, or `studio mcp` over stdio) |
|---|---|---|---|
| sample | **Sample 100 more** (runs in the background) | `sample-labels --task <t> --n 300` | `sample_label_items` |
| label | digits pick, `s` skips, Enter saves a table | `label --task <t> --operator <name>` (interactive; `--open` shows crops), or `label --task <t> --item <id> --value <key>` | `next_label_item` → `submit_label` |
| progress | the task list | `labelling-status` | `list_label_tasks`, `label_summary` |
| export | **Export** link | `export-labels --task <t> --out <file>` | `export_labels` |

2. **Sample** items. They are stratified by language × the decider's confidence: its band, or
   `p` while bands are empty, as they are until knowledge#65 sets thresholds. That way Slovak
   items and the unsure cases are both represented. Re-running only adds new items.
3. **Label.** The model's answer is never shown. About 10 % of items are offered to a second
   person.
   - **SPA:** set your name first.
   - **CLI:** pass `--operator`, or set `$STUDIO_OPERATOR`.
   - **MCP:** the client passes `labeller`.

   **The labels must be human.** An MCP client relays the person's answer and must not answer
   for them. The server's instructions and tool descriptions say so, and every label records
   `via` (spa, cli or mcp), so knowledge#65 can filter on it. To use the portal's MCP endpoint
   from Claude Code, run:
   `claude mcp add --transport http evilflowers-studio http://<portal>/mcp`.
4. **Export:** `evilflowers-digitalizer studio export-labels --task figure_kind --out labels/figure_kind.jsonl`,
   or the **Export** link, or `GET /api/v1/labelling/tasks/{task}/export`. One line per label:

   ```json
   {"task": "figure_kind", "item": {"entry_id": "…", "run_id": "…", "region": "p012-fig01", "region_id": "…"}, "language": "sk", "label": "stamp", "labeller": "…", "labelled_at": "…", "via": "spa"}
   ```

   A `title_page` line comes from a confirmed form, one per suggested field:

   ```json
   {"task": "title_page", "item": {"book_id": 12, "item_id": "stu-fei-…", "field": "year", "leaf": 3}, "language": "slo", "label": "changed", "suggested": "1998", "line": "…", "p": 0.81, "value": "2007", "decider_model": "…", "labeller": "…", "labelled_at": "…", "via": "spa"}
   ```

   `labels/figure_kind.jsonl.summary.json` holds counts per label, language, labeller and `via`,
   raw agreement and Cohen's κ on the doubly labelled items, and the sampling note.

Known limits: the catalog has no route for section text yet (the studio matches paragraphs to
the heading), `sections/{key}/related` drops pairs the catalog hid and is cross-book only, and
regions carry no key (the studio builds `p012-fig01`). The asks are written up in
[catalog-labelling-requests](design/catalog-labelling-requests.md). Design:
`docs/diagrams/*-digitalizer-labelling.puml`.

## Scan-mark groups (#14)

Library stamps, stickers and bleed-through repeat across the pages of a book. scan-service
only counts repeats inside one clean job (at most 32 leaves; an interactive job has one leaf),
so a stamp on every 20th page would cost one decider call per occurrence. The orchestrator
groups the marks of the whole book instead and decides once per distinct mark.

![Scan marks](diagrams/sequence-digitalizer-scan-marks.svg)

**When.** The `clean/marks` step (`digitalizer.local.clean_marks`) runs once after every clean
job of a run is applied, before analyze. Code: `studio/marks.py` (pure rules),
`studio/workers/stages/marks.py` (the step).

**Grouping** (`[marks]`):

| rule | default |
|---|---|
| same category: `stamp`, `sticker` or `bleed_through` (an `edge` band is a page border, not grouped) | |
| 64-bit dHashes differ in at most `hash_distance` bits | 6 |
| aspect ratios differ by at most `aspect_tolerance` (relative) | 25 % |
| a group on at least `min_leaves` leaves is furniture | 3 |

A mark already in a group keeps it, so a group's verdict and an operator's split survive later
runs. A new mark joins the first matching group, else forms a group with the other new marks
(single linkage). A re-clean of a leaf carries the groups over.

**Verdicts.**

- A group on `min_leaves` leaves or more is `hide`, decision `repeated`, source `rule`. No
  model is asked.
- A group with an uncertain member (band `review`) is sent to decider-vision **once**, with its
  clearest occurrence (highest probability, then the largest). The question is `scan_mark`;
  the choices are `stamp`, `sticker`, `bleed_through`, `edge`, `content`. The verdict is every
  member's.
- The decider's verdict becomes `hide` or `keep` only when its probability reaches
  `[marks] hide_above` or `keep_above`. Both are unset until knowledge#65 calibrates
  `scan_mark`, so every decider verdict is `review`: stored with its probability, never acted
  on.
- Fail soft: with no `[decider] vision_url` (or `DECIDER_ENABLED=false`), or when a call
  fails, the group keeps its members' bands and is asked again on the next run. The run never
  fails on it.

**Effect.** At analyze, stamps and stickers become *disputed* regions (a mark whose band is
`keep` is not proposed). Bleed-through and edge marks have no region kind. **Nothing is erased
by a verdict.** Erasing stays a confirmed `remove` region, which scan-service paints out
(`evilflowers_scan.erase`).

**Operator.** The book screen's **Scan marks** panel lists the groups, largest first, each with
its crop, category, band, probability, who decided it, and the leaves it is on.

| action | what it does | API |
|---|---|---|
| **Keep** | the mark is part of the printed page | `PATCH /api/v1/mark-groups/{id}` with `{"band": "keep"}` |
| **Hide** | the mark is an artefact | the same, `{"band": "hide"}` |
| **Split…** | move the occurrences on the given leaves into a new group (verdict `review`); at least one occurrence stays | `POST /api/v1/mark-groups/{id}/split` with `{"leaves": [...]}` |

Other routes: `GET /api/v1/books/{id}/mark-groups`, `GET /api/v1/mark-groups/{id}/image` (the
crop). An operator verdict sets the group's source to `operator`, which the next regrouping
leaves alone. The members' marks change at once; their regions change at the next analyze of
those leaves, that is, when you re-run those pages.

Data: table `mark_groups`, column `pages.marks` (migration `0007_marks_title_page`). Metrics:
`digitalizer_marks`, `digitalizer_mark_groups`, `digitalizer_mark_decider_calls_saved`.

## Title-page prefill (#15)

The title page and the colophon carry the title, authors, publisher and year. The studio
suggests them; an operator confirms them.

![Title page](diagrams/sequence-digitalizer-title-page.svg)

**When.** The `analyze/title_page` step (`digitalizer.local.title_page`) runs once after every
analyze job of a run is applied. Code: `studio/title_page.py` (candidate lines from hOCR),
`studio/workers/stages/title_page.py` (the step), `studio/application/title_page.py` (the API).

**How.**

1. The first `[title_page] first_leaves` leaves (4) and the last `last_leaves` leaves (2) give
   the candidate lines from their hOCR.
2. For each field (`title`, `authors`, `publisher`, `year`) the decider (text model) gets one
   `choice` question: which candidate line is it? There are at most `max_candidates` (12) lines,
   plus "none".
3. The answers are stored in `books.metadata_suggestions` with their probability, the line and
   the leaf.

The step is skipped when the decider is off, when a re-run touched no title or colophon leaf,
or once an operator confirmed the book's metadata. A failed call leaves that field without a
suggestion. The run never fails on it.

**Operator.** The book screen's **Title page** panel shows the suggestions as a form, each with
its probability, the source line and a link to the leaf. An empty field means the page does not
say it.

- `GET /api/v1/books/{id}/title-page` returns the suggestions and, once confirmed, the
  comparison.
- `POST /api/v1/books/{id}/title-page/confirm` takes the final `title`, `authors`, `publisher`
  and `year`. It writes them to the item metadata (`title`, `creator`, `publisher`, `date`)
  through the same validation as any metadata edit. It answers 400 when the book has no
  suggestions.

**Nothing is imported unconfirmed.** Confirming also stores, per suggested field, the outcome in
`books.metadata_confirmed`: `accepted` (the final value is the suggestion), `changed` (the
operator typed another value) or `none` (left empty). Each is a labelled `title_page` case
([labelling](#8-label-items-for-decider-calibration)).

This is separate from the metadata providers (Excel, OPAC, OCR colophon) that
`render/prepare` consults; see [Metadata](metadata.md). Values an operator has saved win over a
provider's lookup, which only fills gaps.

Metric: `digitalizer_title_page_fields{field,outcome}`.

## Decision services

Two shared HTTP services judge uncertain cases. Both are in namespace `shared` on the cluster;
knowledge#77 admits the `digitalizer` namespace.

| Service | `[decider]` key | Used by |
|---|---|---|
| `decider` (Laya, text only) | `url` | title-page prefill (#15) |
| `decider-vision` (images) | `vision_url` | scan-mark groups (#14) |

```toml
[decider]
url = "http://decider.shared.svc.cluster.local/v1/systemone"
vision_url = "http://decider-vision.shared.svc.cluster.local/v1/systemone"
model = "multilingual"      # Laya checkpoint; without it Laya may swap
timeout = 60
```

The environment wins over the file: `DECIDER_URL`, `DECIDER_VISION_URL`, `DECIDER_MODEL`,
`DECIDER_TIMEOUT`. `DECIDER_ENABLED=false` switches both off. An empty URL switches that use
off.

The client is `studio/adapters/decider.py` (`HttpDecider`, port `ports.Decider`). It posts one
`choice` question per call to `POST /v1/systemone` with `state` (at most 50 000 characters),
optionally up to four `images`, and the criteria. It reads back the chosen key, its probability
and the model name, which is stored as `decider_model`. There is no API key; a NetworkPolicy
admits the calling namespaces.

**Both uses fail soft.** A missing URL, a timeout or an error leaves marks at `review` and the
metadata unprefilled. A run never fails because a decision service is down. The probabilities
mean something only after calibration (knowledge#65), which is what [labelling](#8-label-items-for-decider-calibration)
collects.

## Troubleshooting

| symptom | cause / fix |
|---|---|
| A run stays `queued` | the orchestrator is not running (no worker on `digitalizer.portal`, or no beat) |
| A run stays `running`, its jobs `dispatched` | no service worker takes the job's queue: check it is on the same Redis db and has the task and queue of `DEFAULT_ROUTES` |
| Render fails with "access leaves without cleaned image" | the scratch cache (`cache_dir`) was evicted: run a full run or re-run those leaves |
| The original layer says the frame is missing | the scan mount is offline; cleaned pages and PDFs still work |
| Tiles do not change after a re-run | hard-reload; tile URLs carry `?v=<page version>` |
| `studio serve` fails with "configs/studio.toml" | set `STUDIO_CONFIG` or pass `--config` |
| The config is refused at load | a removed key or a path outside the storage root; the message names it ([Deployment](deployment.md#configuration)) |

More in [Operations](operations.md#common-issues).

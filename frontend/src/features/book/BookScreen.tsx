/**
 * Screen 3 — Book: metadata, quality flags, runs with a live log, the pages grid,
 * review/approve, publication history and downloads.
 */

import { Link, useNavigate, useParams } from "react-router-dom";
import { useBook, useCreateRun, usePages } from "../../api/hooks";
import type { RunKind } from "../../api/types";
import { Button, Empty, ErrorNote, PageHeader, Panel, Spinner, StatusBadge } from "../../components/ui";
import s from "../screen.module.css";
import { DownloadsPanel, NotesPanel, PagesGrid, PathsPanel, PublicationsPanel, QualityPanel, ReviewPanel, RunsPanel } from "./BookPanels";
import { MetadataForm } from "./MetadataForm";

export function BookScreen() {
  const bookId = Number(useParams().bookId);
  const { data: detail, isLoading, error } = useBook(bookId);
  const { data: pages = [] } = usePages(bookId);
  const createRun = useCreateRun();
  const navigate = useNavigate();

  if (isLoading) return <Empty><Spinner /></Empty>;
  if (!detail) return <div className={s.screen}><ErrorNote error={error ?? "Book not found"} /></div>;

  const { book } = detail;
  const firstDisputed = pages.find((p) => (p.disputed ?? 0) > 0) ?? pages.find((p) => p.status === "disputed");
  const run = (kind: RunKind) => createRun.mutate({ bookId, body: { kind } });
  const disputedTotal = pages.reduce((n, p) => n + (p.disputed ?? 0), 0);

  return (
    <div className={s.screen}>
      <PageHeader
        title={
          <span className={s.row}>
            {book.title || book.slug} <StatusBadge status={book.status} />
          </span>
        }
        sub={
          <>
            <Link to="/books">Books</Link> / <code>{book.item_id ?? book.slug}</code> · {book.n_pages ?? "?"} leaves
            {book.collection ? ` · ${book.collection}` : ""}
          </>
        }
        actions={
          <div className={s.row}>
            {firstDisputed && (
              <Button variant="primary" onClick={() => navigate(`/pages/${firstDisputed.id}`)}>
                Resolve {disputedTotal || ""} disputed
              </Button>
            )}
            <Button onClick={() => run("full")} disabled={createRun.isPending}>
              Full run
            </Button>
            <Button onClick={() => run("render")} disabled={createRun.isPending}>
              Re-render
            </Button>
            <Button onClick={() => run("publish")} disabled={createRun.isPending}>
              Publish
            </Button>
          </div>
        }
      />
      <ErrorNote error={createRun.error} />
      <div className={s.grid2}>
        <div className={s.stack}>
          <Panel title={`Leaves (${pages.length})`} actions={<span className="faint" style={{ fontSize: 12 }}>dimmed = excluded from access formats</span>}>
            <PagesGrid pages={pages} />
          </Panel>
          <MetadataForm bookId={bookId} metadata={detail.item_metadata} />
        </div>
        <div className={s.stack}>
          <QualityPanel detail={detail} />
          <ReviewPanel detail={detail} />
          <RunsPanel runs={detail.runs} />
          <PublicationsPanel detail={detail} />
          <DownloadsPanel detail={detail} />
          <NotesPanel detail={detail} />
          <PathsPanel detail={detail} />
        </div>
      </div>
    </div>
  );
}

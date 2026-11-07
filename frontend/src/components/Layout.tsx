/**
 * App shell: left navigation, a top status bar with the live SSE state, the latest
 * activity line, running runs and the operator name; the routed screen below.
 */

import { useMemo } from "react";
import { Link, NavLink, Outlet } from "react-router-dom";
import { describeEvent } from "../api/events";
import { useLive, type SseStatus } from "../api/live";
import { useSession } from "../lib/session";
import { BooksIcon, DashboardIcon, ReviewIcon, SettingsIcon, UserIcon } from "./icons";
import s from "./Layout.module.css";
import { ProgressBar } from "./ui";

const SSE_LABEL: Record<SseStatus, { text: string; color: string }> = {
  open: { text: "Live", color: "var(--ok)" },
  connecting: { text: "Connecting…", color: "var(--warn)" },
  down: { text: "Offline · polling", color: "var(--danger)" },
};

function StatusBar() {
  const status = useLive((st) => st.status);
  const events = useLive((st) => st.events);
  const runs = useLive((st) => st.runs);
  const { operator } = useSession();
  const active = useMemo(() => Object.values(runs).filter((r) => r.status === "running"), [runs]);
  const latest = events.find((e) => e.type !== "run.progress") ?? events[0];

  return (
    <header className={s.top}>
      <span className={s.sse} title="Server-sent events">
        <span className={s.sseDot} style={{ background: SSE_LABEL[status].color }} />
        {SSE_LABEL[status].text}
      </span>
      {import.meta.env.VITE_MOCK === "1" && <span className={s.mock}>MOCK DATA</span>}
      <div className={s.ticker} aria-live="polite">
        {latest ? (
          <>
            <b>{latest.project}</b> · {describeEvent(latest)}
          </>
        ) : (
          "Waiting for activity…"
        )}
      </div>
      {active.slice(0, 3).map((r) => (
        <span key={r.runId} className={s.runs} title={`run #${r.runId} · ${r.stage ?? ""}`}>
          #{r.runId} {r.stage}
          <span className={s.mini}>
            <ProgressBar done={r.done} total={r.total} />
          </span>
        </span>
      ))}
      <Link to="/settings" className={`${s.operator} ${operator ? "" : s.missing}`} title="Operator name (X-Operator)">
        <UserIcon />
        {operator || "Set your name"}
      </Link>
    </header>
  );
}

function Nav() {
  const { project, projects, setProject } = useSession();
  const review = project?.counts.review ?? 0;
  const link = ({ isActive }: { isActive: boolean }) => `${s.link} ${isActive ? s.active : ""}`;
  return (
    <nav className={s.nav} aria-label="Main">
      <div className={s.brand}>
        <span className={s.logo}>R</span>
        <div>
          Restoration
          <small>EvilFlowers studio</small>
        </div>
      </div>
      <NavLink to="/" end className={link}>
        <DashboardIcon /> Dashboard
      </NavLink>
      <NavLink to="/books" className={link}>
        <BooksIcon /> Books
      </NavLink>
      <NavLink to="/review" className={link}>
        <ReviewIcon /> Review queue
        {review > 0 && <span className={s.count}>{review}</span>}
      </NavLink>
      <div className={s.navSection}>Studio</div>
      <NavLink to="/settings" className={link}>
        <SettingsIcon /> Settings
      </NavLink>
      <div className={s.navFoot}>
        <label htmlFor="project-picker">Project</label>
        <select id="project-picker" value={project?.slug ?? ""} onChange={(e) => setProject(e.target.value)}>
          {projects.map((p) => (
            <option key={p.slug} value={p.slug}>
              {p.name}
            </option>
          ))}
        </select>
      </div>
    </nav>
  );
}

export function Layout() {
  return (
    <div className={s.shell}>
      <Nav />
      <StatusBar />
      <main className={s.main}>
        <Outlet />
      </main>
    </div>
  );
}

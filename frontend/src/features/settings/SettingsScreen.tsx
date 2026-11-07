/**
 * Screen 6 — Settings: the operator name (this browser only, sent as X-Operator)
 * and a read-only view of the current project's preset.
 */

import { useState } from "react";
import { ApiError } from "../../api/client";
import { useProjectConfig } from "../../api/hooks";
import { Button, Empty, ErrorNote, PageHeader, Panel, Spinner } from "../../components/ui";
import { useSession } from "../../lib/session";
import s from "../screen.module.css";

function OperatorForm() {
  const { operator, setOperator } = useSession();
  const [name, setName] = useState(operator);
  const [saved, setSaved] = useState(false);
  return (
    <Panel title="Operator">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          setOperator(name);
          setSaved(true);
        }}
      >
        <label className="label" htmlFor="operator-name">
          Your name
        </label>
        <div className={s.row}>
          <input id="operator-name" className="input" value={name} placeholder="e.g. Jana Nováková" onChange={(e) => { setName(e.target.value); setSaved(false); }} />
          <Button type="submit" variant="primary">
            Save
          </Button>
        </div>
        <div className={s.hint}>
          Stored in this browser only. Recorded on your notes, reviews and region edits. {saved && <b>Saved.</b>}
        </div>
      </form>
    </Panel>
  );
}

function renderValue(value: unknown): string {
  if (Array.isArray(value)) return value.join(", ");
  if (value && typeof value === "object") return JSON.stringify(value, null, 1).replace(/[{}"\n]/g, " ").trim();
  return String(value ?? "—");
}

function ProjectPreset() {
  const { project } = useSession();
  const { data, isLoading, error } = useProjectConfig(project?.slug);
  const missing = error instanceof ApiError && error.status === 404;
  return (
    <Panel title={`Project preset${project ? ` · ${project.slug}` : ""}`}>
      {isLoading && <Spinner />}
      {missing && (
        <Empty>
          The API does not expose project presets yet (<code>GET /projects/{"{p}"}/config</code>). Edit{" "}
          <code>configs/studio.*.toml</code> on the server.
        </Empty>
      )}
      {error && !missing ? <ErrorNote error={error} /> : null}
      {data && (
        <dl className={s.dl}>
          {Object.entries(data).map(([k, v]) => (
            <div key={k} style={{ display: "contents" }}>
              <dt>{k}</dt>
              <dd className="mono">{renderValue(v)}</dd>
            </div>
          ))}
        </dl>
      )}
      {project && (
        <>
          <h3 style={{ margin: "16px 0 6px", fontSize: 13 }}>Collections</h3>
          <dl className={s.dl}>
            {Object.entries(project.collections).map(([k, name]) => (
              <div key={k} style={{ display: "contents" }}>
                <dt className="mono">{k}</dt>
                <dd>{name}</dd>
              </div>
            ))}
          </dl>
        </>
      )}
    </Panel>
  );
}

export function SettingsScreen() {
  return (
    <div className={s.screen}>
      <PageHeader title="Settings" sub="Per-browser preferences and the project configuration (read-only)" />
      <div className={s.grid2}>
        <ProjectPreset />
        <OperatorForm />
      </div>
    </div>
  );
}

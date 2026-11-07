/**
 * Per-browser session state: the operator name and the current project.
 * Both live in localStorage (no auth, VPN only) and are exposed via one context.
 */

import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { useProjects } from "../api/hooks";
import { getOperator, onOperatorChange, setOperator } from "../api/operator";
import type { ProjectOut } from "../api/types";

const PROJECT_KEY = "studio.project";

function readProject(): string {
  try {
    return localStorage.getItem(PROJECT_KEY) ?? "";
  } catch {
    return "";
  }
}

interface Session {
  operator: string;
  setOperator: (name: string) => void;
  project: ProjectOut | undefined;
  projects: ProjectOut[];
  setProject: (slug: string) => void;
}

const SessionContext = createContext<Session | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const [operator, setOp] = useState(getOperator);
  const [slug, setSlug] = useState(readProject);
  const { data: projects = [] } = useProjects();

  useEffect(() => onOperatorChange(setOp), []);

  const value = useMemo<Session>(() => {
    const project = projects.find((p) => p.slug === slug) ?? projects[0];
    return {
      operator,
      setOperator,
      project,
      projects,
      setProject: (next) => {
        setSlug(next);
        try {
          localStorage.setItem(PROJECT_KEY, next);
        } catch {
          /* per-tab only */
        }
      },
    };
  }, [operator, projects, slug]);

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): Session {
  const ctx = useContext(SessionContext);
  if (!ctx) throw new Error("useSession outside SessionProvider");
  return ctx;
}

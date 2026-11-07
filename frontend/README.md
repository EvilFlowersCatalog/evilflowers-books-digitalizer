# Restoration Studio SPA

React 18 + TypeScript + Vite front end for the studio API (`/api/v1`, see
`docs/design/studio-spec.md`). Builds to `frontend/dist`, which the API serves
(`STUDIO_STATIC_DIR`).

```sh
pnpm --dir frontend install
pnpm --dir frontend dev          # proxies /api -> http://127.0.0.1:8000 (override: STUDIO_API_URL)
pnpm --dir frontend dev:mock     # VITE_MOCK=1: in-browser fixture backend, no server needed
pnpm --dir frontend build        # tsc --noEmit + vite build -> dist/
pnpm --dir frontend test         # vitest (API client, SSE -> cache plan, shortcuts)
```

Layout:

- `src/api/` — hand-written wire types, typed client (`X-Operator` is
  percent-encoded), endpoints, query hooks, SSE (`events.ts` maps each event to
  cache invalidations; `live.ts` owns the one EventSource and polling fallback),
  `media.ts` (every image / DZI / download URL).
- `src/features/` — the six screens; `studio/` is the page studio
  (OpenSeadragon, Annotorious, compare modes, OpenCV.js preview, KaTeX).
- `src/mock/` — fixture backend, fake SSE bus and generated page images; loaded
  only when `VITE_MOCK=1` (dynamic import, absent from normal builds).

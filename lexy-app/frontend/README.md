# Lexy frontend

The React SPA for the Lexy language-learning app. The repository guide is
[`CLAUDE.md`](../../CLAUDE.md) at the repo root; the file-level index is
[`docs/SUMMARY.md`](../../docs/SUMMARY.md). This file covers only what is
specific to running and testing this package.

> This README replaced the unmodified `npm create vite` template text on
> 2026-08-25. That template described a scaffold, not this app.

## Stack

Versions below are from `package.json` as of 2026-08-25.

- **React 19.2** + **react-router-dom 7.13**
- **Vite 8** with `@vitejs/plugin-react` 6 (Oxc). The React Compiler is not enabled.
- **TypeScript 5.9**, `"strict": true` in both `tsconfig.app.json` and `tsconfig.node.json`
- **Vitest 4** + jsdom for tests
- **No state library** — `useOutletContext`, custom hooks in `src/hooks/`, and
  `localStorage` for the auth token
- **No CSS framework** — inline `React.CSSProperties`, themed through the
  CSS-variable system in `src/index.css` driven by `[data-theme]` on `<html>`

## Commands

```bash
npm install
npm run dev        # Vite dev server on :5173
npm run build      # tsc -b && vite build
npm run lint       # eslint .
npm run test       # vitest run
npm run preview    # serve the production build locally
```

`npm run dev` proxies `/api` to `http://localhost:8000` (`vite.config.ts`), so
the backend must be running separately — see `CLAUDE.md` §11. In the Docker
build there is no proxy and no :5173: the backend serves the built `dist/` as
static files, and the whole app is on :8000.

## Two things that will cost you time

**`tsc --noEmit` passing does not mean `npm run build` passes.** The build runs
`tsc -b`, which uses the project references and stricter settings. Always run
the real `npm run build`. This is logged in
[`docs/COMMON_ERRORS.md`](../../docs/COMMON_ERRORS.md) — read it before
debugging a build failure from scratch.

**`npm run lint` is not currently clean.** Measured 2026-08-25: **22 errors and
8 warnings**, all pre-existing, mostly `react-refresh/only-export-components`
from files that export both a component and a helper. Lint is not part of the
validation gate for this package (that is `npm run build` plus `npx vitest
run`), so treat a finding here as pre-existing unless your change introduced it.

## Tests

`npx vitest run` — **322 tests across 44 files** as of 2026-08-25. Setup file is
`src/test/setup.ts`. Per-file coverage notes live in
[`docs/TESTS.md`](../../docs/TESTS.md); update that file whenever you add,
remove, or rename a test.

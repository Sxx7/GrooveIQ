# Session 01 — Foundation

You are starting the GrooveIQ dashboard rebuild. This is **session 01 of 12**. The new dashboard replaces a vanilla-JS single-page admin UI; this session sets up the scaffold that all subsequent sessions plug into.

## Read first (in order, before doing anything)

1. `docs/rebuild/README.md` — master plan and working agreement.
2. `docs/rebuild/conventions.md` — file layout, JS module pattern, CSS organisation. **Locked.**
3. `docs/rebuild/components.md` — design tokens (palette, type, spacing) and shell specs.
4. `design_handoff_grooveiq_dashboard/README.md` — design hand-off framing.
5. `design_handoff_grooveiq_dashboard/styles.css` — token CSS to lift verbatim into `tokens.css`.
6. `design_handoff_grooveiq_dashboard/page-realistic.jsx` — high-fidelity sidebar / topbar / activity-pill reference.
7. `design_handoff_grooveiq_dashboard/primitives.jsx` — reference implementations.

## Goal

Stand up the new dashboard scaffold at `/static/dashboard-v2.html` so a user can:
- Load the page and see the new dark-mode shell with sidebar + topbar.
- Toggle the sidebar between expanded (220px) and collapsed (60px). Persists across reload.
- Click any of the four buckets (Explore / Actions / Monitor / Settings) and see the bucket's sub-page tabs in the topbar update accordingly.
- See a stub page in the main area for whichever sub-page is active. Stubs literally read `Page: {bucket} → {sub-page}` for now.
- Connect with an API key (re-use the existing flow — bottom-sidebar input + Connect, persist in `sessionStorage`).
- See an SSE-status pill in the topbar (lavender pulse if connected; grey "SSE off" if not).

The old dashboard at `/dashboard` stays untouched. Verify it still works after this session.

## Out of scope

- Real data on any page (just stubs).
- Activity pill popover content (placeholder only — hard-code "3 active" with a non-functional dropdown for now).
- Mobile / responsive (desktop-only this session).
- Light mode (dark-only this session).
- ⌘K command palette.

## Tasks (ordered)

1. **Branch.** `git checkout -b gui-rebuild` from `main`. Confirm `git status` clean before starting.
2. **Create directory structure** per `conventions.md`:
   - `app/static/css/tokens.css`
   - `app/static/css/shell.css`
   - `app/static/css/components.css` (empty file with header comment for now)
   - `app/static/css/pages.css` (empty file with header comment for now)
   - `app/static/js/v2/core.js`
   - `app/static/js/v2/router.js`
   - `app/static/js/v2/shell.js`
   - `app/static/js/v2/components.js` (empty stub)
   - `app/static/js/v2/explore.js` (stub bucket)
   - `app/static/js/v2/actions.js` (stub bucket)
   - `app/static/js/v2/monitor.js` (stub bucket)
   - `app/static/js/v2/settings.js` (stub bucket)
   - `app/static/js/v2/index.js` (entry point)
   - `app/static/dashboard-v2.html`
3. **`tokens.css`** — port the `:root` (light) and `[data-theme="dark"]` blocks from `design_handoff/styles.css` verbatim. Add `--accent-soft`, `--wine-soft`, etc. as defined there. Set `[data-theme="dark"]` as the default by adding `<html data-theme="dark">` in `dashboard-v2.html`.
4. **`shell.css`** — sidebar, topbar, activity-pill, search-row styles per `components.md → Shell`. Include the pulsing-dot `@keyframes pulse` (1.6s ease-in-out infinite, opacity 1↔0.5, scale 1↔1.3).
5. **`dashboard-v2.html`** — single `<html data-theme="dark">` page with:
   - `<head>` linking Inter + Inter Tight + JetBrains Mono from Google Fonts (or CDN), tokens.css, shell.css, components.css, pages.css.
   - `<body>` containing `<div id="app">` for the shell.
   - `<script>` tags loading v2 JS in order: core → router → shell → components → explore + actions + monitor + settings → index.
6. **`core.js`** — port a minimal subset of utilities from old `app.js`:
   - `GIQ.fmt.esc(s)` — HTML escape.
   - `GIQ.fmt.timeAgo(unix)`, `GIQ.fmt.fmtTime(unix)`, `GIQ.fmt.fmtDuration(secs)`, `GIQ.fmt.fmtNumber(n)`.
   - `GIQ.api.get(path)`, `GIQ.api.post(path, body)`, etc. — fetch wrappers using the API key from `sessionStorage.getItem('giq.apiKey')` as `Bearer`.
   - `GIQ.toast(msg, kind, duration?)` — bottom-right stack, auto-dismiss.
   - `GIQ.state` — empty object placeholder.
7. **`router.js`** — hash-based routing. On `hashchange` (and on initial load), parse `#/{bucket}/{subpage}`, validate against the bucket's sub-page list, dispatch to `GIQ.pages[bucket][subpage](root)`. Default route on empty hash: `#/monitor/overview`. Bucket landing pages: `#/explore` → recommendations · `#/actions` → pipeline-ml · `#/monitor` → overview · `#/settings` → algorithm. Maintain `GIQ.router.cleanup` (the cleanup fn returned by the last page render) and call it on nav-away.
8. **`shell.js`** — render the sidebar (4 nav items, collapsible with localStorage persistence under `groove.nav.collapsed`, activity-pill placeholder, search-row placeholder) and the topbar (sub-page tabs for the current bucket, SSE-status pill). The topbar tabs come from a hard-coded list per bucket — see below. SSE pill: lavender pulse "SSE live" when connected, grey "SSE off" when not. (No real SSE this session — show "SSE off" always; session 02 wires it.)
9. **Sub-page lists** (hard-code in shell.js for now):
   - Explore: Recommendations · Radio · Playlists · Tracks · Text Search · Music Map · Charts · Artists · News
   - Actions: Pipeline & ML · Library · Discovery · Charts · Downloads
   - Monitor: Overview · Pipeline · Models · System Health · Recs Debug · User Diagnostics · Integrations · Downloads · Lidarr Backfill · Discovery · Charts
   - Settings: Algorithm · Download Routing · Lidarr Backfill · Connections · Users · Onboarding
10. **Stub bucket modules** — each registers a render fn for every sub-page that does:
    ```js
    GIQ.pages.monitor.overview = function (root) {
        root.innerHTML = '<div class="page-stub"><div class="eyebrow">MONITOR</div><h1>Overview</h1><p class="muted">TBD — session 02</p></div>';
    };
    ```
    Apply the same pattern for all sub-pages across all four buckets. Add `.page-stub` styles to `pages.css`.
11. **`index.js`** — boot: read API key from `sessionStorage`, render shell, parse current hash → render initial page, set up `hashchange` listener.
12. **API key flow** — sidebar bottom (above search row) gets a small input + Connect button. On Connect, validate against `GET /health`, store key in `sessionStorage` as `giq.apiKey`. If unconfigured, show a message in the main area.
13. **Verify visually** with the preview tools:
    - Start the dev server (`uvicorn app.main:app --reload` or whatever the project's standard is — check `CLAUDE.md`).
    - Use `preview_start` to load `http://localhost:<port>/static/dashboard-v2.html`.
    - Take a screenshot. Verify: dark background, sidebar with 4 buckets, topbar with sub-pages of the active bucket, stub page in main area.
    - Click each bucket; verify sub-pages update.
    - Click sidebar toggle; verify collapse + expand + persistence (reload after collapsing).
    - Verify old `/dashboard` still loads with the old UI.
14. **Hand-off note** — write `docs/rebuild/handoffs/01-foundation.md` per the template at `handoffs/_template.md`.
15. **Commit** on `gui-rebuild` with message `rebuild: session 01 — foundation`.

## Verification checklist

- [ ] `gui-rebuild` branch exists with one new commit.
- [ ] `/static/dashboard-v2.html` loads cleanly with no console errors.
- [ ] Sidebar collapse persists across reload.
- [ ] All four buckets navigable; sub-pages render stub text.
- [ ] Old `/dashboard` still works.
- [ ] Hand-off note written.
- [ ] Screenshot saved.

## Deliverables

A working shell. No data wiring. No fancy components yet. The next session (02) will build the canonical Monitor → Overview page on top of this scaffold and validate the design system end-to-end.

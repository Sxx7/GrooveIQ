# Hand-off: Session 01 — Foundation

## Status
- [x] Session goal achieved
- [x] Visual verification done (screenshots inline in session transcript)
- [x] No regressions in old `/dashboard` (zero modifications to tracked files)
- [x] Committed on `gui-rebuild` branch with message `rebuild: session 01 — foundation`

## What landed

The new dashboard scaffold lives at `/static/dashboard-v2.html`. It boots into a dark-mode shell with a 4-bucket sidebar (Explore · Actions · Monitor · Settings), a topbar that shows the active bucket's sub-pages plus an SSE-status pill, and a stub page area. The sidebar collapses to 60px (persisted to `localStorage` under `groove.nav.collapsed`). Every sub-page across all four buckets has a registered renderer that prints `Page: {bucket} → {sub-page} — TBD`. Hash routing (`#/{bucket}/{subpage}`) is wired with sensible defaults (empty hash → `#/monitor/overview`, bucket-only → bucket default subpage). The bottom-sidebar API key block reads/writes `sessionStorage.giq.apiKey`, validates against `GET /health`, and updates a status indicator + toast on success/failure.

The old `/dashboard` is untouched.

## File inventory after this session

New files only; no existing files modified.

- [app/static/dashboard-v2.html](app/static/dashboard-v2.html) — entry HTML; `<html data-theme="dark">`, loads Inter / Inter Tight / JetBrains Mono from Google Fonts CDN, then 4 CSS files and 9 JS files in order.
- [app/static/css/tokens.css](app/static/css/tokens.css) — palette / type / spacing / radius tokens lifted verbatim from `design_handoff_grooveiq_dashboard/styles.css`. Defines `:root` (light) and `[data-theme="dark"]`. Sets up the `pulse` keyframes used by live indicators. Adds geometry vars (`--side-w-expanded`, `--topbar-h`, etc.).
- [app/static/css/shell.css](app/static/css/shell.css) — sidebar (logo, nav, activity pill, API key block, search row), topbar (subnav tabs + SSE pill), main column. Includes the collapsed-sidebar variants for every shell sub-component.
- [app/static/css/components.css](app/static/css/components.css) — empty stub; populated by session 02.
- [app/static/css/pages.css](app/static/css/pages.css) — `.page-stub`, `.page-message`, and the toast stack styles.
- [app/static/js/v2/core.js](app/static/js/v2/core.js) — `GIQ` namespace; `GIQ.fmt` (esc, timeAgo, fmtTime, fmtDuration, fmtNumber); `GIQ.api` (get/post/put/patch/del + validateKey); `GIQ.apiKey` (load/save/clear in sessionStorage); `GIQ.toast`. `GIQ.state` and `GIQ.pages` initialized.
- [app/static/js/v2/router.js](app/static/js/v2/router.js) — hash router; `GIQ.router.{BUCKETS,SUBPAGES,DEFAULTS,SUBPAGE_LABELS,BUCKET_LABELS,BUCKET_ICONS,parseHash,resolve,dispatch,navigate,cleanup}`. Sub-page lists per the spec. Calls page-cleanup on nav-away.
- [app/static/js/v2/shell.js](app/static/js/v2/shell.js) — renders sidebar + topbar into `#app`; binds nav clicks, collapse toggle, activity-pill placeholder, API key submit (Enter or Connect button). Exposes `GIQ.shell.init/render/renderSidebar/renderTopbar`.
- [app/static/js/v2/components.js](app/static/js/v2/components.js) — empty stub; session 02 populates with shared components.
- [app/static/js/v2/explore.js](app/static/js/v2/explore.js) — registers `GIQ.pages.explore[*]` for all 9 sub-pages with stub renderers.
- [app/static/js/v2/actions.js](app/static/js/v2/actions.js) — same shape, 5 sub-pages.
- [app/static/js/v2/monitor.js](app/static/js/v2/monitor.js) — same shape, 11 sub-pages.
- [app/static/js/v2/settings.js](app/static/js/v2/settings.js) — same shape, 6 sub-pages.
- [app/static/js/v2/index.js](app/static/js/v2/index.js) — boot: load API key, `GIQ.shell.init()`, validate key in background, dispatch initial route.
- [docs/rebuild/handoffs/01-foundation.md](docs/rebuild/handoffs/01-foundation.md) — this file.

`.claude/launch.json` is gitignored (local-only). I updated `grooveiq-local.url` from `/dashboard` to `/static/dashboard-v2.html` so my own preview lands on the new shell; future-session machines will need to make the same edit if they want auto-landing.

The previously-untracked design handoff and rebuild docs are now committed alongside the scaffold so subsequent sessions inherit them on the branch.

## State of the dashboard at end of session

What works at `/static/dashboard-v2.html`:

- Dark-mode shell renders with the locked palette (`--bg #171821`, `--paper #292631`, `--accent #a887ce`, `--wine #9c526d`, etc.).
- Logo `groove`+`iq` (lavender `iq`); collapses to a single accented `g`.
- Sidebar shows all 4 buckets with correct icons (♪ ⚡ ◉ ⚙) and sub-page counts (9 · 5 · 11 · 6). Active bucket gets the lavender accent, the soft-lavender background, and the 2px-protruding left bar.
- Sidebar collapse to 60px and back; persists across reload via `localStorage.groove.nav.collapsed`.
- Activity pill: pulsing lavender dot, "3 active" + "pipeline · scan · 2 dl" sub-line, chevron. Click fires a placeholder toast ("wired in session 02"). Collapses to a circle with a "3" count badge.
- API key block above the search row: password input + Connect button + status indicator (`not connected` / `connected` / `invalid` in lavender or wine). Persists to `sessionStorage.giq.apiKey`. Submit on Enter or click. Validates against `GET /health`; toasts success/failure.
- Search row at bottom is a placeholder (disabled button) with magnifier + label + ⌘K hint.
- Topbar shows the active bucket's sub-pages as horizontally-scrolling tabs. Active tab gets the 2px lavender bottom border. SSE pill on the far right shows `SSE off` (grey) — session 02 wires real SSE.
- Hash routing works for all 31 sub-pages (9+5+11+6). Each shows the eyebrow / title / "Page: bucket → subpage — TBD" stub.
- Empty hash defaults to `#/monitor/overview`. Bucket clicks land on the bucket's default subpage (`recommendations` / `pipeline-ml` / `overview` / `algorithm`). Hash-only nav (e.g. paste `#/explore/charts`) works.
- Toast stack at bottom-right (auto-dismiss 4s default, 7s for errors, manual `×`). Reused by session-02+ pages.

What's stubbed:
- Activity pill popover (just toasts a placeholder).
- ⌘K command palette (search row is disabled; visual placeholder only).
- SSE pill is permanently off (no SSE bus until session 02).
- Every sub-page is a "TBD" stub.

## Decisions made (with reasoning)

- **Single namespace `window.GIQ`, no modules / build step.** Per `conventions.md`. Files communicate via shared globals; load order in `dashboard-v2.html` enforces the dependency graph (core → router → shell → components → bucket modules → index).
- **`sessionStorage` for the API key** (matches the task brief). Cleared when the tab closes; intentional.
- **`localStorage` for sidebar collapse** (matches `components.md` spec under `groove.nav.collapsed`).
- **`router.dispatch()` calls `cleanup()` on nav-away** so session-02+ pages can return a teardown closure (interval timers, SSE subscriptions). Cleanup error is caught + logged; never blocks navigation.
- **Sub-page slugs use kebab-case** (`pipeline-ml`, `system-health`, `recs-debug`, `lidarr-backfill`, `download-routing`, `text-search`, `music-map`, `user-diagnostics`). Display labels are looked up from `SUBPAGE_LABELS`. Splitting like this avoids ambiguity inside hashes and matches typical URL conventions.
- **Stat indicator + Connect button uses a single `submit()` for both Enter and click.** Disables the button while in flight to prevent double-submit; re-renders the sidebar after to refresh the status pill.
- **Page renderer signature: `function(root) -> cleanup | undefined`.** The router clears `root` between renders, so each renderer just `root.innerHTML = ...` or `root.appendChild(...)`. This matches the example in `conventions.md` § "Page pattern".
- **Inline event wiring via `addEventListener` not `onclick=` strings.** Per `conventions.md` ("inline `onclick` go through `GIQ.handle.foo(...)` not bare `foo(...)`"). The shell binds handlers directly after every `innerHTML` write.
- **`#app` is the only DOM hook.** Shell renders into it on first call; subsequent `renderSidebar` / `renderTopbar` patch the existing nodes' innerHTML rather than blowing the whole shell away. This keeps `#page-root` stable so page renderers don't see their root yanked from under them.
- **Did NOT pre-build a panel / stat-tile component.** Out of scope for session 01 per the README ("No fancy components yet"). Session 02 builds them.

## Gotchas for the next session

- **Local Python toolchain can't run uvicorn.** The repo's `.venv` is on Python 3.9 (predates `from datetime import UTC`); the only system Python with brew is 3.14, which has no project deps installed. For session 01 verification I served the static files via `python3.14 -m http.server 8001` from `app/`. That serves `/static/dashboard-v2.html` correctly but `/health` 404s, so the Connect-button validation always falls into the failure path locally. **For sessions that need real backend behaviour (02 onwards), either: (a) install project deps into a fresh 3.12+ venv, or (b) point preview at the remote dev server at 10.10.50.5:8000 once you've rsync'd / pushed `gui-rebuild` there.** Confirmed remote `/dashboard` returns 200; new `/static/dashboard-v2.html` will too once the branch is on the remote.
- **`preview_resize` "desktop" preset doesn't actually resize the viewport.** It reset to 198x700 (the iframe's natural size). Use explicit `width/height` (I used 1400×900). Doesn't break anything but produced a confusing first screenshot until I noticed.
- **Toast after async Connect:** the toast _does_ render (verified via `GIQ.toast(...)` directly), but I observed once that after a back-to-back fill+click sequence the post-validate toast wasn't in the DOM at the time of inspection. State + status pill updated correctly, so the user-visible signal is still there. Worth a glance in session 02 if you change the submit flow.
- **Sub-page slug collisions across buckets are intentional** — both Explore and Monitor have `charts`, both Actions and Monitor have `discovery` / `downloads` / `charts`. The router scopes by `bucket` so they don't conflict; just remember when wiring deep links from session 12.
- **The hash default redirect uses `window.location.hash = '#/monitor/overview'`.** That triggers a `hashchange` and re-dispatches. If you ever want to skip the extra dispatch, switch to `history.replaceState` then call `dispatch()` directly. Not currently a problem.
- **Activity pill ".collapsed::after" sets count to literal "3"** — placeholder. Session 02 must rewire this to read from real activity state.
- **`docs/rebuild/`, `docs/gui-rework-plan.md`, and `design_handoff_grooveiq_dashboard/`** were untracked on `main` at session start. They're now part of the session-01 commit on `gui-rebuild` so all subsequent sessions can read them. If `main` later acquires its own copy, expect a merge conflict at session 12 cutover.

## Open issues / TODOs

- The "3 active" copy in the activity pill is hard-coded in two places (`shell.js` body markup and the `.sidebar.collapsed .activity-pill::after` CSS). Centralise once session 02 has real data.
- The `apikey-block` in `shell.css` could stand to be clearer about which `border-color` token it uses on focus when light mode is in play (we're dark-only this session).
- No favicon — the page currently inherits the project's `/favicon.ico` route (204). Acceptable.
- Browser title is plain `GrooveIQ`. Session 12 may want per-page titles.

## Verification screenshots

Captured via `mcp__Claude_Preview__preview_screenshot` and inline in the session transcript:

1. Initial load — dark shell, Monitor active, Overview sub-tab, "SSE off" pill, expanded sidebar with API key block "NOT CONNECTED".
2. Settings → Users — confirms sub-page navigation; topbar shows the 6 Settings tabs.
3. Collapsed sidebar (60px) on Settings → Users — single-letter logo "g", icon-only nav items, collapsed activity pill rendering as a circle with "3".
4. Reload after collapse — sidebar still collapsed (`localStorage` persistence works), route preserved (`#/settings/users`).
5. Final return to `#/monitor/overview` — clean state for the next session to start from.

Functional checks evidenced via `preview_eval`:
- All 4 buckets navigable, each sub-page list correct (9 / 5 / 11 / 6 tabs).
- `localStorage.groove.nav.collapsed` toggles between `'1'` and `'0'`.
- `sessionStorage.giq.apiKey` is set on Connect; status pill updates `not connected` → `connected` / `invalid` based on `/health` response.
- Old `/static/index.html` still loads in isolation, references its own `style.css`, and `window.GIQ` is undefined there — no cross-contamination.
- Remote `http://10.10.50.5:8000/dashboard` returns 200.

## Time spent

≈ 70 min: reading prior docs (15) · scaffolding files (25) · verification + visual check loops (20) · hand-off note (10).

---

**For the next session to read:** Session 02 — Monitor → Overview at full fidelity + activity pill, see `docs/rebuild/02-overview.md`.

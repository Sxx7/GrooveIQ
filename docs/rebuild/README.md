# GrooveIQ dashboard rebuild — master plan

The web dashboard at `/dashboard` is being rewritten from scratch into a four-bucket information architecture (**Explore · Actions · Monitor · Settings**) using a locked design language (lavender + wine on charcoal, Inter Tight + JetBrains Mono, restrained colour). The work is split into **12 sessions**, each sized to fit a single Claude context window, with explicit hand-offs between them.

This README is the entry point. Read it first, then jump to the next un-done session.

## Inputs (read these first, in order)

1. **[gui-rework-plan.md](../gui-rework-plan.md)** — canonical IA, per-tab migration mapping, splits requiring deep links. Authoritative for "what goes where".
2. **[design_handoff_grooveiq_dashboard/README.md](../../design_handoff_grooveiq_dashboard/README.md)** — design hand-off doc with locked tokens, palette, typography, spacing, component specs.
3. **[design_handoff_grooveiq_dashboard/styles.css](../../design_handoff_grooveiq_dashboard/styles.css)** — token definitions to lift verbatim into the new dashboard.
4. **[design_handoff_grooveiq_dashboard/page-realistic.jsx](../../design_handoff_grooveiq_dashboard/page-realistic.jsx)** — high-fidelity reference for Monitor → Overview. Match exactly.
5. **[design_handoff_grooveiq_dashboard/primitives.jsx](../../design_handoff_grooveiq_dashboard/primitives.jsx), page-overview.jsx, page-lidarr.jsx, page-actions.jsx, page-recs.jsx** — wireframes for other surfaces. Apply the high-fidelity visual system from `page-realistic.jsx` when implementing them.

The JSX files are **design references, not production code**. Implement in vanilla JS — no React, no build step. (See [conventions.md](conventions.md).)

## Working agreement (apply on every session)

- **Branch:** all rebuild work happens on `gui-rebuild`. Create it on session 1; never merge to `main` until session 12.
- **Parallel dashboards:** the existing dashboard at `/dashboard` stays untouched until session 12 (cutover). The new one lives at `/static/dashboard-v2.html` (no backend changes — `/static/` is already mounted). After cutover, the old becomes `/static/dashboard-old.html` for rollback.
- **No backend changes** during the rebuild. Every Monitor / Action / Setting view maps to existing endpoints (see [api-map.md](api-map.md)). If you find you need a new endpoint, stop and flag it.
- **Hand-off discipline:** every session ends by writing `handoffs/NN-name.md` describing what landed, what state files are in, and any gotchas for the next session. The next session reads all prior hand-offs before starting.
- **Commit per session:** at session end, commit on `gui-rebuild` with the message `rebuild: session NN — <name>`. Do not amend prior commits.
- **Verify visually:** every UI session must use `preview_*` tools to load `/static/dashboard-v2.html` and check the result. No "looks right to me" without a screenshot.
- **Tokens come from the design hand-off** — never invent palette / type / spacing values. If a value isn't in the design hand-off, ask before guessing.

## Sessions

Each session is a self-contained prompt for a fresh Claude window. Status updates as work progresses.

| #  | Session | Status | Depends on | Notes |
|----|---------|--------|------------|-------|
| 01 | [Foundation](01-foundation.md) — shell, tokens, router, stub pages | done | — | Sets everything up; subsequent sessions assume this is done |
| 02 | [Monitor → Overview](02-overview.md) at full fidelity + activity pill | done | 01 | Canonical visual reference; do this before any other content |
| 03 | [Settings: versioned-config shell + Algorithm](03-settings-config-shell-algorithm.md) | done | 01, 02 | Shell is reused by sessions 04 too |
| 04 | [Settings: Routing + Backfill + Connections + Users](04-settings-rest.md) | done | 03 | Reuses versioned-config shell from 03 |
| 05 | [Actions bucket](05-actions.md) — 5 grouped pages | done | 01, 02 | Independent of Settings; can run in parallel with 03/04 |
| 06 | [Monitor: Pipeline + Models + Recs Debug](06-monitor-pipeline.md) | done | 02 | Heaviest visualization work |
| 07 | [Monitor: System Health + User Diagnostics + Integrations](07-monitor-health.md) | done | 02 | Independent of 06; can run in parallel |
| 08 | [Monitor: Downloads + Backfill + Discovery + Charts stats](08-monitor-ops.md) | done | 02 | Independent of 06, 07; can run in parallel |
| 09 | [Explore: track table + Recommendations + Tracks](09-explore-recs-tracks.md) | done | 02 | Extracts reusable track-table component |
| 10 | [Explore: Playlists + modal + Text Search + Music Map](10-explore-playlists-search-map.md) | done | 09 | Reuses track-table from 09 |
| 11 | [Explore: Radio + Charts + Artists + News](11-explore-radio-charts.md) | done | 09 | Radio is the most stateful surface |
| 12 | [Cross-cutting + mobile + cutover](12-cutover.md) | done | all above | Wires deep links; flips `/dashboard` to new HTML |

**Parallelism:** sessions 03+04 (Settings), 05 (Actions), 06+07+08 (Monitor), 09 → 10 → 11 (Explore) form four independent tracks once 02 is done. If you have multiple humans driving Claude windows, you can run those tracks concurrently. Otherwise run sequentially in the listed order.

## Shared references (every session reads these)

- **[conventions.md](conventions.md)** — file layout, naming, JS module pattern, CSS organisation, accessibility, browser support.
- **[components.md](components.md)** — specs for shared components (panel, stat tile, track table, versioned-config shell, integration card, jump link, LIVE badge, generate-playlist modal). Multiple sessions build or reuse these.
- **[api-map.md](api-map.md)** — endpoint inventory by page, so each session knows exactly which `/v1/...` calls it owns.
- **[handoffs/_template.md](handoffs/_template.md)** — fill in at the end of each session.

## "Done" definition for the whole rebuild

- `/dashboard` serves the new four-bucket UI in dark mode at full design fidelity.
- All current functionality preserved (no regressions).
- `gui-rebuild` is merged into `main` and the old dashboard is parked at `/static/dashboard-old.html`.
- Mobile responsive at ≥320px portrait.
- All cross-bucket deep links work (split pages have working "related →" rails).
- Activity pill shows real running jobs and deep-links to Monitor.
- All 12 hand-off notes filed.

## Rollback

If something is badly broken after cutover, swap the `index.html` symlink / route back to `dashboard-old.html`. Old code remains in the bundle until at least one week after cutover.

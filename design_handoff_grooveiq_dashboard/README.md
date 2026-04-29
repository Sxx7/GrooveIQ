# Handoff: GrooveIQ Dashboard Rework

## Overview

This package documents the new four-bucket information architecture and visual design for the **GrooveIQ** web dashboard. It covers a wholesale rework of the single-page dashboard at `/dashboard` (today: `app/static/index.html` + `app/static/js/app.js` + `app/static/css/style.css`).

Goals:
1. Reorganise 7 existing top-level tabs into 4 semantic buckets: **Explore · Actions · Monitor · Settings**.
2. Apply visual restraint — colour reserved for state that genuinely changes meaning, not decoration.
3. Adopt a moody dark-first aesthetic (lavender + wine on charcoal) with a derived light mode.
4. Make the layout responsive (smartphone portrait through wide desktop). PWA features are explicitly out of scope.

## About the design files

The files in this bundle (`*.html`, `*.jsx`, `*.css`) are **design references created in HTML/React** — prototypes showing intended look and behaviour. They are **not production code to copy directly**.

The task is to **recreate these designs in the GrooveIQ codebase**, which today is plain JavaScript + a single CSS file (no React, no build step). The developer should:
- Match the exact visual fidelity of `page-realistic.jsx` (the realistic Monitor → Overview mockup).
- Match the IA, layout, and behaviour shown in the wireframes for the other buckets.
- Use the existing vanilla-JS architecture in `app/static/js/app.js` — do **not** introduce React unless the team agrees to that build-tool change separately. Implement components as plain JS modules / functions that build DOM, matching the current codebase conventions.
- Reuse existing API endpoints (see `docs/API.md` and `CLAUDE.md`) — no backend changes are expected.

## Fidelity

**Mixed.**

- **High-fidelity:** `page-realistic.jsx` (Monitor → Overview). Colours, typography, spacing, and component styling are final and should be matched pixel-perfectly.
- **Low-fidelity (sketchy wireframes):** Lidarr Backfill split, Actions shapes, Recommendations, notes/legend. These show **structure, IA, layout, and copy** — apply the high-fidelity visual system from the realistic mockup when implementing them. Do not ship the sketchy aesthetic.

The full design canvas with all sections is in `GrooveIQ Wireframes.html` — open it locally to navigate everything.

## Information architecture (canonical)

Top-level nav: **Explore · Actions · Monitor · Settings**.

```
🎵 Explore     — what users do with the library
⚡ Actions      — one-shot operations (triggered, then forget)
📊 Monitor     — live state, history, debug, observability
⚙ Settings    — versioned configs + entity management
```

**Landing page:** Monitor → Overview replaces today's Dashboard tab.

Per-bucket sub-pages, splits, and migration notes are documented in full in `gui-rework-plan.md` (included in this folder). That document is canonical for the IA — read it end-to-end before implementing.

## Design tokens

These tokens are used everywhere. Define them once and reference via CSS custom properties.

### Palette (locked)

| Token | Dark hex | Light hex | Usage |
|---|---|---|---|
| `--bg` | `#171821` | `#f3f0f5` | Page background (deepest) |
| `--paper` | `#292631` | `#ffffff` | Card / panel surface |
| `--paper-2` | `#4d3e50` | `#f7f5f9` | Elevated surface, hover, active toggle pill |
| `--ink` | `#ece8f2` | `#171821` | Default text, headings |
| `--ink-2` | `#b8b0c4` | `#4d3e50` | Body text, secondary |
| `--ink-3` | `#7b6e7f` | `#7b6e7f` | Muted text, mono labels, sub-copy |
| `--accent` | `#a887ce` | `#a887ce` | Primary action, active nav, "ok" / live state |
| `--wine` | `#9c526d` | `#9c526d` | Warning, error, destructive, "stale" |
| `--line-soft` | `rgba(236,232,242,0.14)` | `rgba(41,38,49,0.18)` | Card borders |
| `--line-faint` | `rgba(236,232,242,0.06)` | `rgba(41,38,49,0.08)` | Row dividers |

Dark mode is the default and primary target. Light mode is supported but secondary.

**Colour rules (enforce):**
- Use `--accent` (lavender) for: primary buttons, active nav state, live indicators, "ok"/"ready" model state, success deltas.
- Use `--wine` for: error, destructive, "stale" model state, "failed" queue rows, negative deltas.
- Everything else is monochrome (text greys + surface charcoals).
- Source / event-type / reranker badges are **typographic** — uppercase JetBrains Mono in a faint outline, no fill colour.
- Score bars are monochrome (lavender on `--line-faint` track).
- Heatmaps and Music Map keep colour because colour itself is the data.

### Typography

| Family | Use | Weights |
|---|---|---|
| **Inter Tight** | Display: large numbers, page titles, panel titles | 600, 700 |
| **Inter** | Body, nav, labels | 400, 500, 600 |
| **JetBrains Mono** | Eyebrows, timestamps, IDs, axis labels, badges | 400, 500 |

Eyebrow style: `JetBrains Mono`, 10px, uppercase, letter-spacing 0.10–0.14em, colour `--ink-3`.

Page title: `Inter Tight`, 26px, weight 600, letter-spacing -0.02em.

Panel title: `Inter`, 13px, weight 600.

Stat value: `Inter Tight`, 22px, weight 600, letter-spacing -0.02em.

Body: `Inter`, 13px, line-height 1.45.

### Spacing

Page padding: 22px top / 28px sides. Panel padding: 16px. Stat-tile padding: 14px 16px. Grid gap between panels: 14px. Grid gap inside stat row: 12px.

### Radius

Panels: 10px. Buttons / stat tiles: 7–8px. Pills / chips: 999px. Mono badges: 3px.

### Borders

Panel: `1px solid var(--line-soft)`. Row dividers: `1px solid var(--line-faint)`. Active accent bar (nav): 2px wide, full vertical, `--accent`.

## Screens

### 1. Shell — collapsible sidebar (universal)

Lives on every page. The single nav scaffold for the app.

- **Width:** 220px expanded / 60px collapsed. Animate `width` 200ms ease.
- **Background:** `--paper`. Right border `1px solid --line-faint`.
- **Logo:** "groove" + "iq" in `Inter Tight` 17/700, "iq" coloured `--accent`. When collapsed: single "g" in `--accent`, centered.
- **Collapse toggle:** chevron `«` top-right when expanded, `»` centered below logo when collapsed. Colour `--ink-3`.
- **Nav items (4):** Explore (♪, 9), Actions (⚡, 5), Monitor (◉, 11), Settings (⚙, 6). Count is the sub-page count.
  - Idle: `--ink-2` text, no background.
  - Active: `--accent` text, `rgba(168,135,206,0.14)` background, plus a 2px-wide vertical accent bar protruding 12px to the left.
  - Item padding: 9px 11px. Gap between items: 2px. Icon column: 14px wide, centered.
  - Count badge: JetBrains Mono 10px, padding 1px 6px, radius 999px. Background `rgba(236,232,242,0.05)` idle / `rgba(168,135,206,0.14)` active.
  - Collapsed: items become 40px-tall icon-only squares, centered.
- **Activity pill** (lower in the sidebar, above search):
  - Background `rgba(168,135,206,0.10)`, border `1px solid rgba(168,135,206,0.30)`, radius 10px, padding 10px 12px.
  - Lavender pulsing dot (1.6s ease-in-out infinite, opacity 1→0.5, scale 1→1.3) + "3 active" + sub-line "pipeline · scan · 2 dl" + chevron.
  - Click expands a 320px-wide popover above with rows for each active job (Pipeline run · Downloads · Library scan etc.), each with icon, label, sub-text, optional LIVE badge, and "View →" deep-link to Monitor.
  - When sidebar collapsed: pill collapses to a circle with a "3" count.
- **Search row** at bottom: idle background `rgba(236,232,242,0.04)`, magnifier icon, "Search" label, ⌘K keyboard hint in a faint mono badge.

### 2. Top bar (subnav)

Sits inside the main column, above the page header. Height 46px, border-bottom `1px solid --line-faint`.

- Horizontal scrolling tabs of the bucket's sub-pages (Monitor's 11, Explore's 9, etc.).
- Idle tab: `--ink-3` text, weight 450, 12px.
- Active tab: `--ink` text, weight 600, 2px solid `--accent` bottom border.
- Tab padding: 0 14px. No gap.
- Right side: SSE status pill — small lavender circle + "SSE live" in `--accent`, on `rgba(168,135,206,0.10)` with `rgba(168,135,206,0.25)` border, radius 999px.

### 3. Monitor → Overview (HIGH FIDELITY — match exactly)

See `page-realistic.jsx` for the canonical implementation.

**Page header** (padding 22px top / 28px sides, flex row, end-aligned):
- Left: eyebrow "MONITOR" + page title "Overview" (Inter Tight 26/600, -0.02em).
- Right: "last update · 2s ago" in mono `--ink-3` + range toggle group (1h · 24h · 7d · 30d). Toggle group: background `--paper`, padding 2px, radius 7px, border `1px solid --line-faint`. Active item: background `--paper-2`, weight 600, radius 5px, padding 4px 12px.

**Stat row** (6-col grid, gap 12px):
Six tiles. Each: background `--paper`, border `1px solid --line-faint`, radius 10px, padding 14px 16px.
- Eyebrow label
- Value (Inter Tight 22/600)
- Delta line (11px) — `--accent` for up, `--wine` for down, `--ink-3` for flat. Prefix with ↑ ↓ as appropriate.

Six tiles: Events · Users · Tracks · Playlists · Events/hr · Ranker. The Ranker tile shows "ready" as the value and "ndcg 0.412" as the delta.

**2-col body** (grid `2fr 1fr`, gap 14px):

Left column (stack, gap 14px):
1. **Event ingest panel** — sub "play_end · like · skip · pause · etc · last 24h · 5m bins" + action "View full breakdown →" (lavender). Body: smooth area chart, dual series.
   - Series 1 (all events): lavender stroke 2px, gradient fill `#a887ce` 0.45→0 top-to-bottom.
   - Series 2 (engagement): wine stroke 1.5px @ 0.7 opacity, gradient fill `#9c526d` 0.35→0.
   - Smoothing via cubic Beziers between points.
   - Three horizontal gridlines at 25/50/75% in `rgba(236,232,242,0.04)`, dasharray `2 4`.
   - Axis labels below: 7 timestamps in mono 9px `--ink-3`.
   - Legend below: two coloured 8×8 squares + label.

2. **Top tracks + Event types** (sub-grid `1.1fr 1fr`, gap 14px):
   - **Top tracks panel:** rank chip (22×22 `--paper-2`, mono number), track + artist, mini bar (60×3, `--accent` on `--line-faint`), play count (mono, right-aligned, 32px wide). Row padding 7px 0, divider `--line-faint`.
   - **Event types panel:** horizontal bar list. Each row: label (mono 10px) + value (mono 10px `--ink-3`) + 5px-tall track with filled bar. Bar colour: `--accent` for play_end/like, `--ink-3` for skip/pause/volume, `--wine` for dislike.

3. **Recent events panel** with LIVE badge — flat list, 4 rows. Each row: timestamp (mono 60px), event-type chip (mono 9px uppercase, faint outline, 70px centered), user (lavender 50px), track (truncated), duration (mono right). LIVE badge style: see component spec below.

Right column (stack, gap 14px):
1. **Models panel** ("See all →" action). 6 rows. Each row: 7px dot (lavender for ready, wine for stale), name + sub-line in mono 10px, state chip mono 9px uppercase right-aligned (`--accent` or `--wine`).
2. **Library scan panel** with LIVE badge. Progress label + percent (mono lavender). 6px-tall progress bar with linear gradient `--paper-2`→`--accent`. Below: 2×2 mini-stat grid (Found · New · Updated · Removed). Each cell: `rgba(236,232,242,0.04)` background, radius 6px, padding 8px 10px, mono eyebrow + Inter Tight 16/600 value.
3. **Quick run panel** ("jumps to Actions" sub). 4 rows. Each row: name + sub-line (e.g. "14m ago · ok"), arrow → on the right in `--accent`.

### 4. Lidarr Backfill — the triple split (LOFI)

Today's Lidarr Backfill page fragments across all three of Settings, Actions, and Monitor. See `page-lidarr.jsx` for layout.

**Pattern:** every split page has a "related →" rail at the top (background `--paper`, border `1.5px dashed --line-soft`, radius 8px) with **two cross-link pills** to the other two surfaces.

- **Settings → Lidarr Backfill Config:** versioned-config shell (collapsible groups, slider+number-input fields, header buttons: History · Diff · Save & Apply). Cross-links: `Actions` queue management, `Monitor` stats & ETA.
- **Actions → Discovery → Lidarr Backfill Queue:** filter chips (All · Queued · In flight · Failed) + queue table with state chips. Header buttons: Pause · ▶ Run now. Per-row actions: retry · skip · forget. Cross-links: `Settings` edit config, `Monitor` live stats.
- **Monitor → Lidarr Backfill:** 6-stat grid (Missing · Complete · Failed · Capacity · ETA · Run #) + throughput bar chart. Cross-links: `Settings` edit config, `Actions` manage queue.

State chips use the typography rule: mono 9px uppercase, with `--accent` background for "in flight", `--wine` for "failed", outline-only for "queued".

### 5. Actions bucket shape (LOFI — design decision needed)

Three shapes shown side by side in `page-actions.jsx`. **Recommended shape: B (grouped pages)** — matches the Settings UX shape and groups related triggers.

Each Actions page is a "trigger + last-result + jump-to-Monitor" pattern. The action fires; status display lives in Monitor.

**Action card spec (shape B):**
- Background `--paper`, border `1px solid --line-soft`, radius 10px, padding 14px.
- Top row: name (Inter 14/600) + state dot (lavender for last-run-ok, wine for destructive) on the left; "▶ Run" button (`--accent` background, white text, radius 7px, padding 7px 14px) on the right.
- Description line (12px `--ink-2`).
- Sub-line (mono 10px `--ink-3`) — e.g. "last run · 14m ago".

### 6. Explore → Recommendations (LOFI)

See `page-recs.jsx`.

- Page header: eyebrow + title + filter row (user dropdown · seed track · limit · "Get Recs" primary button).
- Cross-link banner: "request_id 7f2a-3c8e" + "Debug this request →" (jumps to Monitor → Recs Debug).
- Results table: track + artist, source chip (mono 9px outline), score bar (40×4 monochrome), BPM (mono), mood, per-row "debug→" link in lavender.
- Source chips are the typographic kind — never coloured pills.

### 7. Activity pill (universal)

Documented in section 1 (Shell). Keep behaviour identical on every page — single source of truth, lives in the sidebar.

## Reusable components to build

These exist multiple times today and should become shared components in the new GUI:

1. **Track table** — used in Tracks, Playlists detail, Recommendations, Charts, Audit. Same columns, same sort logic. Make one component, parameterise the visible columns.
2. **Generate Playlist modal** — opened from Tracks, Playlists, Text Search, Music Map. Already shared today; keep shared.
3. **Versioned-config shell** — Algorithm, Download Routing, Lidarr Backfill all share: Save & Apply / Discard / Reset / Export / Import / History / Diff buttons in header + collapsible groups + slider+number-input fields + per-field default indicator + MODIFIED/RETRAIN badges. Build one shell, three configs plug into it.
4. **Integration card** — used in Settings → Connections (snapshot) and Monitor → Integrations (live). One component, two data sources.
5. **User dropdown** — used in Recommendations, Radio, Audit, News. Already centralised; keep.
6. **Stat tile** — see Overview spec.
7. **Panel** — title + sub + optional action link + optional LIVE badge + body slot.
8. **LIVE badge** — `rgba(168,135,206,0.16)` background, 2px-margin lavender 5×5 dot + "LIVE" in mono 8px weight 700, letter-spacing 0.1em, padding 2px 6px, radius 3px.
9. **Cross-link pill** — used on every split page. Format: small grey label ("Actions") + main label ("Manage queue") + arrow.

## Interactions & behaviour

- **Sidebar collapse:** persist collapsed state in `localStorage` under `groove.nav.collapsed`. Animate width 200ms ease.
- **Activity pill:** poll/SSE driven. Click toggles a 320px popover. Each row deep-links to the relevant Monitor surface.
- **Range toggle (Overview):** 1h / 24h / 7d / 30d. Refetches event-ingest series on change.
- **SSE indicator:** lavender pulsing dot whenever SSE is connected. Greys out + label changes to "SSE off" when disconnected.
- **Cross-bucket deep links:** every link from Actions → Monitor that triggers a long-running job should land on the corresponding Monitor surface with SSE auto-connected (preserve today's `runPipelineFromTab()` behaviour).
- **Save & Apply (Algorithm):** triggers pipeline reset. Show toast "Saved. Pipeline reset triggered →" with jump to Monitor → Pipeline. Don't auto-redirect.

## Responsive

The dashboard must work on smartphones in portrait. **Out of scope:** PWA / install / offline / push.

Breakpoints (suggested):
- ≥ 1100px: full sidebar + 2-col body (default).
- 700–1099px: collapsed sidebar (icon-only) + single-column body, panels stack.
- < 700px: bottom tab bar replaces sidebar (4 buckets). Sub-page tabs become horizontal-scroll chips. Tables become card lists.

Mobile-aware components:
- **Track tables:** horizontal-scroll OR collapse to card layout below 700px.
- **Music Map:** pinch-zoom + tap-to-select. Acceptable to show a "best on desktop" notice if implementation cost is high.
- **Pipeline flow diagram:** vertical stack on small screens.
- **Versioned-config 2-col grids:** single column.
- **Diff modals:** stack vertically.

## State

Per-page state to manage:
- Nav: `currentBucket`, `currentSubpage`, `sidebarCollapsed`, `activityPillOpen`.
- Overview: `range` (1h/24h/7d/30d), `events`, `topTracks`, `models`, `scanStatus`, `quickRuns`.
- Lidarr: `queueFilter`, `queueRows`, `stats`, `eta`, `config` (versioned).
- Recs: `userId`, `seedTrack`, `limit`, `results`, `requestId`.
- Activity: `runningJobs[]` (pipeline · scan · downloads · etc.), polled or SSE-pushed.

## API

No new endpoints required. Every Action and Monitor view maps to existing endpoints — see `docs/API.md` and the API endpoints section in `CLAUDE.md`.

## Files in this bundle

| File | What |
|---|---|
| `README.md` | This document |
| `gui-rework-plan.md` | Canonical IA + per-tab migration notes (read first) |
| `GrooveIQ Wireframes.html` | Full design canvas — open locally to navigate everything |
| `styles.css` | Token definitions (colour, type, spacing). Lift the `:root` and `[data-theme="dark"]` blocks directly. |
| `page-realistic.jsx` | **High-fidelity reference** for Monitor → Overview |
| `page-overview.jsx` | Wireframe layout reference for Monitor → Overview |
| `page-lidarr.jsx` | Wireframe for the 3-bucket Lidarr split |
| `page-actions.jsx` | Wireframe for the 3 Actions shapes (recommend B) |
| `page-recs.jsx` | Wireframe for Explore → Recommendations |
| `primitives.jsx` | Reference implementations of nav, subnav, activity pill, stat tile, jump links |
| `design-canvas.jsx`, `tweaks-panel.jsx` | Design-tool scaffolding — ignore for implementation |

## Implementation order (suggested)

Mirrors the phasing in `gui-rework-plan.md` §8:

1. **Phase 1** — restructure HTML/JS into 4 top-level routes; move existing tab contents into the new buckets without redesign. Verifies the IA works in vanilla JS.
2. **Phase 2** — apply the design tokens (palette, type, spacing) globally. Then component-by-component visual rework starting with the shell (sidebar, subnav, activity pill).
3. **Phase 3** — Monitor → Overview at full fidelity. This is the landing page.
4. **Phase 4** — Lidarr Backfill triple split. This is the most fragmented page; nailing it validates the cross-link pattern.
5. **Phase 5** — remaining Monitor / Explore / Actions / Settings pages.
6. **Phase 6** — extract the shared components (track table, versioned-config shell, integration card).
7. **Phase 7** — global features (⌘K palette) if approved.

## Open questions for product

These are flagged in `gui-rework-plan.md` §7 and should be resolved before or during implementation:

1. Landing route logic (Monitor → Overview vs Settings → Connections for new installs).
2. Whether ⌘K command palette ships in v1 or later.
3. Mobile parity for Music Map (full responsive vs "best on desktop" notice).
4. User-context topology — Settings → Users → {user} ↔ Monitor → User Diagnostics → {user} as separate routes vs shared header tab bar.
5. Whether Download Routing wants drag-reorder rather than the standard versioned-config form.

# Shared components

Specs for components used across multiple pages. Each lists which session builds it and which sessions reuse it.

## Design tokens (locked)

Palette / typography / spacing are lifted directly from `design_handoff_grooveiq_dashboard/README.md` and `styles.css`. **Do not invent values.**

### Palette

| Token | Dark | Light | Use |
|---|---|---|---|
| `--bg` | `#171821` | `#f3f0f5` | Page background |
| `--paper` | `#292631` | `#ffffff` | Card / panel surface |
| `--paper-2` | `#4d3e50` | `#f7f5f9` | Elevated, hover, active toggle |
| `--ink` | `#ece8f2` | `#171821` | Default text |
| `--ink-2` | `#b8b0c4` | `#4d3e50` | Body, secondary text |
| `--ink-3` | `#7b6e7f` | `#7b6e7f` | Muted, mono, sub-copy |
| `--accent` | `#a887ce` | `#a887ce` | Primary, active nav, ok, live |
| `--wine` | `#9c526d` | `#9c526d` | Error, destructive, stale |
| `--line-soft` | `rgba(236,232,242,0.14)` | `rgba(41,38,49,0.18)` | Borders |
| `--line-faint` | `rgba(236,232,242,0.06)` | `rgba(41,38,49,0.08)` | Row dividers |

**Colour rules (enforce):**
- `--accent` for primary buttons, active nav, live indicators, ok/ready model state, success deltas.
- `--wine` for error, destructive actions, stale model state, failed rows, negative deltas.
- Everything else is monochrome (text greys + surface charcoals).
- Source / event-type / reranker badges are **typographic** — uppercase JetBrains Mono in a faint outline, no fill colour.
- Score bars are monochrome (lavender on `--line-faint`).
- Heatmaps and Music Map keep colour because colour itself is the data.

### Typography

| Family | Use | Weights |
|---|---|---|
| Inter Tight | Display: large numbers, page titles, panel titles | 600, 700 |
| Inter | Body, nav, labels | 400, 500, 600 |
| JetBrains Mono | Eyebrows, timestamps, IDs, axis labels, badges | 400, 500 |

| Style | Spec |
|---|---|
| Eyebrow | JetBrains Mono · 10px · uppercase · letter-spacing 0.10–0.14em · `--ink-3` |
| Page title | Inter Tight · 26px · 600 · letter-spacing -0.02em |
| Panel title | Inter · 13px · 600 |
| Stat value | Inter Tight · 22px · 600 · letter-spacing -0.02em |
| Body | Inter · 13px · line-height 1.45 |

### Spacing

Page padding `22px` top / `28px` sides. Panel padding `16px`. Stat-tile padding `14px 16px`. Grid gap between panels `14px`. Grid gap inside stat row `12px`.

### Radius

Panels `10px`. Buttons / stat tiles `7-8px`. Pills / chips `999px`. Mono badges `3px`.

---

## Components

### Shell — sidebar (session 01)

- Width: `220px` expanded, `60px` collapsed. `transition: width 200ms ease`.
- Background `--paper`. Right border `1px solid --line-faint`.
- Logo: "groove**iq**" Inter Tight 17/700, "iq" coloured `--accent`. Collapsed: single "g" in `--accent`, centered.
- Collapse toggle `«` top-right when expanded; `»` centered below logo when collapsed. Colour `--ink-3`.
- Persist collapsed state in `localStorage` under `groove.nav.collapsed`.

**Nav items (4):** Explore (♪, 9), Actions (⚡, 5), Monitor (◉, 11), Settings (⚙, 6). Sub-page count in mono badge.

- Idle: `--ink-2` text, no background.
- Active: `--accent` text, `rgba(168,135,206,0.14)` background, plus 2px-wide vertical accent bar protruding 12px to the left.
- Item padding `9px 11px`. Gap `2px`. Icon column `14px` centered.
- Count badge: JetBrains Mono 10px, padding `1px 6px`, radius 999px. Background `rgba(236,232,242,0.05)` idle / `rgba(168,135,206,0.14)` active.
- Collapsed: items become 40px-tall icon-only squares, centered.

**Activity pill** (above search):
- Background `rgba(168,135,206,0.10)`, border `1px solid rgba(168,135,206,0.30)`, radius 10px, padding `10px 12px`.
- Lavender pulsing dot + "3 active" + sub-line "pipeline · scan · 2 dl" + chevron. Pulse: 1.6s ease-in-out infinite, opacity 1→0.5, scale 1→1.3.
- Click → 320px-wide popover above with rows for each active job. Each row: icon, label, sub-text, optional LIVE badge, "View →" deep-link to Monitor.
- Collapsed sidebar: pill becomes a circle with a count.

**Search row** at bottom: idle background `rgba(236,232,242,0.04)`, magnifier icon, "Search" label, ⌘K hint in faint mono badge.

### Shell — top bar (session 01)

Sits inside the main column, above the page header. Height 46px, border-bottom `1px solid --line-faint`.

- Horizontal scrolling tabs of the bucket's sub-pages.
- Idle tab: `--ink-3`, 12px, weight 450.
- Active tab: `--ink`, weight 600, 2px solid `--accent` bottom border.
- Tab padding `0 14px`. No gap.
- Right side: SSE status pill — small lavender circle + "SSE live" in `--accent`, on `rgba(168,135,206,0.10)` with `rgba(168,135,206,0.25)` border, radius 999px.

### Page header (session 02)

Padding `22px 28px 16px`, flex row, end-aligned.

- Left: eyebrow (e.g. "MONITOR") + page title (Inter Tight 26/600).
- Right: page-specific controls (range toggle, action buttons, etc.). Format depends on page.

### Stat tile (session 02)

- Background `--paper`, border `1px solid --line-faint`, radius 10px, padding `14px 16px`.
- Eyebrow label.
- Value (Inter Tight 22/600).
- Optional delta line (11px) — `--accent` for up, `--wine` for down, `--ink-3` for flat. Prefix with `↑` `↓`.

### Panel (session 02)

- Background `--paper`, border `1px solid --line-faint`, radius 10px, padding 16px.
- Header row: title (Inter 13/600) + optional sub (mono 10px `--ink-3`) on the left; optional action link (`--accent` 11px) on the right.
- Optional LIVE badge next to title (see below).
- Body slot.

### LIVE badge (session 02)

- `rgba(168,135,206,0.16)` background.
- 5×5 lavender dot (no animation on the badge dot itself; the parent context uses pulsing where appropriate) + "LIVE" in mono 8px weight 700, letter-spacing 0.1em.
- Padding `2px 6px`, radius 3px.
- Inline-flex with 4px gap between dot and text.

### Cross-link "jump" pill (session 03+)

Used on every split page (Lidarr Backfill triple split, etc.).

- Format: small grey label ("Actions") + main label ("Manage queue") + arrow `→`.
- Border `1.25px dashed --line-soft`, radius 999px, padding `2px 9px`.
- Background `--paper`. Colour `--accent` for label, `--ink-4` for prefix.

### "Related →" rail (sessions 03, 04, 06, 08, 09)

Sits at the top of each split page. Background `--paper`, border `1.5px dashed --line-soft`, radius 8px, padding `8px 12px`.

Format: hand-styled "related →" label (Inter 14, `--ink-3`) + 2 cross-link pills.

Example for Settings → Lidarr Backfill Config: pills go to Actions queue + Monitor stats.

### Versioned-config shell (session 03; reused 04)

The shared shell for Algorithm, Download Routing, and Lidarr Backfill configs.

**Header:**
- Eyebrow "VERSIONED CONFIG · v{n}".
- Title (e.g. "Algorithm Config") in Inter Tight 18/600.
- Right: button row — `History`, `Diff`, `Reset`, `Discard` (conditional, when changes exist), `Save & Apply` (primary, disabled when no changes), `Export`, `Import`.

**Optional retrain banner** (warning style, `--wine-soft` background, mono 11px text): "Changes include parameters that trigger a full model retrain."

**Groups:** collapsible sections.
- Header row: chevron, group label (Inter 12/600), `RETRAIN` badge (conditional), `MODIFIED` badge (conditional, when group has unsaved changes), sub-text.
- Body: 2-col grid (single-col below 700px) of fields.

**Field:**
- Label + optional `RETRAIN` per-field badge.
- Slider (full-width, `--accent` thumb, `--line-faint` track) + numeric input box (with +/− spinner buttons, mono).
- Bidirectional sync between slider and input.
- Description (Inter 11px `--ink-3`).
- "default: {x}" indicator (Inter 10px `--accent`, only shown when current ≠ default).
- Modified state: blue border on the field row.

**Modals:**
- History — table of versions (version, name, timestamp, active flag, Activate / Diff buttons).
- Diff — side-by-side comparison of two configs.
- Import — file input (hidden, button-triggered) + optional name input + Import button.

**APIs (parameterized per config):**
- GET `/v1/{kind}/config` (active)
- GET `/v1/{kind}/config/defaults` (defaults + group metadata for GUI)
- GET `/v1/{kind}/config/history`
- PUT `/v1/{kind}/config` (save → new version)
- POST `/v1/{kind}/config/reset`
- POST `/v1/{kind}/config/activate/{version}`
- GET `/v1/{kind}/config/export`
- POST `/v1/{kind}/config/import`

`{kind}` is `algorithm`, `downloads/routing`, or `lidarr-backfill`.

### Track table (session 09; reused 10, 11)

Used in Tracks, Playlists detail, Recommendations, Charts, Audit. Same columns, same sort logic.

**Default columns:** rank (optional) · cover (optional) · title · artist · album (optional) · BPM · key · energy bar · danceability · valence · mood · duration · version · track ID.

Each instance picks which columns to show via a config object. Sort by clicking column headers. Pagination via prev/next + "showing X-Y of Z" footer.

**Below 700px:** collapse to card layout — title + artist on top line, sub-line with BPM · key · mood · duration. No table.

### Generate Playlist modal (session 10; reused 09, 10, 11)

Opened from Tracks, Playlists, Text Search, Music Map (and from the "Generate" button on Recommendations as a future improvement).

Fields:
- Name (text, default "My Playlist")
- Strategy (dropdown): flow / mood / energy_curve / key_compatible / path / text. Conditionally shows/hides fields below.
- Seed Track ID (text, for flow / path / key_compatible).
- Target Track ID (text, for path).
- Text Prompt (text, for text). Note: requires CLAP enabled and backfilled.
- Mood (dropdown, for mood): happy / sad / aggressive / relaxed / party.
- Curve (dropdown, for energy_curve): ramp_up_cool_down / ramp_up / cool_down / steady_high / steady_low.
- Max Tracks (number, default 25, min 5, max 100).

Buttons: Cancel, Generate (primary).

Calls `POST /v1/playlists`.

### Integration card (sessions 04 + 07)

Used in Settings → Connections (snapshot, read-only) and Monitor → Integrations (live probe).

Per-card shape:
- Header: icon + name + type/version (if any) + status badge.
- Description (one-line purpose statement).
- Details (configured fields: URL, scrobbling, etc.) — only shown in Settings or Monitor, depending on parent.
- Error message (Monitor only, if probe failed).
- Configure hint (Settings only, if not configured): "Set the required environment variables in your `.env` file to enable this integration."

Same visual shell, two data sources. Build the component once, parameterize.

### SSE bus (session 02)

Single connection to `/v1/pipeline/stream`. Re-broadcasts events to subscribed page handlers. Auto-reconnect with exponential backoff. Indicator (lavender pulse vs grey) lives in the topbar SSE pill.

```js
GIQ.sse.subscribe('step_start', handler);  // returns unsubscribe fn
GIQ.sse.connect();
GIQ.sse.disconnect();
```

### Toast (session 02)

Bottom-right stack, auto-dismiss 4s (7s for errors). Types: success / error / warning / info. Manual dismiss `×`.

Already exists in old `app.js:notify(...)` — port the behaviour to `GIQ.toast(message, kind, duration?)`.

## Responsive

| Breakpoint | Layout |
|---|---|
| ≥ 1100px | Full sidebar (220px) + 2-col body. Default. |
| 700–1099px | Collapsed sidebar (60px) + single-column body, panels stack. |
| < 700px | Bottom tab bar (4 buckets) replaces sidebar. Sub-page tabs become horizontal-scroll chips. Tables become card lists. Versioned-config 2-col grids → single col. Diff modals stack. Pipeline flow vertical stack. |

Music Map at < 700px: render with pinch-zoom + tap-to-select, OR show "Best on desktop" notice (per design hand-off open question 3 — implementer's call, prefer the notice for v1 to limit scope).

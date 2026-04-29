# GrooveIQ GUI Rework Plan

Hand-off document for design. Captures the current state of the web dashboard, the new four-bucket information architecture, and per-feature migration notes including splits and cross-bucket deep links.

> **Working assumptions (locked in 2026-04-29):**
> 1. Four top-level buckets: **Settings · Actions · Monitoring/Debug · Content/Explore**
> 2. Settings/Actions stay separate even when coupled — backfill config etc. is "set once, run with it" by design
> 3. Settings only contains the three already-versioned configs + Connections snapshot + Users + Onboarding. Env-var-driven config stays env-var-driven (no GUI editor)
> 4. Connections splits: configured-services snapshot → Settings, live health probes → Monitoring
> 5. Users (CRUD + onboarding + per-user Last.fm) → Settings; user diagnostic surfaces (taste profile, history, sessions) → Monitoring
> 6. Responsive layout — dashboard must work on smartphones (tall portrait aspect ratios). PWA features (service worker, install, offline) are explicitly out of scope
> 7. Visual restraint — current design is colour-overloaded; new design reserves colour for meaningful signals only

---

## 1. Today's structure (what's there now)

Single-page dashboard at `/dashboard` (`app/static/index.html` + `app/static/js/app.js` ~5,200 LOC + `app/static/css/style.css` ~1,800 LOC).

**7 top-level tabs:**

| Tab | Role today | Notes |
|---|---|---|
| Dashboard | System overview + library scan | Mixes scan triggers with scan status |
| Pipeline | Flow diagram + step detail + run history + SSE | Mixes "Run Pipeline" / "Reset" buttons with observability |
| Content | 10 sub-tabs: Recommendations, Tracks, Playlists, Radio, Text Search, Music Map, Charts, Discovery (3 sub-sub-tabs), News, Audit | Bag of everything; some are content, some are debug, some are actions |
| Users | List + detail (taste profile, Last.fm, interactions, history, sessions) | User CRUD mixed with diagnostic views |
| Connections | 7 integration status cards (Media Server, Lidarr, spotdl, streamrip, slskd, Last.fm, AB) | Mixes "what's configured" with "is it healthy right now" |
| Algorithm | 7 collapsible config groups, ~78 tunables, versioning, history, diff, import/export | Pure settings page — no migration except moving under Settings hub |
| Downloads | Routing config + live queue + telemetry + multi-search test | Mixes config, monitoring, and user-triggered actions |

**Pain points the rework is solving:**
- Settings buried in three different places (Algorithm tab, Discovery → Lidarr Backfill sub-sub-tab, Downloads tab)
- "Run X" buttons scattered across 5+ tabs (Dashboard, Pipeline, Discovery, Charts, Lidarr Backfill, Downloads)
- Content/browse surfaces (Recommendations, Tracks, Playlists, Radio, Music Map) mixed with debug surfaces (Audit, Pipeline)
- Connections card reused for "configured?" and "live health?" with no clear distinction
- Live queue + routing config compete for attention on the Downloads tab
- "Generate Playlist" modal duplicated across four tabs
- User detail page mixes admin (rename, delete) with diagnostic (taste profile, history) with content (Get Recs jump)

---

## 2. New IA — four buckets

### Top-level nav

```
🎵 Explore     (Content/Explore — what users do with the library)
⚡ Actions      (one-shot operations — triggered, then forget)
📊 Monitor      (live state, history, debug, observability)
⚙  Settings    (versioned configs + entity management)
```

> **Landing page:** Monitor → Overview replaces today's Dashboard tab. Explore is the "use the product" entry point.

### Sub-pages per bucket

**🎵 Explore**
- Recommendations (per-user feed; same controls as today)
- Radio (start session, drift, feedback)
- Playlists (browse + view + delete; "Generate" button → opens shared modal)
- Tracks (search, browse, sort, paginate; "Generate from track" → modal)
- Text Search (CLAP prompt → results)
- Music Map (UMAP scatter + click two tracks → path playlist)
- Charts (browse Last.fm charts; filter scope/type)
- Artists (NEW — promote artist detail from inside user-Last.fm card to a first-class explore page)
- News (Reddit feed, planned)

**⚡ Actions** — grouped by domain

| Group | Actions |
|---|---|
| Pipeline & ML | Run Pipeline · Reset Pipeline · Backfill CLAP · Cleanup Stale Tracks |
| Library | Scan Library · Sync IDs (Navidrome/Plex) |
| Discovery | Run Lidarr Discovery · Run Fill Library · Run Lidarr Backfill (now) · Soulseek Bulk Download |
| Charts | Build Charts (now) · Download chart track (per-row, deep link from Explore → Charts) |
| Downloads | Search & Download (multi-agent test) · Cancel in-flight · Retry / Skip / Forget queue rows |

Each Actions page is a simple "trigger + last-result + jump-to-Monitor" pattern. The action itself fires; status display lives in Monitor.

**📊 Monitor** — grouped

| Group | Pages |
|---|---|
| Overview | System stats (events, users, tracks, playlists, events/h, model readiness summary) — replaces today's Dashboard |
| Pipeline | Flow diagram + step detail + run history + SSE stream + Errors panel |
| Models | Ranker, CF, Embeddings, SASRec, Session GRU, Last.fm Cache readiness cards (separated from Pipeline tab) |
| System Health | Event ingest sparkline · Library coverage · Listening activity timeline · User engagement leaderboard · Library scan status |
| Recs Debug | Recommendation Audit (sessions list + request detail + replay) · Debug Recs (live `?debug=true` trace) · Per-step pipeline detail (sessionizer, scoring, taste, ranker) |
| User Diagnostics | Taste profile explorer · User listening history · User sessions (drill-in from Settings → Users → user) |
| Integrations | Live health probes for all 7 integrations (mirrors Settings → Connections layout but with live status) |
| Downloads | In-flight queue · Recent activity · Per-backend telemetry (success/failure/in-flight) |
| Lidarr Backfill | Queue table · Stats · ETA · Capacity remaining · Bulk-reset by scope |
| Discovery | Lidarr discovery stats · Fill Library run history · Soulseek bulk download log |
| Charts | Last build · Match rate · Coverage stats |

**⚙ Settings**

| Page | Origin |
|---|---|
| Algorithm | (today's Algorithm tab — moves wholesale) |
| Download Routing | Downloads tab → routing config half |
| Lidarr Backfill Config | Discovery → Lidarr Backfill → settings drawer half |
| Connections | Connections tab → "what's configured" half (read-only snapshot of env-driven integrations) |
| Users | Users tab → CRUD + rename + delete + Last.fm connect/disconnect/sync (per-user) |
| Onboarding | (per-user, nested under Users → user) — explicit preference editor |

> **Versioned configs** (Algorithm, Download Routing, Lidarr Backfill) all share the same UX shell: collapsible groups · slider+number-input fields · Save & Apply / Discard / Reset / Export / Import / History / Diff. Today's Algorithm tab is the canonical pattern; the other two should match it visually.

---

## 3. Per-current-tab migration

### Dashboard tab → Monitor → Overview + System Health (split)

| Today | New home |
|---|---|
| 6 stat cards (events, users, tracks, playlists, events/h, ranker model) | Monitor → Overview |
| Recommendation Model Card (NDCG, i2s) | Monitor → Models |
| Event Types (24h) bar chart | Monitor → System Health |
| Users card table | Monitor → System Health (with row click → Settings → Users → user) |
| Top Tracks (24h) table | Monitor → System Health |
| **Library Scan Panel** (status, phase, progress, file counts, activity log) | Monitor → System Health → Library Scan |
| **"Scan Now" / "Sync IDs" buttons** | Actions → Library |
| Recent Events table | Monitor → System Health |

**Split note**: scan trigger lives in Actions, scan status lives in Monitor. Wire a "View status →" link on the Action page that jumps to the Monitor view, and a "Run scan" link on the Monitor view that jumps to Actions.

### Pipeline tab → Monitor → Pipeline + Models (split + extract)

| Today | New home |
|---|---|
| Flow diagram (step nodes + arrows + status colors) | Monitor → Pipeline |
| SSE indicator + Connect/Disconnect | Monitor → Pipeline (kept as live-updates indicator) |
| **"Run Pipeline" / "Reset Pipeline" buttons** | Actions → Pipeline & ML |
| Run header (trigger, status, config_version, run ID, duration) | Monitor → Pipeline |
| Selected step detail panel (metrics, error, started_at) | Monitor → Pipeline (modal or side-panel on click) |
| Sessionizer detail view (4 stat cards + 2 bar charts) | Monitor → Recs Debug → Pipeline Step Detail |
| Track Scoring detail (distributions, signal breakdown, top/bottom tracks) | Monitor → Recs Debug → Pipeline Step Detail |
| Taste Profiles explorer (radar, heatmap, mood/key bars) | Monitor → User Diagnostics → Taste Profile (extracted, per-user surface) |
| Ranker detail (training stats, feature importance, NDCG, impression funnel) | Monitor → Recs Debug → Pipeline Step Detail |
| Model Readiness cards (6 models) | Monitor → Models (separated into its own surface, was buried under Pipeline) |
| Recent Errors panel | Monitor → Pipeline → Errors |
| Run History table | Monitor → Pipeline |

**Split note**: deep link from Actions → Pipeline & ML → "Run Pipeline" auto-redirects to Monitor → Pipeline with SSE auto-connected (preserve current behaviour from `runPipelineFromTab()`).

### Content → Recommendations sub-tab → split

| Today | New home |
|---|---|
| User dropdown + seed track + limit + "Get Recs" button | Explore → Recommendations |
| Results table (track, artist, source, score, BPM, key, energy, mood, duration) | Explore → Recommendations |
| Source distribution bar chart | Explore → Recommendations (small inline panel) OR collapse into Debug |
| **"Debug Recs" button + full debug view** (candidates by source, reranker actions, rank comparison, feature inspector) | Monitor → Recs Debug → Debug Recs |
| **"Run Pipeline" / "Reset Pipeline" buttons in header** | Removed — duplicated from Pipeline tab; users go to Actions |

**Split note**: Explore → Recommendations is the clean "fetch and view" flow. A small "Debug this request" link on each result jumps to Monitor → Recs Debug → Debug Recs with the request_id pre-loaded.

### Content → Tracks sub-tab → Explore → Tracks

Wholesale move. Search box, sortable columns, pagination, "Generate Playlist" button (opens shared modal). Track ID column stays mono.

### Content → Playlists sub-tab → Explore → Playlists

Wholesale move. Card grid + detail view + delete. "Generate Playlist" button (opens shared modal). Playlist detail's track table reuses the Tracks-tab table component.

### Content → Radio sub-tab → Explore → Radio

Wholesale move. Start panel, active sessions, Now Playing card with feedback buttons, queue display.

### Content → Text Search sub-tab → Explore → Text Search

Wholesale move. Prompt input, example chips, results table, "Generate Playlist" button (pre-fills strategy=text).

### Content → Music Map sub-tab → Explore → Music Map

Wholesale move. UMAP canvas, color-scheme selector, click-to-select, "Build Path" → opens Generate Playlist modal pre-filled with strategy=path.

### Content → Charts sub-tab → split

| Today | New home |
|---|---|
| Filter bar (scope dropdown, type dropdown) + charts table + thumbnails + status badges | Explore → Charts |
| **"Build Charts" button** | Actions → Charts → Build Charts |
| Per-row "⬇ get" download button | Stays inline in Explore → Charts (deep-action call) — but routes through Actions → Downloads under the hood |
| Stats cards (last build, match rate, etc.) | Monitor → Charts |
| Auto-rebuild status banner | Monitor → Charts |

**Split note**: per-row download from Explore is the one place we let an Action live inline in Explore — moving it to a separate page would make chart browsing useless. Document this as an explicit exception.

### Content → Discovery sub-tab → multi-way split

Today's three sub-sub-tabs unwind into:

**Discovery → Lidarr** today →
- "Run Discovery" button → Actions → Discovery → Run Lidarr Discovery
- Lidarr stats + top artists chart → Monitor → Discovery

**Discovery → Fill Library** today →
- Status badge + "Run Fill Library" button → Actions → Discovery → Run Fill Library
- (Settings panel today appears to be Lidarr Backfill UI — see next row; Fill Library itself is env-var-only per CLAUDE.md, no GUI config)
- Run history → Monitor → Discovery

**Discovery → Lidarr Backfill** today →
- Settings drawer (4 grouped sections, ~20 tunables) → Settings → Lidarr Backfill Config (matches Algorithm UX shell)
- Top stats grid (missing total, queued, complete, failed, capacity, ETA) → Monitor → Lidarr Backfill
- Queue table + status filter + per-row Retry/Skip/Forget + bulk Clear actions → Actions → Discovery → Lidarr Backfill
- Run Now / Pause / Resume / Preview Match / History / Export / Import buttons → split:
  - Run Now / Pause / Resume → Actions → Discovery
  - History / Export / Import → Settings → Lidarr Backfill Config (versioned-config UX shell)
  - Preview Match modal → Settings → Lidarr Backfill Config (calibration tool — sits with the config it calibrates)

**Discovery → Soulseek Bulk** today →
- Max artists / tracks-per-artist inputs + Start/Cancel button → Actions → Discovery → Soulseek Bulk Download
- Progress display + download log → Monitor → Discovery

**Split note**: Lidarr Backfill is the most-fragmented current page — it ends up in 3 buckets. Settings has the policy, Actions has the queue management, Monitor has the live stats. Cross-link aggressively: Settings page should have "View queue →" and "View stats →" jumps; Monitor page should have "Edit config →" jump.

### Content → News sub-tab → Explore → News

Wholesale move (when implemented).

### Content → Audit sub-tab → Monitor → Recs Debug → Recommendation Audit

Wholesale move. Sessions list with filters, request detail with feature inspector, replay (rerank_only / full) with rank delta + Kendall's τ.

### Users tab → split

| Today | New home |
|---|---|
| Users list (table) | Settings → Users |
| User detail header (UID, display name, "Edit User" button) | Settings → Users → user detail |
| Rename modal | Settings → Users (modal) |
| **Taste Profile cards** (audio prefs, behaviour, mood prefs, key prefs) | Monitor → User Diagnostics → Taste Profile |
| **Last.fm Integration card** (status, sync, backfill, disconnect) | Settings → Users → user detail (per-user integration config) |
| Last.fm profile data (top artists tabbed, loved tracks, genres) | Monitor → User Diagnostics → Last.fm Profile (or roll into Taste Profile) |
| Top Tracks (interactions) table | Monitor → User Diagnostics → User History |
| Listening History table | Monitor → User Diagnostics → User History |
| Sessions table | Monitor → User Diagnostics → User History |
| **"Get Recs" button** | Deep link to Explore → Recommendations with user pre-filled |

**Split note**: Settings → Users → {user} shows admin info (UID, Last.fm integration, onboarding). It has prominent "View diagnostics →" jump to Monitor → User Diagnostics → {user}. The diagnostic page is per-user but lives in Monitor because the data is observability-shaped.

### Connections tab → split

Each of the 7 integration cards splits in two:

| Today (one card) | Settings → Connections | Monitor → Integrations |
|---|---|---|
| Header (icon, name, type, version) | ✓ | ✓ |
| Status badge | "Configured" / "Not configured" (env-driven) | "Healthy" / "Error" / "Probing" (live) |
| URL | ✓ (mono, read-only) | (not shown — same as Settings) |
| Description | ✓ | ✓ |
| Configured details (Last.fm scrobbling enabled, Plex token present, etc.) | ✓ | — |
| Live error message | — | ✓ |
| "Set env vars" hint when not configured | ✓ | — |
| Probe latency, last-checked-at | — | ✓ |

**Split note**: Settings page is read-only (env-driven) and rarely changes. Monitor page polls/probes regularly. Visually distinct: Settings has a static "configured snapshot" feel (think a server-info screen); Monitor has live-status feel (badges that change color).

### Algorithm tab → Settings → Algorithm

Wholesale move. Header buttons (Export / Import / History / Diff / Reset / Discard / Save & Apply), retrain warning banner, 7 collapsible groups, ~78 fields with slider + number input + RETRAIN/MODIFIED badges.

**Side-effect preserved**: "Save & Apply" still triggers pipeline reset via SSE. Today this navigates to the Pipeline tab — in the new IA, the user is in Settings, so on Save they should see a toast "Saved. Pipeline reset triggered →" with a jump to Monitor → Pipeline.

### Downloads tab → split

| Today | New home |
|---|---|
| Routing chains (3 chains: individual, bulk_per_track, bulk_album) with reorder/toggle/quality/timeout | Settings → Download Routing |
| Parallel search backends panel | Settings → Download Routing |
| Save & Apply / Discard / Reset / Export / Import / History | Settings → Download Routing (versioned-config shell) |
| **Live Queue panel** (in-flight + recent activity + per-row cancel) | Monitor → Downloads (top panel) |
| **Backend telemetry** (per-backend success/failure rates over window) | Monitor → Downloads (lower panel) |
| **Multi-Search box** (live test: query → heterogeneous results → "Download via X" buttons) | Actions → Downloads → Search & Download |

**Split note**: today's Downloads tab packs three different concerns onto one screen. After split, Settings → Download Routing has zero live data; Monitor → Downloads has zero config; Actions → Downloads is the operator tool for ad-hoc downloads.

---

## 4. Splits requiring careful design treatment

These are the cases where a single current page fragments into 2+ new pages and the user workflow needs explicit nav glue:

| Today | New homes (and the jump pattern needed) |
|---|---|
| Library Scan Panel | Actions (trigger) ↔ Monitor (status). Need: action page shows "View live progress →"; monitor page shows "Run new scan →" |
| Pipeline tab | Actions (run/reset) ↔ Monitor (flow + steps + history). Need: clicking Run in Actions should land on Monitor → Pipeline with SSE auto-connected (preserve today's behaviour) |
| Lidarr Backfill | Settings (policy) ↔ Actions (queue management + run now) ↔ Monitor (stats + ETA + capacity). Triple-jump cross-links between all three |
| Charts | Settings (none — env-driven) ↔ Actions (build now) ↔ Explore (browse) ↔ Monitor (stats). Per-row download stays inline in Explore as an exception |
| Connections | Settings (configured?) ↔ Monitor (healthy?). Visually similar layouts but different data |
| Users | Settings (admin: CRUD, Last.fm, onboarding) ↔ Monitor (diagnostic: taste, history, sessions). One "View diagnostics →" jump on the Settings user detail page |
| Recommendations | Explore (browse) ↔ Monitor (debug + audit). Per-row "Debug this →" link from Explore opens Monitor → Recs Debug with request_id pre-loaded |
| Downloads | Settings (routing) ↔ Actions (search & download) ↔ Monitor (queue + telemetry). Three-way split |

**Pattern**: every split needs at least a one-way deep link from the place you'd start to the related places you'd want to go. Symmetric where it makes sense.

---

## 5. Components to extract / unify

Things that exist multiple times today and should become shared components in the new GUI:

- **Track table** — used in Tracks, Playlists detail, Recommendations, Charts, Audit. Same columns, same sort logic. Make one component, parameterise the visible columns.
- **Generate Playlist modal** — opened from Tracks, Playlists, Text Search, Music Map. Shared today; keep shared.
- **Versioned-config shell** — Algorithm, Download Routing, Lidarr Backfill all want: Save & Apply / Discard / Reset / Export / Import / History / Diff buttons in header + collapsible groups + slider+number-input fields + per-field default indicator + MODIFIED/RETRAIN badges. Build one shell, three configs plug into it.
- **Integration card** — used in Settings → Connections (snapshot) and Monitor → Integrations (live). One component, two data sources.
- **User dropdown** — populated from `cachedUsers`, used in Recommendations, Radio, Audit, News. Already centralised; keep.
- **Per-step pipeline detail panel** — sessionizer, scoring, taste, ranker each have their own bespoke layout today. Consider a uniform "step detail" frame with step-specific content slots.

---

## 6. Design direction

Two cross-cutting constraints that shape the visual rework, separate from the IA reshuffle.

### Responsive layout

The dashboard must work on smartphones with tall portrait aspect ratios. PWA features (service worker, app manifest, offline mode, install banner, push notifications) are explicitly out of scope — this is "the website works on a phone", not "the website can be installed as an app".

**Implications for navigation**:
- The 4-bucket top nav needs a small-screen treatment. With four equal-weight buckets, a bottom tab bar (Explore / Actions / Monitor / Settings) is the strongest fit; alternatives are a hamburger drawer or a horizontal pill bar that scrolls. Designer's call.
- Sub-page nav (e.g. Explore's 9 sub-pages, Monitor's 11 surfaces) needs its own small-screen pattern — horizontal-scroll chips, accordion, or a secondary bottom-sheet menu.

**Components that need explicit small-screen design**:
- **Track tables** (Tracks, Playlists, Recommendations, Charts, Audit) — wide on desktop, untenable on mobile. Either horizontal-scroll the table or collapse to a card layout below a breakpoint.
- **Music Map** (canvas-based scatter) — pinch-zoom + tap-to-select. May need a dedicated mobile treatment.
- **Pipeline flow diagram** (horizontal node graph with arrows) — wraps poorly. Vertical stack on small screens.
- **Versioned-config grids** (Algorithm / Download Routing / Lidarr Backfill — slider + number-input pairs in a 2-col grid) — collapse to single column below breakpoint.
- **Side-by-side diff modals** (version compare) — stack vertically on mobile.
- **Multi-column dashboards** (Monitor → Overview, Monitor → Pipeline) — reflow into single-column scroll on small screens.

**Out of scope, explicitly**:
- Install prompts, push notifications, offline mode, app manifest
- Mobile-native gestures beyond standard scroll/tap/pinch (no swipe-to-dismiss, no pull-to-refresh)
- Dedicated mobile-only flows or surfaces — same content, responsive container

### Visual restraint — less colour

The current dashboard uses colour aggressively as a signal carrier:
- Event-type badges (7 distinct colours across `play_end`, `like`, `skip`, `dislike`, `pause`, `volume_up`, etc.)
- Status badges across pipeline steps, downloads, charts, integrations (success / warning / danger / info / primary)
- Source badges in recommendations (content / cf / artist_recall / popular / session_skipgram / sasrec / lastfm_similar — each its own colour)
- Reranker action badges (freshness_boost / skip_suppression / anti_repetition / exploration / artist_diversity — each colour-coded)
- Score bar gradients (red → yellow → green)
- Stat-card numbers colour-shifted by range
- Chart-row status (in library / via lidarr / downloading / pending / failed — each its own colour)

The new design should retreat from this. Suggested direction (designer to refine):
- **Reserve colour for state that genuinely changes meaning** — destructive actions (red), errors (red), success confirmations (green), and one accent colour for primary actions / brand. That's it for chromatic colour.
- **Demote categorical badges** — sources, event types, reranker actions, etc. become typographic distinctions (uppercase mono, weight, subtle outline, icon) rather than coloured pills. Where colour is genuinely needed (e.g. seven candidate sources in a stacked bar), use a single neutral hue family at varying saturation, not a rainbow.
- **Status badges** use a small fixed semantic palette (4 states max: ok / warning / error / neutral), not the current 7+.
- **Progress and score bars** stay monochromatic by default; reserve colour-shift gradients for cases where colour itself is the data (heatmaps, music-map colouring, taste-profile radar overlays).

Net effect: a design where colour means something everywhere it appears, vs. today where colour decorates everything.

## 7. Open design questions

Hand-off questions for design — flag any that need product input before designing.

1. **Landing page on first load.** Today it's Dashboard (system stats). Candidates: Monitor → Overview (status quo), Explore → Recommendations (product surface), Settings → Connections (set-up nudge for new installs). Probably "Monitor → Overview if data exists, Settings → Connections if no integrations configured."

2. **Global pipeline-running indicator.** SSE pipeline runs are useful to surface across all tabs (a small "Pipeline running · 4/10 steps · click to view" pill). Today it's only visible on the Pipeline tab. Worth adding to top nav?

3. **Persistent live indicators in nav.** SSE connection state, scan in progress, downloads in flight, Lidarr backfill running — are these globally visible badges, or do you discover them by visiting the relevant page?

4. **Search/command palette.** With four buckets and ~30 sub-pages, a global ⌘K search could short-circuit navigation. Worth designing in from the start, or defer?

5. **Mobile feature parity.** Per design constraint 6, the dashboard must work on smartphones. Some surfaces are awkward on small screens (Music Map canvas, full per-step pipeline detail panels, version-diff side-by-side modals, the multi-search live-results panel). Are these (a) full-parity on mobile with bespoke responsive treatments, or (b) desktop-only with a polite "best viewed on desktop" notice on small screens? Most other surfaces are easy to make responsive.

6. **Light/dark mode.** Today's dashboard appears to be dark-only. Confirm whether to support both or stay single-theme.

7. **User detail page topology.** Settings → Users → {user} shows admin (rename, delete, Last.fm, onboarding). Monitor → User Diagnostics → {user} shows everything else. The two pages share a user identity. Should there be a header-level toggle / shared sidebar / tab bar within "user context"? Or are they entirely separate routes?

8. **Onboarding editor placement.** Onboarding preferences are submitted by the iOS app on first launch. The GUI editor is probably for admin override / debugging. Confirm whether it lives:
   - At Settings → Users → {user} → Onboarding (always available), OR
   - At Settings → Users → {user} → … → an "Edit onboarding" rare-action button

9. **Versioned-config UX consistency.** Algorithm, Download Routing, Lidarr Backfill should all use the same shell. Is "groups + sliders + save/discard/reset/history/import/export" really the right pattern for all three, or does Download Routing want something more like a list-builder for chain entries (drag-reorder feels closer)?

10. **Actions page UX.** Each Action is a trigger button. Should the Actions section be:
    - One page per action (lots of nav clicks), OR
    - One page per group with multiple actions stacked, OR
    - A single Actions hub with all triggers as cards?
    
    Lean towards (b) — grouped pages — to mirror the Settings UX shape.

11. **Inline action exception (Charts download).** The per-row "⬇ get" button in Explore → Charts is the one place an Action lives inside Explore. Are there other inline-action exceptions worth allowing (e.g. Music Map "Build Path" — actually that creates content, so it's fine in Explore)? Lock down the rule: inline actions in Explore are OK only when (a) they create content the user is browsing, or (b) they fetch external content already represented on screen.

12. **What to drop entirely.** A few tabs/buttons exist today that may not be worth porting:
    - "Run Pipeline" / "Reset Pipeline" duplicated on Recommendations sub-tab — drop, users find it in Actions
    - Empty/placeholder UI for News (planned but not implemented) — design a "coming soon" stub or leave out of new IA?
    - Stats cards on Recommendations sub-tab (source distribution) — might be redundant with Monitor → Recs Debug

---

## 8. Migration phasing (rough)

Not for design, but worth recording:

1. **Phase 1** — restructure HTML/JS into 4 top-level routes; move existing components without redesign. Verifies the IA works.
2. **Phase 2** — apply new design from hand-off; component-by-component visual rework.
3. **Phase 3** — add deep-link glue between split pages (Settings ↔ Actions ↔ Monitor).
4. **Phase 4** — extract shared components (track table, versioned-config shell, integration card).
5. **Phase 5** — global features (search, persistent live indicators) if approved.

---

## Reference

- **Inventory source**: `app/static/index.html` (55 LOC), `app/static/js/app.js` (5,210 LOC), `app/static/css/style.css` (1,816 LOC) — captured 2026-04-29
- **API surface**: see `docs/API.md` and the API endpoints section in `CLAUDE.md` — every Action/Monitor view maps to existing endpoints; no new backend work expected for the rework

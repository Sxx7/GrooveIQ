# Session 09 — Explore: track table component + Recommendations + Tracks

You are continuing the GrooveIQ dashboard rebuild. This is **session 09 of 12**.

First Explore-bucket session. Builds the **shared track-table component** (used in Recommendations, Tracks, Playlists, Charts, Audit) plus the two pages that consume it most directly.

## Read first

1. `docs/rebuild/README.md`
2. All prior hand-offs (01–02 minimum; 06 is helpful for the debug deep-link pattern).
3. `docs/rebuild/components.md` → **Track table**.
4. `docs/rebuild/api-map.md` → "Recommendations" and "Tracks" rows.
5. `gui-rework-plan.md` § "Content → Recommendations sub-tab → split", § "Content → Tracks sub-tab → Explore → Tracks".
6. `design_handoff_grooveiq_dashboard/page-recs.jsx` — visual reference for Recommendations.
7. The current Recommendations + Tracks views in `app/static/js/app.js`.

## Goal

Two Explore pages working:
- `#/explore/recommendations` — fetch + render recommendations for a user. Per-row "debug→" link jumps to Monitor → Recs Debug with `request_id`.
- `#/explore/tracks` — search, sortable + paginated track table.

Plus the reusable `GIQ.components.trackTable` component.

## Out of scope

- Playlists / Music Map / Text Search / Radio / Charts / Artists / News (sessions 10, 11).

## Tasks (ordered)

### A. `GIQ.components.trackTable`

Spec — see `components.md → Track table`. Implementation guidelines:

1. **Signature**:
   ```js
   GIQ.components.trackTable({
       columns,        // ['rank','title','artist','bpm','key','energy','dance','valence','mood','duration','version','id']
       rows,           // array of track objects from API
       sort,           // { field, dir }
       onSort,         // (field) => void
       pagination,     // { offset, limit, total, onPage } or null for fixed display
       rowAction,      // (row) => DOMElement, e.g. a debug→ link
   }) → DOMElement
   ```
2. Each column has consistent rendering rules (BPM mono, energy mini-bar lavender on `--line-faint`, mood = top-tag string, duration MM:SS, track ID mono small).
3. Below 700px, collapse to a card layout: title + artist on top line, sub-line with BPM · key · mood · duration. The card layout is also a `trackTable` mode — same data, different render.
4. Source chips (when shown — Recommendations passes a `source` column) are typographic: mono 9px uppercase, faint outline, no fill colour.
5. Score bars (when shown) are monochrome `--ink` on `--line-faint`.
6. Empty state: "No tracks." in `--ink-3`, centred.

### B. Explore → Recommendations

`GIQ.pages.explore.recommendations`:

1. **Page header** — eyebrow EXPLORE, title "Recommendations", right-side filter row:
   - User dropdown (loads from cached users; URL `?user={id}` updates).
   - Seed track input (text — paste a track_id).
   - Limit input (number, default 25, range 1–100).
   - "Get Recs" primary button.
2. **Cross-link banner** (the design hand-off rail style) — appears after first fetch:
   - `request_id 7f2a-3c8e` (mono).
   - Right side: "Debug this request →" jumping to `#/monitor/recs-debug?debug={request_id}`.
3. **Filter pills** — optional context controls: device_type / output_type / context_type / location_label / hour / day. Empty default; if user clicks "Add context" expand a row of dropdowns. Each context filter passed as query param to `/v1/recommend/{user_id}`.
4. **Results** — `GET /v1/recommend/{user_id}?limit=&seed_track_id=&...`. Render via `trackTable` with columns `['rank','title','artist','source','score','bpm','key','energy','mood','duration']`. Per-row action: small `debug→` link in `--accent` jumping to `#/monitor/recs-debug?debug={request_id}` with that track scrolled into view.
5. **Empty state** — "No recommendations available. Try a different user or remove filters."

### C. Explore → Tracks

`GIQ.pages.explore.tracks`:

1. **Page header** — eyebrow EXPLORE, title "Tracks ({total})", right-side controls:
   - Search box (220px, placeholder "Search title, artist, ID…", clear (×) button when search active, Enter triggers).
   - Search button (secondary).
   - "Generate Playlist" button (primary). Click → opens Generate Playlist modal (built in session 10; for now stub with `GIQ.toast('Generate Playlist modal — session 10')`).
2. **Track table** — sortable columns BPM / Title / Artist / Genre / Key / Energy / Dance / Valence / Mood / Duration / Version / Track ID. Sort sticky between navigations (state in `GIQ.state.trackList`). Paginated 50 per page.
3. **State persistence** — when user navigates away and back, sort + offset + search persist (lifted from current `trackState`).

### D. Wire jump from Monitor → User Diagnostics → "Get Recs"

If session 07 is done, its "Get Recs" button targets `#/explore/recommendations?user={id}`. Verify the URL parses correctly and pre-fills the user dropdown.

### E. Wire jump from Activity Pill / Monitor → Pipeline back

Not really a deep-link target, but verify the topbar SSE pill still pulses correctly when SSE is active on this page (no leaked subscriptions from session 06).

## Verification

1. Load `#/explore/recommendations`. Pick a user. Click "Get Recs". Results render. Click `debug→` on a row → jumps to `#/monitor/recs-debug?debug={id}` and the request is loaded in mode 2.
2. Load `#/explore/tracks`. Search "lud" → table filters. Sort by Energy desc → bars at top, smaller bars at bottom. Paginate to page 2 → offset advances.
3. Resize the browser to < 700px → table collapses to card layout cleanly. Sort + pagination still work.
4. Navigate away and back → state persists.
5. Empty queries / users with no recs → empty state renders, doesn't crash.

## Hand-off

Write `handoffs/09-explore-recs-tracks.md`. Critical:
- Final shape of `GIQ.components.trackTable` API — sessions 10 + 11 will reuse heavily.
- The card-layout breakpoint behaviour.
- Any quirks of the recommendations response (e.g. how `source` is encoded).

Commit: `rebuild: session 09 — explore: track table + recommendations + tracks`.

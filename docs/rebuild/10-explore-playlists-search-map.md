# Session 10 — Explore: Playlists + Generate Playlist modal + Text Search + Music Map

You are continuing the GrooveIQ dashboard rebuild. This is **session 10 of 12**.

Three Explore pages plus the shared Generate Playlist modal that's used from many surfaces.

## Read first

1. `docs/rebuild/README.md`
2. All prior hand-offs (especially 09 for `trackTable` API).
3. `docs/rebuild/components.md` → **Generate Playlist modal**, **Track table**.
4. `docs/rebuild/api-map.md` → "Playlists", "Text Search", "Music Map" rows.
5. `gui-rework-plan.md` § "Content → Playlists / Music Map / Text Search".
6. The current implementations in `app/static/js/app.js` — search `// Playlists View`, `// Music Map`, `// Text Search`, `// Generate Playlist Modal`. Port behaviour faithfully.

## Goal

Three Explore pages working + reusable modal:
- `#/explore/playlists` — browse + view + delete playlists. "Generate" button → modal.
- `#/explore/text-search` — CLAP prompt → results table. Quick-prompt examples. "Generate Playlist" → modal pre-filled with strategy=text.
- `#/explore/music-map` — UMAP scatter. Click two tracks → "Build Path" → modal pre-filled with strategy=path.
- `GIQ.components.generatePlaylistModal` — opens from anywhere, supports prefill.

## Out of scope

- Radio / Charts / Artists / News (session 11).
- Mobile responsive Music Map ("Best on desktop" notice — see notes below for v1 scope).

## Tasks (ordered)

### A. `GIQ.components.generatePlaylistModal`

Spec — see `components.md → Generate Playlist modal`. Implementation:

1. **Signature**:
   ```js
   GIQ.components.generatePlaylistModal({ prefill }) → opens modal
   ```
   `prefill` is an optional `{ strategy?, seed_track_id?, target_track_id?, prompt?, mood?, curve?, name? }`.
2. **Fields** (show/hide based on selected strategy):
   - Name (text, default "My Playlist").
   - Strategy dropdown: flow / mood / energy_curve / key_compatible / path / text.
   - Conditional fields:
     - flow / path / key_compatible → seed_track_id input.
     - path → also target_track_id input.
     - text → prompt input + helper text "Requires CLAP enabled and backfilled".
     - mood → mood dropdown.
     - energy_curve → curve dropdown.
   - Max Tracks (number, default 25, range 5–100).
3. **Buttons**: Cancel · Generate (primary, disabled until required fields are valid).
4. **Submit** → `POST /v1/playlists` with payload. On success: toast "Playlist generated · X tracks" + jump to `#/explore/playlists/{id}` after the toast click.
5. **Close on Esc / outside click / Cancel.**
6. **Focus trap** — focus the first input on open; restore focus on close.

### B. Explore → Playlists

`GIQ.pages.explore.playlists`:

1. **Page header** — eyebrow EXPLORE, title "Playlists ({count})", right-side: "Generate Playlist" primary button.
2. **Card grid** — `GET /v1/playlists?limit=50`. Each card: name (Inter Tight 14/600), meta row (strategy chip · track_count · total duration · created_at time-ago). Click → detail view.
3. **Playlist detail** (`#/explore/playlists/{id}`):
   - Page header with back link → `#/explore/playlists`. Title = playlist name. Subtitle: strategy chip + track_count + duration.
   - Right-side button: "Delete" (destructive, confirmation).
   - **Track table** — list of tracks in playlist order. Use `trackTable` from session 09.

### C. Explore → Text Search

`GIQ.pages.explore.textSearch`:

1. **Page header** — eyebrow EXPLORE, title "Text Search".
2. **CLAP availability gate** — `GET /v1/tracks/clap/stats`. If `enabled === false` or `with_clap === 0`, render an empty state: "CLAP is disabled or no tracks have CLAP embeddings yet. Enable in `.env` and run a CLAP backfill from Actions → Pipeline & ML." Hide search controls.
3. **Search panel**:
   - Prompt input (Inter, large, 16px).
   - Limit input (number, default 50, range 5–200).
   - Search button (primary).
   - **Example prompts** as clickable chips: "upbeat summer night driving" · "chill lofi study session" · "aggressive workout metal" · "rainy coffee shop jazz". Click fills prompt.
4. **Results** — `GET /v1/tracks/text-search?q=&limit=`. Use `trackTable` with default columns. Show response model_version + request_id in a small mono sub-line.
5. **"Generate Playlist" button** (top-right of results) → opens modal pre-filled with `strategy='text'` and `prompt={current query}`.

### D. Explore → Music Map

`GIQ.pages.explore.musicMap`:

1. **Page header** — eyebrow EXPLORE, title "Music Map", right-side controls:
   - Color scheme dropdown: by_energy / by_danceability / by_valence / by_acousticness / by_key / by_mood. Default `by_energy`. Change → recolor without re-fetching.
   - "Reload" button → refetch.
2. **Canvas** — full-width canvas (or SVG, but canvas is faster for thousands of dots) rendering UMAP coordinates from `GET /v1/tracks/map`.
3. **Interactivity**:
   - Hover → tooltip with track + artist + selected metric.
   - Click → select track A.
   - Shift+click (or second click) → select track B.
   - When both A and B selected: "Build Path" button (primary) appears + "Clear" button appears.
   - "Clear" → resets selection.
4. **Build Path** → opens Generate Playlist modal pre-filled with `strategy='path'`, `seed_track_id=A`, `target_track_id=B`. After success → close modal, navigate to playlist detail.
5. **Color scheme** — this is one of the two places where colour-as-data is allowed (the other is heatmaps). Use a perceptually-uniform colour ramp like viridis or `interpolateLab` between `--paper-2` and `--accent` for monochrome metrics; only `by_mood` and `by_key` should use multiple hues (and even there, prefer a constrained set).
6. **Empty state** — if the API returns an empty list (less than `MIN_TRACKS=50` analysed tracks), show "Music Map needs at least 50 analysed tracks. Run a library scan first." with a jump to `#/actions/library`.
7. **Mobile** — at < 700px, render a notice "Music Map is best viewed on desktop. Pinch-zoom and tap-to-select are supported but limited." Then still render the canvas. (Per design hand-off open question 3 — implementer chose "best on desktop" notice for v1 scope.)

### E. Wire jump from Recommendations and Tracks "Generate" buttons (session 09)

Both buttons currently stub-toast. Replace with `GIQ.components.generatePlaylistModal({ prefill })`:
- Tracks page: clicking on a track row + "Generate Playlist" → opens modal pre-filled with `seed_track_id={track_id}, strategy='flow'`.
- Recommendations page: optional — add a "Generate from these" button that takes the top track as seed.

## Verification

1. Generate Playlist modal opens from each of: Tracks page, Playlists page, Text Search results, Music Map "Build Path", User Diagnostics (if applicable).
2. Build a flow playlist: pick a track → modal pre-filled → Generate → playlist appears in list → opens detail with track table.
3. Run a CLAP search "melancholic piano at 2am" → top results are appropriately mellow (sanity check).
4. Music Map: click two distant tracks → Build Path → playlist appears containing tracks that smoothly interpolate between them.
5. Music Map at < 700px → notice shows + canvas is still rendered.
6. Delete a playlist → confirmation → playlist removed from list.

## Hand-off

Write `handoffs/10-explore-playlists-search-map.md`. Note:
- The colour-ramp choice in Music Map.
- Any quirks of `/v1/tracks/map` payload shape (especially `selected` flag if any).
- The Generate Playlist modal validation rules.

Commit: `rebuild: session 10 — explore: playlists + modal + text search + music map`.

# Session 11 — Explore: Radio + Charts + Artists + News

You are continuing the GrooveIQ dashboard rebuild. This is **session 11 of 12**.

The last Explore-bucket session. Four pages. Radio is the most stateful surface in the entire dashboard.

## Read first

1. `docs/rebuild/README.md`
2. All prior hand-offs (especially 09 for `trackTable`, 10 for modal).
3. `docs/rebuild/components.md`
4. `docs/rebuild/api-map.md` → "Radio", "Charts", "Artists", "News" rows.
5. `gui-rework-plan.md` § "Content → Radio sub-tab → Explore → Radio", § "Content → Charts sub-tab → split", § "Users tab → split" (Artists is the new page promoted from per-user Last.fm artist data).
6. The current implementations in `app/static/js/app.js` — search `// Radio View`, `// Charts View`, `// News View`. Artists is partly new — port from the per-user artist UI in `renderUserDetail`.

## Goal

Four Explore pages working:
- `#/explore/radio` — start, listen, give feedback (skip / like / dislike), fetch next batch, stop. Multi-session support.
- `#/explore/charts` — browse Last.fm charts with filter + per-row download. Per-row download is the **inline-action exception** — keep it.
- `#/explore/artists` — new first-class page. Recommended artists ranked from listening history + Last.fm + per-user top tracks.
- `#/explore/news` — Reddit-sourced music feed if implemented; "coming soon" stub if not.

## Out of scope

- Radio drift visualisation (taste vector plot) — nice-to-have, defer.
- Cross-cutting deep-link audit (session 12).

## Tasks (ordered)

### A. Explore → Radio

`GIQ.pages.explore.radio`:

1. **Page header** — eyebrow EXPLORE, title "Radio".
2. **Start panel** (when no active session for current user):
   - User dropdown.
   - Seed type radio: Track / Artist / Playlist.
   - Seed value input (text or dropdown depending on seed type).
   - Optional context filters (device / output / context_type / location_label / hour / day) — collapsible "advanced" section.
   - "Start Radio" primary button → `POST /v1/radio/start`.
3. **Active sessions list** — `GET /v1/radio?user_id={current_user}`. Each session card: session ID (mono), seed info, started timestamp, "Resume" button (loads as Now Playing), "Stop" (destructive) → `DELETE /v1/radio/{id}`.
4. **Now Playing panel** (when session is loaded):
   - Current track info (artist + title + album).
   - Position / duration timeline.
   - **Feedback buttons** in a row, large:
     - ♥ Like — `--accent` accent button.
     - ▶ Skip — neutral button.
     - ✕ Dislike — `--wine` button.
     - All three POST `/v1/events` with `event_type=like|skip|dislike`, `context_type=radio`, `context_id={session_id}`.
   - "Next 10" / "Next 25" buttons — `GET /v1/radio/{id}/next?count=` to fetch more tracks.
   - "Stop" (destructive) — `DELETE /v1/radio/{id}`.
5. **Track queue panel** — upcoming tracks. Same `trackTable` pattern but with per-row feedback buttons inline (Like / Skip / Dislike).
6. **Drift indicator** (small, decorative) — text "drift {N} tracks since seed" using session metadata. Optional.
7. **Auto-advance** — when current track ends (heuristic: position + duration check), advance to next in queue and fire a `play_end` event. Or just allow manual advance — both fine for v1.
8. **Polling** — refresh queue when fewer than 3 tracks remain ahead.

### B. Explore → Charts

`GIQ.pages.explore.charts`:

1. **Page header** — eyebrow EXPLORE, title "Charts", auto-rebuild status badge ("✅ Auto-rebuild every Xh — next in Y" / "⚠️ Auto-rebuild OFF — set CHARTS_ENABLED=true").
2. **Filter bar**:
   - Scope dropdown — populated from `GET /v1/charts` (Global / Genre: rock / Genre: electronic / Country: germany / etc.).
   - Type dropdown — Top Tracks / Top Artists.
3. **Charts table** — `GET /v1/charts/{type}?scope=&limit=&offset=`. Columns:
   - For Top Tracks: # / thumbnail / title / artist / plays / listeners / status.
   - For Top Artists: # / thumbnail / artist / plays / listeners / library tracks / status.
4. **Status column** (per-row chip):
   - "in library" (`--accent` outline, no fill).
   - "via lidarr" (purple-tinted outline).
   - "⬇ lidarr" (info, in queue).
   - "✗ lidarr" (`--wine`, failed).
   - "not in library" (neutral, faded `--ink-3`).
   - "⬇ get" — small inline button (the inline-action exception) → `chartsDownloadTrack(...)` → `POST /v1/charts/download`. After click: row updates to show download status.
5. **Thumbnail handling** — primary URL from cover_art_cache (resolves through spotdl/Spotizerr fallback chain server-side). Fallback to Last.fm image URL. Fallback to placeholder. Use `<img onerror>` chain.
6. **Pagination** — prev / next.

### C. Explore → Artists

This is a **new** first-class page promoted from the per-user Last.fm card.

`GIQ.pages.explore.artists`:

1. **Page header** — eyebrow EXPLORE, title "Artists", right side: user dropdown.
2. **Source segmented toggle** — "Listening history" / "Last.fm similar" / "Last.fm top". Default "Listening history".
3. **Recommended artists list** — `GET /v1/recommend/{user_id}/artists`. For the selected source, render artist cards in a grid:
   - Cover image (from `cover_art_cache`).
   - Name (Inter Tight 14/600).
   - Aggregate audio stats line (mono): "BPM 72 · energy 0.4 · valence 0.6".
   - Library presence indicator: "✓ in library (12 tracks)" or "+ via Lidarr" or "? unknown".
   - Play / like counts (when source = "Listening history").
   - "Top tracks" sub-list (up to 5) ranked by satisfaction score.
   - Click card → opens artist detail modal (or navigate to a `#/explore/artists/{name}` page if you go that route).
4. **Artist detail** (modal or page):
   - `GET /v1/artists/{name}/meta` — Last.fm bio + tags + similar artists + top tracks + images.
   - Local library cross-reference: which of their tracks are analysed, with mini track-table.
   - Buttons: "Play radio from this artist" → `#/explore/radio?seed_type=artist&seed_value={name}`. "Add to Lidarr" (if not in library) → POST to discovery / add-artist endpoint (verify it exists; if not, defer).

### D. Explore → News

`GIQ.pages.explore.news`:

1. **Feature gate** — `GET /v1/news/{user_id}` (or 404 / 503 if not implemented). If not implemented, render a "Coming soon" stub: "Personalized music news from Reddit. Set `NEWS_ENABLED=true` in `.env` and configure subreddits." Don't break the page.
2. If implemented, render per the original spec in `CLAUDE.md`:
   - **Page header** — eyebrow EXPLORE, title "News", right side: user dropdown + tag filter dropdown + "Refresh Feed" button.
   - **Cache age indicator** — "cache age 12 min" (mono, `--ink-3`); flag `cache_stale: true` with `--wine` colour.
   - **News items list** — each item: source icon + title + summary + tag chip + relevance score + relevance reasons chips ("Because you listen to Kendrick Lamar"). External link button.

### E. Verify cross-bucket deep-links

After this session, every Explore sub-page exists. Verify:
- Recommendations `debug→` → Monitor → Recs Debug ✓
- Tracks "Generate Playlist" → modal → Playlists detail ✓
- Music Map "Build Path" → modal → Playlists detail ✓
- Charts "⬇ get" → triggers download → Monitor → Downloads (eventually visible there)
- Artists "Play radio" → Explore → Radio ✓
- Radio feedback fires events that show up in Monitor → Overview "Recent events" ✓

## Verification

1. Start a radio session → first batch of tracks loads. Click Like on a track → feedback POSTed; verify in network tab. Click Next 10 → more tracks fetched. Drift indicator updates.
2. Browse Charts → filter by Genre: rock → table updates. Click `⬇ get` on an unmatched track → status changes to "downloading".
3. Browse Artists → switch source toggle → list updates. Click an artist → detail loads (modal or page) with bio + tracks. Click "Play radio" → jumps to Radio with seed pre-filled.
4. Load News → either real feed or graceful stub.

## Hand-off

Write `handoffs/11-explore-radio-charts.md`. Note:
- Whether Artist detail is a modal or its own page (and why).
- News implementation status (feature-gated stub or real).
- Any radio-feedback event-shape quirks.

Commit: `rebuild: session 11 — explore: radio + charts + artists + news`.

**The Explore bucket is now complete.** Session 12 wires cross-bucket deep links, ships mobile responsive, and cuts over `/dashboard`.

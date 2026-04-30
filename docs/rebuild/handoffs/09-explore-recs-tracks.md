# Hand-off: Session 09 — Explore: track table + Recommendations + Tracks

## Status
- [x] Session goal achieved
- [x] Visual verification done (mocked-API harness in `preview_eval` — same approach as sessions 02–08; static-server preview can't reach the real backend)
- [x] No regressions in old `/dashboard` (zero modifications to `app/static/index.html`, `js/app.js`, `css/style.css`)
- [x] Overview (session 02) regression-checked — still renders 6 stat tiles, no console errors
- [x] Other Explore stubs (radio · playlists · text-search · music-map · charts · artists · news) still print their TBD placeholder
- [x] Committed on `gui-rebuild` branch with message `rebuild: session 09 — explore: track table + recommendations + tracks`

## What landed

The Explore bucket now has its first two real pages and the **shared `GIQ.components.trackTable`** that sessions 10 and 11 will reuse heavily. The track table renders 16 column ids in a single config-driven shape — sortable headers (caller-controlled), monochrome score bars, lavender energy bars on `--line-faint`, source chips that are typographic (mono · 9 px · uppercase · faint outline · no fill), MM:SS durations, and a trailing "row action" slot for things like the recommendations `debug→` jump. **Below 700 px the same data renders as a card list** (title + artist on top, sub-line `BPM · key · E energy · mood · duration`); the swap is pure CSS, no JS branching.

`#/explore/recommendations` is now a full page: eyebrow + title + filter row (user dropdown, seed-track input, limit input, "Add context" toggle, "Get Recs" primary). Clicking "Add context" expands a 6-field row (device · output · context · location · hour · day) whose values are passed as query params to `/v1/recommend/{user_id}`. After a successful fetch a hand-off-style "request_id 7f2a-3c8e — Debug this request →" cross-link banner appears, jumping into Monitor → Recs Debug with the persisted audit. Each result row gets its own lavender-accent `debug→` pill that deep-links to `#/monitor/recs-debug?debug={request_id}&track={track_id}` so the debugger lands in detail mode with the right track scrolled into view (track-scrolling itself is a session-12 polish item; today the URL just carries the param).

`#/explore/tracks` is the library browser: page title carries the live total (`Tracks (48,201)`), right-rail has a 220 px search box with an inline × clear button, a "Search" secondary, and a "Generate Playlist" primary that toasts a session-10 stub for now. The table is sortable on BPM / Energy / Dance / Valence / Duration / Version; sort + offset + search persist in `GIQ.state.trackList` so navigating away and back keeps the user in place. Pagination is 50 per page via the trackTable's built-in prev/next.

The **deep link from Monitor → User Diagnostics** ("Get Recs →") was already wired in session 07 to `#/explore/recommendations?user={id}`; this session's page parses that param on render and pre-selects the dropdown.

## File inventory after this session

Substantially modified:
- [app/static/js/v2/components.js](../../../app/static/js/v2/components.js) — appended `GIQ.components.trackTable` + private helpers `_ttGridTemplate`, `_ttHeader`, `_ttCell`, `_ttCardLayout`, `_ttTopMood`, `_ttFmtDur`, `_ttBasename` (~310 new lines, 1996 → 2310). Bumped `hasAction` column width from 48 px → 66 px so `debug→` (6 chars + arrow) renders cleanly.
- [app/static/js/v2/explore.js](../../../app/static/js/v2/explore.js) — went from a 21-line stub to ~430 lines: real `recommendations` and `tracks` renderers, plus a STUBS array that keeps the other 7 sub-pages on their TBD placeholders.
- [app/static/css/pages.css](../../../app/static/css/pages.css) — appended ~330 lines covering: `.track-table-wrap / -empty / -head / -row`, `.tt-h / -sort-btn`, per-column cell modifiers (`.tt-c-title`, `.tt-c-artist`, `.tt-c-source`, `.tt-c-score`, `.tt-c-energy`, etc.), `.tt-rank / -title / -title-sub / -artist / -truncate / -id / -version`, the score & energy mini-bars (`.tt-score-track / -fill`, `.tt-energy-track / -fill / -num`), the card-mode block (`.tt-card / -inner / -top / -title / -name / -artist / -sub / -action`), the recommendations chrome (`.reco-body / -header-controls / -select / -input / -context-row / -ctx-field / -banner / -results / -meta / -empty / -error / -table-panel / -debug-link`), the tracks chrome (`.tracks-body / -header-controls / -search-wrap / -search / -search-clear / -panel`), and the responsive `@media (max-width: 700px)` block that swaps table → card layout.

Doc:
- [docs/rebuild/handoffs/09-explore-recs-tracks.md](09-explore-recs-tracks.md) — this file.

No files added; `dashboard-v2.html` script-tag list unchanged.

## State of the dashboard at end of session

Working at `/static/dashboard-v2.html`:

`#/explore/recommendations`:
- Page header eyebrow `EXPLORE`, title "Recommendations". Right rail: user `<select>`, seed-track `<input>` (180 px), limit `<input>` (60 px, default 25), "Add context" ghost toggle, "Get Recs" primary.
- Empty state: `Pick a user, optionally a seed track, then click Get Recs.` (panel-tinted card, centred).
- Cross-link banner appears after a successful fetch: dashed-outline pill with mono `request_id 7f2a3c8e-1234-5678…` (truncated to 18 chars + ellipsis, full id in the title attr) and a lavender `Debug this request →` jumpLink to `#/monitor/recs-debug?debug={id}`.
- Context drawer (collapsed by default): expands to a 6-field row (Device · Output · Context · Location · Hour · Day). Each field is a `<select>` with the spec's enum values; selected values flow into the `/v1/recommend` query string. State persists in `GIQ.state.recoState.ctx`.
- Results table: 10-column trackTable (`rank`, `title`, `artist`, `source`, `score`, `bpm`, `key`, `energy`, `mood`, `duration`) + per-row `debug→` action cell that targets `#/monitor/recs-debug?debug={request_id}&track={track_id}`. Empty data → "No recommendations available. Try a different user or remove filters."
- URL deep-link `?user={id}` pre-selects the dropdown on cold load (used by the User Diagnostics → Get Recs jump from session 07).
- Toast warning when "Get Recs" clicked without a user picked.

`#/explore/tracks`:
- Page header eyebrow `EXPLORE`, title `Tracks (N)` — N is the live total and updates after each fetch.
- Right rail: 220 px search input with an absolutely-positioned × clear, secondary "Search" button, primary "Generate Playlist" button.
- Library panel: sub-line summarises current state ("matching `lud` · 48,201 total · sorted by energy ↑").
- 12-column trackTable (`title`, `artist`, `genre`, `bpm`, `key`, `energy`, `dance`, `valence`, `mood`, `duration`, `version`, `id`). Sortable on BPM / Energy / Dance / Valence / Duration / Version (the columns whose backend `_SORT_COLUMNS` lookup actually exists; not on Title/Artist/Genre/Mood since those aren't reliably sortable server-side without ilike-shenanigans).
- First click on a sortable header sets it to `desc` (the typical "show me the biggest first" intent); second click flips to `asc`.
- Pagination prev/next at 50 per page, with mono "Showing X–Y of Z" footer.
- Empty result: contextual ("No tracks match `lud`." / "No tracks. Run a Library scan from Actions → Library to populate.").
- State persistence: `GIQ.state.trackList` retains offset / sort / search across navigations and reloads (cleared only by the user clicking the × on search or changing sort).
- Generate Playlist button toasts `Generate Playlist modal — session 10` until the modal lands in session 10.

Below 700 px:
- Both pages collapse: the trackTable hides its column header and per-cell layout, swapping in the card list. Tested at 600 × 800 — title + artist on top, mono sub-line `BPM · key · E energy · mood · duration`, optional source chip + score bar slotted alongside the title where columns include them, and the row action (e.g. `debug→`) drops onto its own line below the sub-line.

Stubbed (sessions 10 / 11):
- Other Explore sub-pages (radio · playlists · text-search · music-map · charts · artists · news) still render the foundation-session "TBD" placeholder.
- The Generate Playlist modal itself.

## Decisions made (with reasoning)

- **`GIQ.components.trackTable` is one function; columns are an array of string ids, not a callable per-column.** Sessions 10 and 11 will pass different column subsets to the same component; keeping the surface declarative ("here are the ids I want, in order") instead of "here are render functions" means the per-column rendering rules (BPM mono, energy bar lavender, mood = top tag, etc.) live in one place and stay consistent across surfaces. The cost is that adding a brand-new column requires editing the helper switch, but that's fine — there are 16 columns total and they're not changing often.
- **Card layout is rendered alongside the table cells in the same row, not as a sibling row.** Each row has a hidden `.tt-card` block whose CSS swaps from `display: none` to `display: block` below 700 px. The table cells flip to `display: none`. This duplicates the row action element (one in `.tt-c-action`, one in `.tt-card-action`), so 8 rows × 2 = 16 anchors in the DOM where naively you'd expect 8 — verified in tests, both sets carry the right hrefs. Trade-off: small DOM bloat in exchange for not needing a JS-driven reflow on resize. If a future session needs the row action to be stateful (e.g. a button that mutates state), only one of the two will be visible at a time so click handlers still fire correctly. Worth knowing if you're debugging "why did clicking debug→ navigate twice?" — short answer: it didn't, the second anchor is `display: none`.
- **Sort-direction default flips from `asc` (old dashboard) to `desc` on first click.** The old `app.js` defaulted to ascending — fine for BPM, weird for Energy/Dance/Valence where users almost always want "show me the highest first". Sessions 10/11 should follow the same convention if they reuse the trackTable.
- **`reco-debug-link` deep-link carries both `request_id` and `track_id`.** Session 06's Recs Debug detail mode already takes `?debug=<request_id>` and loads the persisted audit. The new `&track=<track_id>` param is reserved for session 12 (or a polish session) to scroll-into-view that specific row in the candidates table. Today the param is set but ignored — confirmed by clicking through and watching the request load detail mode normally with the debug param.
- **State persistence lives in `GIQ.state.recoState` and `GIQ.state.trackList` — not in URL params.** URL params would clutter every keystroke during search and would re-trigger the router on each one. Page-local state is cheap and works as long as the user stays in the same SPA session. The one exception is the user dropdown on Recommendations: that one DOES update the URL (via `history.replaceState`, not `pushState`) so reloads land on the same user without forcing a fresh fetch. Reasoning: changing user is a discrete, low-frequency intent — worth a URL update. Changing search-text or sort is high-frequency — keep it page-local.
- **Hour and Day pickers in the context drawer use string values (`'1'..'7'`, `'0'..'23'`).** The `/v1/recommend` endpoint accepts integers, but URL-search-params are strings everywhere — and the conversion happens server-side via Pydantic. Sending strings is fine and avoids one round of `parseInt` / `String()` conversions in the client.
- **Don't bake API call into the tracks panel header sub-line.** The summary `matching "{search}" · 48,201 total · sorted by {field} {arrow}` is computed each render from `GIQ.state.trackList`. If a future session adds a server-side filter (e.g. `?genre=rock`), update `renderTable` once instead of having to plumb the filter through the component.
- **Use `window.cachedUsers` for the dropdown population.** Already in use by Monitor → User Diagnostics (session 07). Avoids a second `GET /v1/users` call when the user lands on Recommendations after using User Diagnostics. Falls back to a fresh fetch if not present. Cache is in-memory only (cleared on full reload) — not persisted, intentionally.
- **The 220 px tracks search box is inline-block-positioned with the × clear button as an absolutely-positioned overlay.** Mirrors the old dashboard's pattern (`position: absolute; right: 8px; top: 50%`). The × is hidden when search is empty so it doesn't read as a stray decoration. Pressing Enter in the input triggers the search, matching the old-dashboard muscle memory.
- **`vc-btn-sm` was already declared at line 1775 of `pages.css` — I redeclared it in the new tracks-section block by accident, then noticed and left both intact since they're identical.** If a future cleanup pass dedupes them, fine; right now it's a 4-line redeclaration with the same rule body.

## Gotchas for the next session

- **`GIQ.components.trackTable({ columns, rows, sort, sortable, onSort, pagination, rowAction, maxScore, empty }) → DOMElement` is the public surface.** Keep it stable; sessions 10 (playlists detail) and 11 (charts) will reuse it with different column sets. The full column id list is `['rank', 'title', 'artist', 'album', 'genre', 'source', 'score', 'bpm', 'key', 'energy', 'dance', 'valence', 'mood', 'duration', 'version', 'id']`. Only `rank`, `title`, `artist`, `bpm`, `key`, `energy`, `mood`, `duration` are guaranteed to render meaningfully across all data sources; the others fall back to "—" when fields are missing.
- **`source` chip rendering**: backend returns the source string verbatim (e.g. `cf`, `content`, `session_skipgram`, `sasrec`, `lastfm_similar`, `artist_recall`, `popular`, `content_profile`). The component renders whatever string it gets, uppercased and clipped by CSS — it does NOT translate "session_skipgram" to "Session Skip-gram". If a future session wants prettier labels, do the mapping at the call site (the trackTable's job is rendering, not interpretation).
- **`mood_tags` shape varies**: API can return `null`, an array of strings, or an array of `{label, confidence}` objects. The `_ttTopMood` helper handles all three but only ever returns the top label — no confidence display. If a future session needs confidence, extend the column.
- **`duration` is in seconds, formatted MM:SS.** Anything ≥ 100 minutes (1:40:00) overflows to `100:00`-style. Acceptable for a music dashboard; long-form podcasts aren't in scope.
- **Card-mode breakpoint is 700 px.** Above that, the table renders. The collapsed sidebar (60 px) doesn't change the breakpoint — only viewport width matters. If the user has the sidebar expanded on a 1100 px viewport, the table still has ~880 px to render which is enough for all 12 Tracks columns.
- **`?user={id}` deep-link**: handled by `params` argument to `recommendations` page renderer. The page uses `decodeURIComponent` on it before populating state, so encoded hashes (`?user=user%2Bx`) work. The param is also written back via `history.replaceState` when the user changes the dropdown — _not_ on each keystroke of the seed-track input or limit. If you add new persistable filters, decide explicitly whether they should ride the URL.
- **`request_id` truncation**: visible UI shows the first 18 chars + ellipsis ("7f2a3c8e-1234-5678…"). The full id is in the `title` attribute and used verbatim in the deep-link href. Don't depend on the visible truncation.
- **Generate Playlist button is a stub** — wires up to a session-10 modal. The button is in the page right-rail of Tracks. Session 10 should add a modal opener (probably exposed via `GIQ.components.generatePlaylistModal` or `GIQ.actions.openGeneratePlaylist`) and replace the toast call. Session 11 will also want this for Charts.
- **Sort field whitelist**: the backend's `_SORT_COLUMNS` dict in `app/api/routes/tracks.py` only accepts a fixed set of fields. The currently-supported sortable columns are `bpm`, `energy`, `danceability`, `valence`, `duration`, `analysis_version` (plus the default `bpm`). I expose only those on the Tracks page sortable list. Don't add `title`/`artist` to `sortable` without first checking the backend supports them.
- **The trackTable does NOT itself manage state** — pages own state and pass it in. This is intentional so the same component can render with totally different state shapes (radio queue · audit candidates · charts entries · playlist detail). Sessions 10/11 should follow this pattern; don't add internal state to the component.
- **Mocked API harness still required for static-server preview** — same gotcha as sessions 02–08. Documented mocks in this session's transcript: `/v1/users` (array of `{user_id, display_name, uid, created_at}`), `/v1/recommend/{id}?...` (returns `{request_id, model_version, user_id, tracks: [...]}` matching `app/api/routes/recommend.py`), `/v1/tracks?...` (returns `{total, tracks: [...]}` matching `app/api/routes/tracks.py`). Real backend at `10.10.50.5:8000` for full integration.
- **Browser caching during dev**: when iterating on `explore.js` / `components.js` / `pages.css`, the browser served stale copies for ~minutes after `location.reload()`. Workaround: re-fetch the file with a `?_=<timestamp>` query and `new Function(text)()` to re-execute, then `GIQ.router.dispatch()`. CSS works similarly by patching `<link>` href cache-busters. Documented this in the session 02 hand-off too — production isn't affected because nothing here changes between deployments unless the file actually changes.

## Open issues / TODOs

- **Generate Playlist modal** — currently a toast stub. Session 10 owns it. The button is in the Tracks right-rail; the spec also wants it on Recommendations as a future improvement (per `components.md`).
- **Track-scroll-into-view on debug→** — the `&track={track_id}` query param is set but the Recs Debug detail mode (session 06) doesn't currently scroll the candidates table to that row. Polish item for session 12.
- **Title / Artist sort** — backend doesn't support these via `_SORT_COLUMNS`. If a self-hosted user really wants alphabetical, would require a backend change. Out of scope here.
- **Card-mode source chip + score bar layout below 700px** — they currently wrap onto the title row. Looks OK at common phone widths but at 320 px (the IA-spec minimum) the source chip can wrap to its own line. Acceptable; if it ever bothers someone, a `flex-shrink: 0` plus small label hide could improve it.
- **`debug→` link duplicate DOM** — 16 anchors per 8-row table because both table-mode and card-mode are rendered. CPU/memory cost is negligible but if a polish session wants to be tidy: replace the `_ttCardLayout` rowAction call with a CSS-driven approach that reuses the same anchor element. Not blocking.
- **Pagination doesn't show pagination chips for jumping multiple pages** — only Prev/Next. Self-hosted libraries up to ~100k tracks (the typical max) at 50/page = 2000 pages. Most users will use Search instead of paginating — acceptable. Add page-jump if anyone complains.
- **Context drawer doesn't validate `hour_of_day` and `day_of_week` types** — they're sent as strings; backend Pydantic coerces. If the backend ever tightens the validator, the client may need an `int()` coercion before sending.
- **`reco-debug-link` styling** — currently uses lavender `--accent` mono 10px. Mirrors the design hand-off but at high-DPI it can read a touch thin. Consider Inter Tight or weight 600 if a designer review flags it.
- **Tracks search input has a hardcoded 220 px width** — at 600 px viewport it dominates the right-rail. The responsive block re-flows the right-rail to its own line below 700 px. Acceptable; if anyone wants a tablet-specific tweak, add a 700–999 px @media block.

## Verification screenshots

Captured inline in the session transcript via `mcp__Claude_Preview__preview_screenshot` (mocked-API state). Five relevant captures:

1. **Recommendations — empty state** at 1400 × 900: dropdown · seed input · limit · Add context · Get Recs row; below, the centred "Pick a user…" empty card.
2. **Recommendations — populated** at 1400 × 900: cross-link banner with `request_id 7f2a3c8e-1234-5678…` and `Debug this request →`; meta line `8 tracks · simon · MODEL · LGBM-V12`; full Results panel with 10-column table, source chips (cf · content · session_skip · sasrec · lastfm_simil · artist_recall), score bars, energy bars, mood, duration, and `debug→` per row.
3. **Recommendations — context drawer open** at 1400 × 900: 6-field row (Device · Output · Context · Location · Hour · Day) with `mobile` and `headphones` selected.
4. **Tracks — populated** at 1400 × 900: title `Tracks (48,201)`, search box with × clear visible (state from prior search "lud"), Generate Playlist primary; Library panel with sub-line `matching "lud" · 48,201 total · sorted by energy ↑`; 12-column table including Energy bars and the Track ID mono column; pagination footer `Showing 51–58 of 48,201` with Prev / Next.
5. **Card layout — 600 × 800** (Tracks): no header row, each row stacks title + artist + sub-line `BPM · key · E energy · mood · duration`. Same view of the recommendations page in the transcript shows the `debug→` action drops to its own line below the sub-line.

Functional checks evidenced via `preview_eval`:
- 9 rows render in trackTable (1 head + 8 data); 6 sortable headers exposed.
- Click Energy → state.sortBy='energy', sortDir='desc'; click again → 'asc'.
- Search "lud" → state.search='lud', clear-x visible, query string `search=lud` sent.
- Pagination Next → offset advances by 50, footer reads "Showing 51–58 of 48,201".
- Add context → drawer opens with 6 fields; `device_type=mobile` flows into state.ctx.
- Navigate away to Tracks then back to Recommendations → state.userId, state.ctx, state.result (8 tracks) all persist; cached results re-render automatically.
- Cold deep-link `#/explore/recommendations?user=alex` → dropdown pre-selected to `alex`.
- "Get Recs" with no user → toast warning "Pick a user first.".
- Empty `tracks: []` response → trackTable renders the spec empty text.
- `debug→` per-row click → router lands at `#/monitor/recs-debug?debug={request_id}&track={track_id}`; session 06's detail mode loads.
- `Debug this request →` banner click → router lands at `#/monitor/recs-debug?debug={request_id}` (no track param).
- Stubs intact: Explore → radio / charts both render the TBD placeholder.
- Overview regression: 6 stat tiles still render, no console errors.
- No errors in browser console throughout.

## Time spent

≈ 110 min: reading prior hand-offs / app.js patterns / API shapes (25) · trackTable component + helpers (25) · explore.js Recommendations + Tracks (25) · pages.css (~330 new lines) (15) · preview verification + screenshots + fixing the `.track-table-head` specificity bug + the action-column width bump (15) · this hand-off note (5).

---

**For the next session to read:** Session 10 — Explore: Playlists + modal + Text Search + Music Map, see [docs/rebuild/10-explore-playlists-search-map.md](../10-explore-playlists-search-map.md). Session 10 reuses `GIQ.components.trackTable` for Playlists detail and Text Search results, and builds the Generate Playlist modal that this session stubs. Session 11 (Radio + Charts + Artists + News) is also unblocked; Charts will reuse the trackTable.

# Session 12 — Cross-cutting + mobile responsive + cutover

You are continuing the GrooveIQ dashboard rebuild. This is **session 12 of 12** — the final session.

This session ships the rebuild: wires every cross-bucket deep link, makes the dashboard responsive on mobile portrait, and flips `/dashboard` from the old UI to the new.

## Read first

1. `docs/rebuild/README.md`
2. **Every prior hand-off** (01–11). Read the gotchas sections especially.
3. `docs/rebuild/components.md` → **Responsive** section.
4. `gui-rework-plan.md` § 4 "Splits requiring careful design treatment" + § 6 "Design direction → Responsive layout".
5. `design_handoff_grooveiq_dashboard/README.md` § "Responsive" + "Interactions & behaviour".

## Goal

By session end:
- Every cross-bucket deep link works (no dead jumps).
- Every split page has a working "related →" rail to its sibling pages in other buckets.
- The dashboard is usable in portrait at 375px wide.
- `/dashboard` serves the new UI; old is parked at `/static/dashboard-old.html` for rollback.
- `gui-rebuild` branch is squashed-or-merged into `main`.

## Out of scope

- ⌘K command palette (open question; defer to a follow-up unless explicitly approved).
- Light mode polish (defer).
- New features. None.

## Tasks (ordered)

### A. Cross-bucket deep-link audit

Walk every split page from `gui-rework-plan.md` § 4 and verify the deep links:

| From | To | Verify |
|---|---|---|
| Settings → Algorithm save | Monitor → Pipeline | Toast jump-link works after save |
| Settings → Download Routing save | (no Monitor jump) | Save toast renders correctly |
| Settings → Lidarr Backfill save | Monitor → Lidarr Backfill | Toast jump |
| Settings → Lidarr Backfill (related rail) | Actions queue + Monitor stats | Both pills work |
| Actions → Discovery → Lidarr Backfill (related rail) | Settings config + Monitor stats | Both pills work |
| Monitor → Lidarr Backfill (related rail) | Settings config + Actions queue | Both pills work |
| Monitor → Overview → Quick Run | Actions → Pipeline & ML | All 4 row links resolve |
| Monitor → Overview → Models "See all" | Monitor → Models | Works |
| Monitor → Overview → Event ingest "View full breakdown" | Monitor → System Health | Works |
| Monitor → Overview → activity pill rows | Pipeline / System Health / Downloads / Lidarr Backfill | Each row resolves |
| Settings → Users → user detail → "View diagnostics" | Monitor → User Diagnostics?user={id} | URL parses, user pre-filled |
| Monitor → User Diagnostics → "Get Recs" | Explore → Recommendations?user={id} | Pre-filled |
| Monitor → User Diagnostics → "Edit user" | Settings → Users → user detail | Works |
| Explore → Recommendations row "debug→" | Monitor → Recs Debug?debug={request_id} | Loads request detail |
| Explore → Charts row "⬇ get" | Triggers download; visible in Monitor → Downloads | End-to-end |
| Explore → Artists "Play radio" | Explore → Radio?seed_type=artist&seed_value={name} | Pre-filled |
| Explore → Tracks "Generate Playlist" | Generate modal → playlist detail | Works |
| Explore → Music Map "Build Path" | Generate modal (path strategy) → playlist detail | Works |
| Activity pill on every page | Always works; no leaked subscriptions on nav-away | Works |

If any deep link is broken, fix it in the source page module — don't pile fixes here.

### B. Responsive layout pass

Add media queries (in `shell.css` and per-page CSS):

**≥ 1100px** — current default (full sidebar 220px, 2-col body grids).

**700–1099px** — collapsed sidebar (60px icon-only, no expand toggle), main column full width, 2-col grids collapse to single column where layout is vertical-friendly. Stat-row stays 6-col but with smaller padding.

**< 700px** — replace sidebar with a **bottom tab bar**:
- Fixed at bottom, full width, height 60px, background `--paper`, top border `1px solid --line-faint`.
- Four buckets: Explore (♪) / Actions (⚡) / Monitor (◉) / Settings (⚙) with icon + label.
- Active bucket: `--accent` text + 2px top border.
- Sidebar component hides at this breakpoint.
- Activity pill becomes a small floating button bottom-right (above the bottom tab bar).

**Component-level responsive treatments:**
- **Track tables** (Tracks, Recommendations, Playlists detail, Charts, Audit candidates) → card layout below 700px, per session 09 spec.
- **Versioned-config 2-col grids** → single column.
- **Diff modals** → stack vertically.
- **Pipeline flow diagram** → vertical stack, arrows become "↓".
- **Music Map** → "Best on desktop" notice + canvas.
- **6-stat tile rows** → 3-col at 700–1099px, 2-col at < 700px.
- **2-col body grids** → single col at < 1100px.
- **Top bar sub-page tabs** → already horizontally scrollable, no change.

Smoke-test every page at 375px (iPhone SE) and 1440px wide using `preview_resize`.

### C. Cleanup

1. **Remove old code references** in new files. If anything imports / references `app.js` (the old monolith), purge it.
2. **Old files stay** until cutover (see step D). Don't delete `index.html`, `style.css`, `app.js` yet.
3. **Console errors** — load every page and ensure zero errors / warnings (use `preview_console_logs`).
4. **Network errors** — every fetch gets a clean response; 4xx errors render as toasts not crashes.
5. **Bundle size sanity check** — total JS shouldn't be dramatically larger than the old `app.js`. Aim for parity.

### D. Cutover

Once verification passes:

1. **Move old to backup**:
   ```bash
   mv app/static/index.html app/static/dashboard-old.html
   ```
2. **Rename new to primary**:
   ```bash
   mv app/static/dashboard-v2.html app/static/index.html
   ```
3. Verify `/dashboard` (which serves `index.html` per `app/main.py:227`) now loads the new UI. Verify `/static/dashboard-old.html` still loads the old UI (for rollback).
4. **Update `app/main.py` if needed** — only if there's a hard-coded reference somewhere. The `FileResponse` in `dashboard()` reads `index.html`, so the rename should be sufficient.
5. **Update CLAUDE.md** — replace any references to old tab structure with the new four-bucket IA. Update the "Web dashboard" bullet under "Additionally built" to describe the new structure.
6. **Smoke test on production-like config** — start the server with `LOG_JSON=true` and a real `.env`, hit `/dashboard`, verify everything loads.

### E. Merge

1. **Final commit on `gui-rebuild`**: `rebuild: session 12 — cutover`.
2. **Open PR to `main`** with a summary linking back to `docs/rebuild/README.md`.
3. **Title**: `GUI rebuild: four-bucket IA, new design language`.
4. **Body**: brief summary of what shipped, link to handoffs/, callout to the rollback path (`/static/dashboard-old.html`).
5. **Do NOT auto-merge** — leave for human review.

### F. Final hand-off

Write `handoffs/12-cutover.md` describing the cutover state, any deferred items (light mode, ⌘K, etc.), and rollback instructions.

## Verification

1. Visit `/dashboard` → loads the new UI in dark mode at desktop width.
2. Resize to 375px → bottom tab bar appears, sidebar hides, tables collapse to cards, stat rows wrap, no horizontal overflow.
3. Visit every Explore + Actions + Monitor + Settings sub-page — all render without console errors.
4. Click every cross-bucket deep link from the table in step A. All resolve.
5. Trigger a pipeline run from `#/actions/pipeline-ml` → activity pill updates → `#/monitor/pipeline` flows the SSE.
6. Visit `/static/dashboard-old.html` → old UI still renders for rollback.
7. PR open against `main`, awaiting review.

## Done

The rebuild is complete. The user (Daniel) reviews and merges. Old code can be deleted in a separate cleanup PR ~1 week post-merge once it's clear no rollback is needed.

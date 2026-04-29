# API endpoint map

Every page in the new GUI maps to existing endpoints. **No new endpoints.** If a session thinks it needs one, stop and surface it.

Authoritative endpoint list: `CLAUDE.md` (root) and `docs/API.md`. This doc is a quick lookup.

All endpoints require `Authorization: Bearer <key>` except `/health` and `/dashboard`.

## Explore

| Page | Endpoints |
|---|---|
| Recommendations | `GET /v1/recommend/{user_id}` (+ `?debug=true` for cross-link to Recs Debug) · `GET /v1/users` (for dropdown) |
| Radio | `POST /v1/radio/start` · `GET /v1/radio/{id}/next` · `DELETE /v1/radio/{id}` · `GET /v1/radio` · feedback via `POST /v1/events` (context_type=radio) |
| Playlists | `GET /v1/playlists` · `GET /v1/playlists/{id}` · `DELETE /v1/playlists/{id}` |
| Tracks | `GET /v1/tracks` (search, filter, sort, paginate) · `GET /v1/tracks/{id}/features` · `GET /v1/tracks/{id}/similar` |
| Text Search | `GET /v1/tracks/text-search?q=&limit=` · `GET /v1/tracks/clap/stats` |
| Music Map | `GET /v1/tracks/map?limit=` |
| Charts | `GET /v1/charts` · `GET /v1/charts/{type}?scope=&limit=&offset=` · `POST /v1/charts/download` |
| Artists | `GET /v1/artists/{name}/meta` · `GET /v1/recommend/{user_id}/artists` |
| News | `GET /v1/news/{user_id}?limit=&tag=&subreddit=` (planned — surface "coming soon" if not implemented) |

## Actions (POST-style triggers)

| Action | Endpoint |
|---|---|
| Run Pipeline | `POST /v1/pipeline/run` |
| Reset Pipeline | `POST /v1/pipeline/reset` |
| Backfill CLAP | `POST /v1/tracks/clap/backfill?limit=` |
| Cleanup Stale Tracks | `POST /v1/library/cleanup-stale?dry_run=&pattern=` |
| Scan Library | `POST /v1/library/scan` |
| Sync IDs | `POST /v1/library/sync` |
| Run Lidarr Discovery | `POST /v1/discovery/run` |
| Run Fill Library | `POST /v1/fill-library/run?max_albums=` |
| Run Lidarr Backfill (now) | `POST /v1/lidarr-backfill/run` |
| Soulseek Bulk Download | (existing endpoint per current Discovery → Soulseek sub-tab — verify in `app/static/js/app.js` `lbf*` and `slsk*` functions) |
| Build Charts | `POST /v1/charts/build` |
| Search & Download (multi) | `GET /v1/downloads/search/multi?q=&limit=&backends=&timeout_ms=` then `POST /v1/downloads/from-handle` |
| Search & Download (single) | `GET /v1/downloads/search?q=` then `POST /v1/downloads` |
| Cancel download | `DELETE /v1/downloads/{id}` (verify endpoint name in current code) |

For Lidarr Backfill queue management (Actions side):
- Per-row: `POST /v1/lidarr-backfill/requests/{id}/retry` · `POST /v1/lidarr-backfill/requests/{id}/skip` · `DELETE /v1/lidarr-backfill/requests/{id}`
- Bulk: `POST /v1/lidarr-backfill/requests/reset` body `{"scope":"failed"|"no_match"|"permanently_skipped"|"all"}`
- Preview: `POST /v1/lidarr-backfill/preview`
- List: `GET /v1/lidarr-backfill/requests?status=&artist=&limit=&offset=`

## Monitor

| Page | Endpoints |
|---|---|
| Overview | `GET /v1/stats` · `GET /v1/pipeline/models` · `GET /v1/pipeline/status` · `GET /v1/library/scan/{scan_id}` · SSE `/v1/pipeline/stream` |
| Pipeline | `GET /v1/pipeline/status?limit=` · `GET /v1/pipeline/models` · SSE `/v1/pipeline/stream` |
| Models | `GET /v1/pipeline/models` |
| System Health | `GET /v1/stats` · `GET /v1/pipeline/stats/events` · `GET /v1/pipeline/stats/activity?days=` · `GET /v1/pipeline/stats/engagement` · `GET /v1/library/scan/{scan_id}` |
| Recs Debug | `GET /v1/recommend/audit/sessions?user_id=&surface=&since_days=&limit=&offset=` · `GET /v1/recommend/audit/{request_id}` · `POST /v1/recommend/audit/{request_id}/replay` · `GET /v1/recommend/audit/stats` · `GET /v1/recommend/{user_id}?debug=true` · `GET /v1/pipeline/stats/sessionizer` · `GET /v1/pipeline/stats/scoring` · `GET /v1/pipeline/stats/taste_profiles` |
| User Diagnostics | `GET /v1/users/{id}` · `GET /v1/users/{id}/profile` · `GET /v1/users/{id}/interactions` · `GET /v1/users/{id}/history` · `GET /v1/users/{id}/sessions` · `GET /v1/users/{id}/lastfm/profile` |
| Integrations | `GET /v1/integrations/status` |
| Downloads (queue + telemetry) | `GET /v1/downloads?status=&limit=&offset=` · `GET /v1/downloads/stats?days=` · `GET /v1/downloads/status/{task_id}` |
| Lidarr Backfill (stats) | `GET /v1/lidarr-backfill/stats` · `GET /v1/lidarr-backfill/requests?status=&limit=&offset=` |
| Discovery (stats) | `GET /v1/discovery` · `GET /v1/discovery/stats` · `GET /v1/fill-library` · `GET /v1/fill-library/stats` |
| Charts (stats) | `GET /v1/charts/stats` |

## Settings

| Page | Endpoints |
|---|---|
| Algorithm Config | `GET /v1/algorithm/config` · `GET /v1/algorithm/config/defaults` · `GET /v1/algorithm/config/history` · `GET /v1/algorithm/config/{version}` · `PUT /v1/algorithm/config` · `POST /v1/algorithm/config/reset` · `POST /v1/algorithm/config/activate/{version}` · `GET /v1/algorithm/config/export` · `POST /v1/algorithm/config/import` |
| Download Routing | `GET /v1/downloads/routing` · `GET /v1/downloads/routing/defaults` · `GET /v1/downloads/routing/history` · `GET /v1/downloads/routing/{version}` · `PUT /v1/downloads/routing` · `POST /v1/downloads/routing/reset` · `POST /v1/downloads/routing/activate/{version}` · `GET /v1/downloads/routing/export` · `POST /v1/downloads/routing/import` |
| Lidarr Backfill Config | `GET /v1/lidarr-backfill/config` · `GET /v1/lidarr-backfill/config/defaults` · `GET /v1/lidarr-backfill/config/history` · `GET /v1/lidarr-backfill/config/{version}` · `PUT /v1/lidarr-backfill/config` · `POST /v1/lidarr-backfill/config/reset` · `POST /v1/lidarr-backfill/config/activate/{version}` · `GET /v1/lidarr-backfill/config/export` · `POST /v1/lidarr-backfill/config/import` |
| Connections (snapshot) | `GET /v1/integrations/status` (read-only — same data source as Monitor → Integrations) |
| Users | `GET /v1/users` · `POST /v1/users` · `GET /v1/users/{id}` · `PATCH /v1/users/{id}` · `POST /v1/users/{id}/onboarding` · `GET /v1/users/{id}/onboarding` |
| Users → Last.fm (per user) | `POST /v1/users/{id}/lastfm/connect` · `DELETE /v1/users/{id}/lastfm` · `POST /v1/users/{id}/lastfm/sync` · `GET /v1/users/{id}/lastfm/profile` |

## Health (no auth)

`GET /health` — used to validate API key on connect.

## Notes

- The full SSE bus subscribes once to `/v1/pipeline/stream` and re-broadcasts to subscribers (see `components.md → SSE bus`). No page should open its own EventSource.
- The activity pill polls a lightweight summary endpoint that doesn't exist yet — assemble the data client-side from `/v1/pipeline/status`, `/v1/library/scan/{id}`, `/v1/downloads?status=in_flight`, etc. If polling becomes wasteful, a future session can add a single `/v1/activity` endpoint, but **not during the rebuild**.
- All endpoints already exist. If you find a page can't be built without a new endpoint, surface it in the session's hand-off note instead of inventing one.

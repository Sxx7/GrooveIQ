# Handoff — iOS integration of GrooveIQ CLAP playlists

> Audience: Claude Code (or a developer) working in the iOS app repo. Self-contained — no need to read GrooveIQ source. Originally verified live against issue [Sxx7/GrooveIQ#89](https://github.com/Sxx7/GrooveIQ/issues/89) on 2026-05-09; the API contract below was re-confirmed against the GrooveIQ source.

---

## 1. Context

**GrooveIQ** is a self-hosted FastAPI music recommendation backend. It analyses a private music library with Essentia (audio features, mood tags, embeddings) and optionally with **LAION-CLAP** — a model that maps natural-language descriptions into the same vector space as the audio. Once CLAP is enabled, the API can rank tracks by cosine similarity to a free-text prompt.

The endpoint that turns a prompt into a playlist is `POST /v1/playlists` with `strategy: "text"`. The user calls these "CLAP playlists." The product UX is a grid of **mix tiles** — each tile is a stable preset (title + a fixed prompt). Tapping one calls the endpoint and starts playback.

The server is deployed at:

```
http://<prod-host>:8000   # internal LAN
```

Reverse-proxied via Caddy externally — confirm the actual internal and public hostnames with the user before hardcoding.

---

## 2. Auth

All endpoints except `/health` require:

```
Authorization: Bearer <api-key>
Content-Type: application/json
```

Store the API key in **Keychain**, never in `UserDefaults`. The user provisions keys server-side; do not attempt to create accounts or rotate keys from the app.

Rate limit: **200 req/min per key**. A burst of POSTs from app launch (mix grid with 12 tiles, all hydrated at once) will hit it — debounce hydration to user gestures or stagger calls.

---

## 3. The endpoint

### `POST /v1/playlists`

Body (Codable shape below):

```json
{
  "name": "Late night drive",
  "strategy": "text",
  "params": { "prompt": "moody synthwave, driving mid-tempo arpeggiated bass, warm analog pads, neon city atmosphere, instrumental, polished production" },
  "max_tracks": 25
}
```

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `name` | string (1–255) | yes | Human-visible title. **Excluded from the cache key** — vary it freely without cache-busting. |
| `strategy` | enum | yes | Use `"text"` for CLAP. Other values: `mood`, `energy_curve`, `flow`, `key_compatible`, `path`. |
| `params.prompt` | string | yes (for `text`) | The CLAP prompt. See §7 for what works. |
| `max_tracks` | int (5–100) | no, default 25 | Playlist length. Different `max_tracks` ⇒ different cache key ⇒ different playlist. |
| `seed_track_id` | string | n/a for `text` | Required for `flow`, `key_compatible`, `path`. |

Query param:

| Param | Default | Effect |
|-------|---------|--------|
| `refresh` | `false` | `true` bypasses the cache and forces regeneration (always returns `201`). Wire to a "Refresh this mix" gesture. |

### Response

`PlaylistDetailResponse` — same shape on both `200` and `201`:

```json
{
  "id": 42,
  "name": "Late night drive",
  "strategy": "text",
  "track_count": 25,
  "total_duration": 5847.3,
  "created_at": 1746825600,
  "tracks": [
    {
      "position": 0,
      "track_id": "9f3a1b2c4d5e6f70",          // GrooveIQ internal ID
      "media_server_id": "qhiFiRW0x0Ux612N02Xmgu", // Navidrome / Subsonic ID — use this for playback
      "title": "...",
      "artist": "...",
      "album": "...",
      "duration": 234.5,
      "bpm": 118.0,
      "energy": 0.62,
      "valence": 0.41,
      "danceability": 0.55,
      "mood_tags": [{"label": "relaxed", "confidence": 0.78}]
    }
  ]
}
```

**Two IDs per track, on purpose** — `media_server_id` is what you send to Navidrome/Subsonic to play. `track_id` is what you send back to GrooveIQ (e.g. `POST /v1/events` for play/skip/like — feeds the recommender).

### Status codes

| Status | Meaning | iOS handling |
|--------|---------|--------------|
| `201 Created` | Fresh generation | Normal path. |
| `200 OK` | Cache hit (same caller + same body params + same UTC day) | Identical to 201. Treat as success. Optional: log for cache-hit telemetry. |
| `400 Bad Request` | The strategy rejected the input or produced no tracks (empty `prompt`, unknown `strategy`, empty result set) | Show inline error; don't retry without changing input. |
| `401 Unauthorized` | Bad/missing API key | Re-prompt for credentials; clear keychain entry. |
| `422 Unprocessable Entity` | Request body failed schema validation (`max_tracks` outside 5–100, wrong field types) | Fix the request; treat like a 400. |
| `429 Too Many Requests` | Rate limit | Back off (use `Retry-After` if present, else 60s). |
| `500 Internal Server Error` | Server bug | Surface a generic error; log the request body + response for diagnosis. |
| `503 Service Unavailable` | CLAP disabled, model files missing, or no tracks have CLAP embeddings yet | "Text mixes aren't available on this server right now." |

---

## 4. Daily idempotency cache (the important behavior)

The server **already deduplicates** repeated calls. It persists a `cache_key` column on each playlist row (added in GrooveIQ migration `015_add_playlist_cache_key.py`, issue #89) and looks it up before generating. Specifically:

- Cache key = `sha256(...)[:32]` over a canonical JSON blob of `{owner, strategy, seed, params, max_tracks, day}`, where `owner` is the **hash of the calling API key**, `params` is canonicalised with sorted keys (so dict ordering can't change the hash), and `day` is `YYYY-MM-DD` in UTC. `name` is **not** in the key.
- Same key today → the existing `playlist.id` is returned (status `200`)
- New UTC day, different params (including a different `max_tracks` or `seed_track_id`), or `?refresh=true` → fresh playlist (status `201`)

**This means the iOS app must NOT cache `playlist.id` per mix on the client.** Rely on the server. Concretely:

- ❌ Do **not** map `mixId → playlistId` in `UserDefaults` / Core Data and reuse it across launches.
- ❌ Do **not** compute the day yourself and try to be clever about when to refetch.
- ❌ Do **not** vary `name` to "force a new playlist" — name is excluded from the key.
- ✅ **Do** call POST every Play tap. The server is idempotent.
- ✅ **Do** wire `?refresh=true` to a user-visible "Refresh" affordance (long-press, swipe, pull-to-refresh on the mix card).
- ✅ **Do** treat the returned `playlist.id` as valid for the rest of today only — fine to use for "/playlists/{id}" deep links and "now playing from" bylines.

---

## 5. Suggested Swift architecture

Keep it small. Three layers:

### 5a. Models (`PlaylistAPI.swift`)

```swift
import Foundation

enum PlaylistStrategy: String, Codable {
    case text, mood, energyCurve = "energy_curve", flow, keyCompatible = "key_compatible", path
}

struct PlaylistGenerateRequest: Codable {
    let name: String
    let strategy: PlaylistStrategy
    let params: [String: AnyCodable]?     // prompt, mood, curve, etc.
    let maxTracks: Int?
    let seedTrackId: String?

    enum CodingKeys: String, CodingKey {
        case name, strategy, params
        case maxTracks = "max_tracks"
        case seedTrackId = "seed_track_id"
    }
}

struct PlaylistTrack: Codable, Identifiable {
    var id: String { trackId }
    let position: Int
    let trackId: String
    let mediaServerId: String?    // play this on Navidrome/Subsonic
    let title: String?
    let artist: String?
    let album: String?
    let duration: Double?
    let bpm: Double?
    let energy: Double?
    let valence: Double?
    let danceability: Double?

    enum CodingKeys: String, CodingKey {
        case position, title, artist, album, duration, bpm, energy, valence, danceability
        case trackId = "track_id"
        case mediaServerId = "media_server_id"
    }
}

struct Playlist: Codable, Identifiable {
    let id: Int
    let name: String
    let strategy: String
    let trackCount: Int
    let totalDuration: Double?
    let createdAt: Int
    let tracks: [PlaylistTrack]

    enum CodingKeys: String, CodingKey {
        case id, name, strategy, tracks
        case trackCount = "track_count"
        case totalDuration = "total_duration"
        case createdAt = "created_at"
    }
}

enum PlaylistGenerateResult {
    case freshlyGenerated(Playlist)   // server returned 201
    case cacheHit(Playlist)            // server returned 200
}
```

`AnyCodable` is whatever you already use to handle dynamic JSON — if you don't have one, add a tiny wrapper or just use `[String: String]` since CLAP `prompt` is a string.

### 5b. Service (`PlaylistService.swift`)

```swift
actor PlaylistService {
    let baseURL: URL
    let apiKey: String      // pulled from Keychain at construction
    let session: URLSession

    init(baseURL: URL, apiKey: String, session: URLSession = .shared) {
        self.baseURL = baseURL
        self.apiKey = apiKey
        self.session = session
    }

    func generate(_ body: PlaylistGenerateRequest, refresh: Bool = false) async throws -> PlaylistGenerateResult {
        var components = URLComponents(url: baseURL.appendingPathComponent("v1/playlists"), resolvingAgainstBaseURL: false)!
        if refresh { components.queryItems = [URLQueryItem(name: "refresh", value: "true")] }

        var req = URLRequest(url: components.url!)
        req.httpMethod = "POST"
        req.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONEncoder().encode(body)

        let (data, response) = try await session.data(for: req)
        guard let http = response as? HTTPURLResponse else { throw PlaylistError.invalidResponse }

        switch http.statusCode {
        case 200:
            return .cacheHit(try JSONDecoder().decode(Playlist.self, from: data))
        case 201:
            return .freshlyGenerated(try JSONDecoder().decode(Playlist.self, from: data))
        case 400, 422:
            throw PlaylistError.invalidRequest(decodedDetail(data))
        case 401:
            throw PlaylistError.unauthorized
        case 429:
            throw PlaylistError.rateLimited(retryAfter: http.value(forHTTPHeaderField: "Retry-After").flatMap(Int.init))
        case 503:
            throw PlaylistError.serviceUnavailable
        default:
            throw PlaylistError.server(status: http.statusCode, body: String(data: data, encoding: .utf8) ?? "")
        }
    }

    private func decodedDetail(_ data: Data) -> String {
        struct E: Decodable { let detail: String? }
        return (try? JSONDecoder().decode(E.self, from: data))?.detail ?? "Unknown error"
    }
}

enum PlaylistError: LocalizedError {
    case invalidResponse, unauthorized, serviceUnavailable
    case invalidRequest(String)
    case rateLimited(retryAfter: Int?)
    case server(status: Int, body: String)
}
```

### 5c. Mix presets (`MixCatalog.swift`)

Keep them static. Each tile = one entry. The user gets a fresh playlist each UTC day for free.

```swift
struct Mix: Identifiable, Hashable {
    let id: String                 // stable client-side, NOT sent to server
    let title: String              // shown on the tile
    let subtitle: String?          // e.g. "Synthwave • Drive"
    let artworkAsset: String?      // local image asset name
    private let prompt: String     // the CLAP prompt
    private let maxTracks: Int

    func request() -> PlaylistGenerateRequest {
        PlaylistGenerateRequest(
            name: title,
            strategy: .text,
            params: ["prompt": AnyCodable(prompt)],
            maxTracks: maxTracks,
            seedTrackId: nil
        )
    }
}

enum MixCatalog {
    static let all: [Mix] = [
        Mix(id: "late-night-drive",
            title: "Late night drive",
            subtitle: "Synthwave • Driving",
            artworkAsset: "mix_drive",
            prompt: "moody synthwave, driving mid-tempo arpeggiated bass, warm analog pads, neon city atmosphere, instrumental, polished production",
            maxTracks: 25),
        // …add more from §7
    ]
}
```

### 5d. View layer

A minimal SwiftUI sketch:

```swift
struct MixGridView: View {
    let service: PlaylistService

    var body: some View {
        LazyVGrid(columns: [.init(.adaptive(minimum: 160))]) {
            ForEach(MixCatalog.all) { mix in
                MixTile(mix: mix)
                    .onTapGesture { Task { await play(mix) } }
                    .contextMenu {
                        Button("Refresh mix") { Task { await play(mix, refresh: true) } }
                    }
            }
        }
    }

    private func play(_ mix: Mix, refresh: Bool = false) async {
        do {
            let result = try await service.generate(mix.request(), refresh: refresh)
            switch result {
            case .freshlyGenerated(let p): startPlayback(p)
            case .cacheHit(let p):         startPlayback(p)
            }
        } catch PlaylistError.rateLimited(let retryAfter) {
            // schedule a retry after `retryAfter ?? 60` seconds
        } catch {
            // surface a toast
        }
    }
}
```

`startPlayback(_:)` hands off to the existing Navidrome/Subsonic playback code: iterate `playlist.tracks`, use each `mediaServerId` to build the play URL.

---

## 6. Recording playback events

After tracks start playing, fire events back to GrooveIQ — this trains the recommender. **Use `track_id`, not `media_server_id`**, for the event payload:

```swift
// POST /v1/events  (single) or /v1/events/batch
{
  "user_id": "<navidrome_user_id>",
  "track_id": "9f3a1b2c4d5e6f70",
  "event_type": "play_start",    // lifecycle: play_start / play_end (value = completion 0–1); also skip, like, dislike
  "context_type": "playlist",
  "context_id": "42",            // the playlist.id
  "surface": "playlist_view",
  "timestamp": 1746825600
}
```

This gives the recommender the signal that "this CLAP playlist worked / didn't work" so subsequent ranking improves.

---

## 7. CLAP prompt patterns (curated for the music model)

The server runs a LAION-CLAP model — `larger_clap_music_and_speech`, auto-downloaded as a pre-exported ONNX from the [`Xenova/larger_clap_music_and_speech`](https://huggingface.co/Xenova/larger_clap_music_and_speech) Hugging Face repo. It was trained predominantly on **MusicCaps-style descriptive captions** — not abstract concepts, not lyrics topics, not artist names, not years.

**Best results: 3–8 sonic attributes, comma-separated, mentioning some of {genre/sub-genre, instrument, BPM-feel, vocal type, production style, mood, scene}.**

### Mix presets that work well — drop-in for `MixCatalog.all`

```swift
// Mood / scene
("late-night-drive",     "Late night drive",      "moody synthwave, driving mid-tempo arpeggiated bass, warm analog pads, neon city atmosphere, instrumental, polished production"),
("sunday-coffee",        "Sunday morning coffee", "mellow acoustic folk, fingerpicked guitar, soft male vocals, warm recording, slow tempo, cozy and intimate"),
("rainy-jazz",           "Rainy night jazz",      "smoky late-night jazz, brushed drums, upright bass walking line, muted trumpet, sparse piano, melancholic and reflective"),
("focus-deep-work",      "Deep focus",            "minimal ambient electronic, slow tempo, no vocals, long evolving pads, subtle granular textures, calm and meditative"),
("workout-steady-high",  "Workout — steady high", "high energy electronic, four-on-the-floor kick at 128 bpm, driving bassline, bright synths, motivating and relentless"),
("cooldown",             "Cooldown",              "chillhop, dusty boom-bap drums, mellow Rhodes piano, vinyl crackle, jazzy chords, slow and relaxed"),

// "Sound of an era" — describe the SOUND, never the year
("y2k-pop",              "Y2K pop",               "glossy late-90s/early-2000s pop production, programmed beats, layered female vocals with autotune, bright synths, polished and radio-ready"),
("70s-soul",             "70s soul",              "warm analog 70s soul, syncopated bass, tight horn section, wah guitar, rhodes piano, soulful male and female vocals"),
("80s-arena-rock",       "80s arena rock",        "big arena rock, gated reverb drums, distorted electric guitars, anthemic chorus, raspy male vocals, bright 80s production"),
("lofi-indie",           "Lo-fi indie",           "lo-fi indie rock, fuzzy guitars, tape hiss, untreated room reverb, reedy male vocal, mid-tempo, melancholic and intimate"),

// Instrument-led
("solo-piano",           "Solo piano",            "solo grand piano, no other instruments, classical romantic style, expressive dynamics, slow to mid tempo, contemplative"),
("singer-songwriter",    "Singer-songwriter",     "solo acoustic guitar fingerpicking, intimate male vocal, no drums, minimal arrangement, vulnerable and personal"),

// Activity
("yoga-stretch",         "Yoga stretch",          "gentle ambient acoustic, soft handpan, slow breath-paced tempo, no vocals, calm and grounding, natural reverb"),
("dinner-party",         "Dinner party",          "smooth bossa nova, nylon guitar, brushed drums, light percussion, warm female vocals in Portuguese, sophisticated and relaxed"),
("road-trip-rock",       "Road trip rock",        "mid-tempo classic rock, distorted guitars, steady drums, gravelly male vocals, anthemic, road-trip energy"),
```

### What to avoid in prompts

- **Years** ("songs from 2003") — CLAP doesn't see metadata; describe the *sound* of the era
- **Lyrics topics** ("songs about heartbreak") — CLAP scores audio, not lyric meaning
- **Artist names** ("like Daft Punk") — translate to sound: "filtered French house, vocoded vocals, four-on-the-floor disco bass, funky guitar"
- **Bare labels** ("happy") — pair with at least 3 sonic attributes
- **Negation** ("no guitar") — CLAP largely ignores it; just describe what you *do* want

---

## 8. CLAP availability & a point-in-time prod note

### Current contract

When CLAP is enabled and embeddings exist, the `text` strategy works as documented above. When CLAP is **not** available — `CLAP_ENABLED=false`, model files missing, text encoding fails, or no tracks have CLAP embeddings yet — the server's `text` code path raises a **`503 Service Unavailable`** (it surfaces a `PlaylistServiceUnavailableError`). That is the signal to treat as "text mixes aren't available right now," per the §3 status table. A genuinely bad request (empty `prompt`, bad `max_tracks`, unknown `strategy`) is still a `400`.

So in normal operation you should never see a `500` from a `text`-strategy POST; treat any `500` as a server bug to log, not an expected state.

### Point-in-time note (as of 2026-05-09 — may be stale)

> At the time this handoff was written, the `text` strategy was returning **HTTP 500** on the production server due to a pre-existing Python 3.12 `SyntaxError` in `app/services/clap_text.py` (duplicate `global` declaration in `_load()`), tracked separately. **This was a deployment-time bug, not the intended contract** (the code path is meant to return `503`, see above). Whether a given server is still affected can only be confirmed by hitting it — don't assume from this doc.

While bringing the integration up against a server where `text` is unavailable for any reason (the bug above, CLAP disabled, or embeddings still backfilling):

- Build the integration end-to-end against the `mood` strategy (substitute `params: {"mood": "happy" | "sad" | "relaxed" | "party" | "aggressive"}`). The cache behavior, response shape, and error handling are identical.
- Once `text` is available on the target server, swap your `MixCatalog` entries from `mood` to `text` — no other code changes needed.

Defensive client posture: treat both `503` (expected "unavailable") and any unexpected `500` from a `text`-strategy POST as a recoverable "service issue, try a non-text mix" rather than a hard failure.

---

## 9. Test plan

Manual smoke (requires API key):

1. Tap any mix tile → expect `201`, playback starts, `Now Playing` shows tracks.
2. Tap the same tile again within the same UTC day → expect `200`, same `playlist.id`, same tracks.
3. Long-press the tile → "Refresh mix" → expect `201`, new `playlist.id`, different tracks.
4. Tap a different mix → expect `201`, unrelated `playlist.id`.
5. Background the app for 5 minutes, return, tap the original mix → expect `200` again.
6. Roll device clock past UTC midnight (or wait), tap original mix → expect `201` (new daily bucket).

Unit:
- `PlaylistService.generate(_:refresh:)` decodes both `200` and `201` responses into the right `PlaylistGenerateResult` case.
- `Mix.request()` produces the expected JSON when encoded with the project's `JSONEncoder`.

---

## 10. Don'ts checklist

- [ ] Don't store API keys in `UserDefaults` or `Info.plist` — use Keychain.
- [ ] Don't cache `playlist.id` per mix on the client across launches.
- [ ] Don't compute UTC day on the device to decide when to re-POST — the server already buckets.
- [ ] Don't vary `name` to defeat the cache — use `?refresh=true`.
- [ ] Don't fan out `POST /v1/playlists` for all 12 mix tiles on app launch — debounce to gestures.
- [ ] Don't send `media_server_id` to GrooveIQ (events, etc.) — send `track_id`.
- [ ] Don't send `track_id` to Navidrome — send `media_server_id`.
- [ ] Don't try to recover from `429` by retrying immediately — back off ≥ `Retry-After`.
- [ ] Don't crash on `200` — it's a success, just a cache hit.

---

## 11. Quick reference

| Need | Do |
|------|----|
| Play a mix | `POST /v1/playlists` with the mix's body |
| Refresh a mix | Same, plus `?refresh=true` |
| Show the playlist later | `GET /v1/playlists/{id}` |
| List all today's playlists | `GET /v1/playlists?limit=20` |
| Delete a playlist | `DELETE /v1/playlists/{id}` (creator/admin only) |
| Tell the recommender what happened | `POST /v1/events` with `track_id` + `context_id=playlist.id` |

That's the whole surface area for the CLAP-mix UX.

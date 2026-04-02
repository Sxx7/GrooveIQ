Here's the full picture.

Research findings: the gap is real
You're right that nothing like this exists for self-hosted. Here's what's out there and why each falls short:
ListenBrainz / Explo — metadata and scrobble history only. No skip, volume, or session data. Recommendations require sending data to an external server.
SoulSync — focuses on automated discovery and downloading, not behavioral learning from your own listening patterns.
Navidrome / Jellyfin — they have play tracking internally, but expose none of it to an external recommendation engine. There's no API contract for a third party to read "user skipped 8 seconds in."
Writers in the self-hosting community have noted that "the biggest thing missing is music discovery" — self-hosted servers are just your library, with no algorithm to surface what you'd actually enjoy next. Joe Karlsson The gap isn't the server — it's the behavior-aware recommendation layer sitting between your apps and your library. That's exactly what you're describing.

Project: GrooveIQ — behavioral recommendation engine for self-hosted music
What it does in plain terms
Your music player (Navidrome, Jellyfin, Symfonium, etc.) sends tiny event packets to GrooveIQ as you listen — things like "played 94% of this track," "skipped at 12 seconds," "turned volume up." GrooveIQ silently builds a taste model from those events, and when your player asks for a recommendation, it returns a ranked feed of songs and playlists tailored to how you listen, not just what you've listened to.
Everything runs on your server. Nothing leaves.

Why behavior signals beat metadata alone
Platforms like Spotify split user feedback into two buckets: explicit signals (likes, saves, skips, playlist adds) and implicit signals (listening-session length, playthrough rate, repeat listens). Music-tomorrow Every skip, replay, and save feeds back into the algorithm, so two people with similar base tastes can diverge significantly based on how they personally interact with music. Beats To Rap On Metadata-only systems like ListenBrainz completely miss the implicit half. GrooveIQ captures both.
A key insight from Spotify's system that you should bake in: a skip doesn't always mean dislike — context matters. In an exploratory playlist, high skip rates are expected. A skip from a "deep focus" session carries much more weight as a negative signal. Music-tomorrow Your engine needs to track what context the skip happened in, not just that a skip happened.

Roadmap
Phase 1 — Event ingestion & storage (weeks 1–4)
The foundation. Build the API that receives events and stores them cleanly.
Events to collect per play session:

play_start, play_end, skip (with timestamp of when in the track)
repeat, like, dislike
volume_change (relative: did they turn it up = positive signal?)
seek (scrubbing back = very positive; scrubbing forward = mild negative)
queue_add, playlist_add
session_context (time of day, device type if available)

Stack recommendation: Python (FastAPI) + SQLite to start. Simple, self-hostable, no dependencies. Every event gets: user_id, track_id, event_type, value, context, timestamp.
Deliverable: A working REST API that any app can POST /events to. Docker container for easy deployment.

Phase 2 — Signal scoring & taste profiling (weeks 5–9)
Turn raw events into a score per track per user, then roll that up into a taste profile.
Signal weights (starting point — tuneable):
SignalWeightListened >80% of track+1.0Repeat play+1.5Like / playlist add+2.0Skip after >50%–0.3Skip before 20%–1.2Volume increased during track+0.4Seek backward (replay a section)+0.6
These weights get multiplied by a recency decay — last week's listens matter more than last year's. This is simple math, no ML required at this stage.
Taste profile structure: For each user, maintain a vector of preferences across dimensions like energy level, tempo range, vocal vs instrumental, mood (derived from audio analysis), and artist/genre affinity. Think of it as a fingerprint that updates every time you listen.
Deliverable: GET /profile/{user_id} returns a JSON taste vector. The system can already say "this user prefers high-energy tracks in the morning and mellow ones after 9pm."

Phase 3 — Audio analysis & library indexing (weeks 10–15)
This is what makes GrooveIQ far better than pure collaborative filtering. Instead of only knowing what you've played, the system understands the audio itself.
Tool: Essentia (open source, runs locally) can extract from any audio file:

BPM, key, time signature
Energy, loudness, danceability
Mood (happy/sad/aggressive/relaxed)
Timbral features (brightness, roughness)

These get stored as a feature vector per track in the library index. Two tracks with similar vectors are "acoustically similar" — this is the backbone of content-based recommendation.
Deliverable: A background scanner that processes your library on first run and updates incrementally. GET /similar/{track_id} works immediately even for new users with no history.

Phase 4 — Recommendation engine & feed API (weeks 16–22)
The part your apps actually consume.
Three recommendation modes:

"More like this" — given a seed track, return acoustically similar tracks from your library weighted by your personal taste score. Pure content-based + collaborative.
"Radio" — a continuous queue that adapts in real-time. As you send events, the queue re-ranks itself. Skip twice in a row → the system steers away from that energy/mood zone.
"Discover" — a weekly-generated playlist of tracks in your library you've played least, but that closely match your taste profile. Surfaces forgotten gems.

API shape your apps consume:
GET /feed/{user_id}?mode=radio&seed_track=abc123&limit=20
Returns a ranked list of track_ids (using your library's existing IDs) with confidence scores. Apps don't need to understand the algorithm — they just get a sorted list and play it.
Deliverable: Full feed API, documented with OpenAPI spec so any developer can integrate.

Phase 5 — Context awareness & smart playlists (weeks 23–30)
The refinement layer that makes recommendations feel magical.

Time-of-day profiles: The system learns you like energetic music on weekday mornings and slow music on Sunday evenings. Recommendations shift automatically.
Session mood detection: If you've been skipping everything for 10 minutes, the system detects you're in a picky mood and narrows recommendations to only high-confidence tracks.
"Decade radio," "workout mode," "focus mode" — named playlist templates that combine audio features with your taste profile.
Multi-user support: Different taste profiles per user on the same server (great for families).


Phase 6 — Dashboard & explainability (weeks 31–36)
A lightweight web UI showing:

Why a track was recommended ("played 3 similar tracks this week, high energy matches your Tuesday morning profile")
Your taste profile visualized over time
Signal tuning: let you adjust weights if the algorithm is getting something wrong


Integration path for existing apps
The goal is zero friction for app developers. GrooveIQ should work as a sidecar — apps keep using Navidrome/Jellyfin normally, and just add two things:

A webhook or background POST sending events to GrooveIQ
Replacing their "next track" logic with a call to GET /feed/...

For apps that support custom API endpoints (like Symfonium), this requires no code change on the app side at all — just configuration.

Tech stack recommendation
LayerChoiceWhyAPIPython + FastAPIFast to build, excellent async support, auto OpenAPI docsDatabaseSQLite → PostgresSQLite for small installs, easy to migrateAudio analysisEssentiaOpen source, no cloud dependency, runs on CPUSimilarity searchFAISS (Facebook AI)Extremely fast nearest-neighbor search, runs locallyPackagingDocker + docker-composeStandard for self-hosting communitySchedulerAPScheduler (built-in)No extra dependencies for background jobs

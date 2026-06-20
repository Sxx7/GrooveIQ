"""
GrooveIQ – Application configuration.

All values can be overridden via environment variables (or a .env file).
See docs/configuration.md for full reference.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_MIN_API_KEY_LENGTH = 32  # 32 chars ≈ 192 bits of entropy (token_urlsafe)


def _split_csv(v: str) -> list[str]:
    """Split a comma-separated string into a list, stripping whitespace."""
    return [item.strip() for item in v.split(",") if item.strip()]


def _validate_service_url(url: str, name: str) -> None:
    """Validate that a service URL has a safe scheme and hostname.

    Rejects URLs with no scheme, non-HTTP(S) schemes, missing hostnames,
    and embedded credentials (``user:pass@host``).
    """
    if not url:
        return
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"{name} has invalid scheme '{parsed.scheme}'. Only http:// and https:// are allowed.")
    if not parsed.hostname:
        raise ValueError(f"{name} is missing a hostname.")
    if parsed.username or parsed.password:
        raise ValueError(
            f"{name} must not contain embedded credentials. Use dedicated config fields for authentication."
        )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------
    # Core
    # ------------------------------------------------------------------
    APP_ENV: str = "production"  # development | production
    SECRET_KEY: str = ""  # REQUIRED in production — generate with: openssl rand -base64 32
    ENABLE_DOCS: bool = False  # set True in development only

    # ------------------------------------------------------------------
    # Database
    # ------------------------------------------------------------------
    # SQLite (default, zero-config)
    DATABASE_URL: str = "sqlite+aiosqlite:///./grooveiq.db"
    # Postgres example: postgresql+asyncpg://user:pass@localhost/grooveiq

    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10
    DB_ECHO: bool = False  # Log all SQL statements (including params) — use with care

    # ------------------------------------------------------------------
    # Security
    # ------------------------------------------------------------------
    # Comma-separated list of API keys (hash-compared, never stored plain)
    # Generate with: openssl rand -base64 32
    # Stored as a raw string to avoid pydantic-settings JSON parsing;
    # split into a list in the model_validator below.
    API_KEYS: str = ""

    # Explicitly disable authentication (development only).
    # Must be set to true AND API_KEYS left empty for auth to be skipped.
    # Ignored when APP_ENV=production.
    DISABLE_AUTH: bool = False

    # Rate limiting (requests per minute per API key)
    RATE_LIMIT_EVENTS: int = 300  # event ingestion endpoint (high for batch clients)
    RATE_LIMIT_DEFAULT: int = 200  # all other endpoints (dashboard polls every 2s during scans)

    # Optional Redis URL for cross-process rate limiting.
    # When set, rate limits are shared across all workers/replicas.
    # When empty, falls back to in-process sliding-window counters.
    # Example: redis://localhost:6379/0
    REDIS_URL: str = ""

    # Per-user API key authorization (optional).
    # When set, each API key is bound to specific user_id(s).
    # Format:  "key1:alice,bob;key2:charlie"
    # Keys not listed here remain unrestricted (can access all users).
    # When empty, all keys can access all users (default).
    API_KEY_USERS: str = ""

    # Admin API keys (optional, comma-separated).
    # Only these keys can trigger pipeline runs, resets, library syncs/scans,
    # and view aggregate stats that span all users.
    # When empty, all authenticated keys have admin privileges (backwards-compatible).
    ADMIN_API_KEYS: str = ""

    # Hosts allowed to reach this service (guards against Host header attacks).
    # Comma-separated.  Example: "grooveiq.yourdomain.com,localhost"
    # Defaults to localhost only — set to your actual domain before exposing.
    ALLOWED_HOSTS: str = "localhost,127.0.0.1"

    # CORS – allowed origins (comma-separated).
    # Empty string means "same-origin only" (no CORS headers sent).
    # Set to your frontend origin(s) if the dashboard runs on a different domain.
    CORS_ORIGINS: str = ""

    # ------------------------------------------------------------------
    # Audio analysis (Phase 3)
    # ------------------------------------------------------------------
    MUSIC_LIBRARY_PATH: str = "/music"
    ANALYSIS_WORKERS: int = max(1, (os.cpu_count() or 2) - 1)  # default: CPU cores - 1
    ANALYSIS_BATCH_SIZE: int = 50  # tracks per job batch
    ANALYSIS_TIMEOUT: int = 300  # seconds before a single file analysis is killed
    RESCAN_INTERVAL_HOURS: int = 6  # how often to check for new files
    ANALYSIS_GPU: bool = False  # use ONNX Runtime GPU for TF enrichment pass
    ANALYSIS_GPU_BACKEND: str = ""  # "cuda", "openvino", or "" (auto-detect)
    ANALYSIS_GPU_BATCH_SIZE: int = 64  # mel-spec patches per GPU forward pass
    ANALYSIS_GPU_WORKERS: int = 1  # workers for GPU inference (usually 1)
    ANALYSIS_ONNX_INTRA_THREADS: int = 2  # ONNX intra_op_num_threads per worker
    ANALYSIS_ONNX_INTER_THREADS: int = 1  # ONNX inter_op_num_threads per worker
    ANALYSIS_OMP_THREADS: int = 2  # OMP/BLAS thread count per worker subprocess

    # ------------------------------------------------------------------
    # CLAP text-audio joint embeddings (optional, off by default)
    #
    # When enabled, each analysed track gets a second 512-dim embedding in
    # the CLAP audio/text joint space. This unlocks natural-language track
    # search ("melancholic rainy-night jazz"), text-seeded playlists/radio,
    # and text filters on recommendations.
    #
    # Models are auto-downloaded on first start from the `Xenova/larger_clap_
    # music_and_speech` Hugging Face repo (pre-exported ONNX of LAION-CLAP's
    # `larger_clap_music_and_speech` variant). Total ~395 MB on first start,
    # cached in the named `grooveiq_data` volume so subsequent restarts are
    # instant. To use a different variant, override the three URL settings
    # below; to skip the download (air-gapped install), pre-place the files
    # in CLAP_MODEL_DIR before starting.
    # ------------------------------------------------------------------
    CLAP_ENABLED: bool = False
    CLAP_MODEL_DIR: str = "/data/models/clap"  # where clap_audio.onnx, clap_text.onnx, tokenizer.json live
    CLAP_AUDIO_MODEL_FILE: str = "clap_audio.onnx"
    CLAP_TEXT_MODEL_FILE: str = "clap_text.onnx"
    CLAP_TOKENIZER_FILE: str = "clap_tokenizer.json"  # HF tokenizers JSON (RoBERTa BPE)
    CLAP_EMBEDDING_DIM: int = 512
    CLAP_AUDIO_SR: int = 48000  # LAION-CLAP expects 48 kHz mono
    CLAP_AUDIO_CLIP_SECONDS: float = 10.0  # length of central slice fed to the audio encoder

    # Auto-download URLs (issue #91). fp32 weights — larger than fp16 but
    # avoid an ONNX Runtime crash on model load: the fp16 text export
    # interacts badly with the SimplifiedLayerNormFusion graph optimisation
    # ("InsertedPrecisionFreeCast" node mismatch). Operators wanting
    # smaller weights can override these to *_fp16.onnx and lower the ORT
    # optimisation level in clap_text.py.
    CLAP_TEXT_MODEL_URL: str = (
        "https://huggingface.co/Xenova/larger_clap_music_and_speech/resolve/main/onnx/text_model.onnx"
    )
    CLAP_AUDIO_MODEL_URL: str = (
        "https://huggingface.co/Xenova/larger_clap_music_and_speech/resolve/main/onnx/audio_model.onnx"
    )
    CLAP_TOKENIZER_URL: str = "https://huggingface.co/Xenova/larger_clap_music_and_speech/resolve/main/tokenizer.json"

    # ------------------------------------------------------------------
    # Lyrics acquisition (optional, off by default)
    #
    # Lyrics are acquired through a priority cascade — real sources first,
    # machine transcription only to fill genuine gaps:
    #   tier 1  embedded tags (USLT/SYLT, Vorbis LYRICS, MP4 ©lyr) — in-scan, ~free
    #   tier 2  LRCLIB (lrclib.net, free, no key, returns synced LRC)
    #   tier 3  ASR fallback (faster-whisper) on a GPU-VM sidecar — gated
    # Display needs real lyrics (tiers 1-2); the algorithm tolerates ASR text.
    # Everything degrades gracefully: no tag -> LRCLIB -> ASR -> none.
    # The scan/ranker/recommend paths are unchanged when LYRICS_ENABLED=false.
    # ------------------------------------------------------------------
    LYRICS_ENABLED: bool = False  # master switch (drain + scan-time embedded read)
    LYRICS_LRCLIB_ENABLED: bool = True  # tier-2 online lookups
    LYRICS_LRCLIB_URL: str = "https://lrclib.net"  # point at a self-hosted mirror later (Phase E)
    LYRICS_LRCLIB_USER_AGENT: str = ""  # LRCLIB asks for an identifying UA; see lyrics_lrclib_user_agent
    LYRICS_ASR_ENABLED: bool = False  # tier-3 (needs the GPU sidecar)
    LYRICS_API_URL: str = ""  # lyrics-api sidecar base URL, e.g. http://<gpu-vm>:8300
    LYRICS_API_MUSIC_PATH: str = ""  # path-map if the VM's /music mount differs from MUSIC_LIBRARY_PATH
    LYRICS_API_TIMEOUT_S: float = 600.0  # per-track ASR HTTP timeout (queue + transcribe headroom)
    LYRICS_ASR_INSTRUMENTAL_MAX: float = 0.5  # skip ASR when instrumentalness >= this (pilot-calibrated)
    LYRICS_ASR_MODEL: str = "large-v3"  # informational; the sidecar owns the model (LYRICS_MODEL env there)
    # Silero VAD is trained on *speech* and over-filters *sung* vocals — the
    # pilot measured ~50% recall loss on clearly-vocal tracks with it on. The
    # instrumentalness gate above already blocks ASR on instrumentals (the
    # pilot saw 0% hallucination), so VAD is off by default; set true to trade
    # recall for extra hallucination safety on voiced tracks with long
    # instrumental passages.
    LYRICS_ASR_VAD: bool = False
    # Drain pacing / retry (env-only; promote to a versioned LyricsConfig + GUI later)
    LYRICS_DRAIN_MAX_PER_HOUR: int = 0  # 0 = unthrottled ASR; raise to pace a shared GPU VM
    LYRICS_DRAIN_BATCH_SIZE: int = 100  # tracks examined per tick (cheap tiers; ASR is paced separately)
    LYRICS_DRAIN_POLL_MINUTES: int = 5  # tick cadence
    LYRICS_DRAIN_COOLDOWN_HOURS: float = 24.0  # base retry cooldown for no_lyrics/failed rows
    LYRICS_DRAIN_MAX_ATTEMPTS: int = 3  # then permanently_skipped
    LYRICS_DRAIN_BACKOFF_MULTIPLIER: float = 2.0  # cooldown * m^(attempt-1), capped at 30 days
    # Phase D (deferred) — torch-free ONNX lyrics text embedding
    LYRICS_EMBED_ENABLED: bool = False
    LYRICS_EMBED_MODEL_DIR: str = "/data/models/lyrics"

    # Supported audio extensions (comma-separated)
    AUDIO_EXTENSIONS: str = ".mp3,.flac,.ogg,.m4a,.wav,.aac,.opus,.wv"

    # ------------------------------------------------------------------
    # Recommendation engine (Phase 2)
    # ------------------------------------------------------------------
    SESSION_GAP_MINUTES: int = 30  # inactivity gap that splits sessions
    SESSION_MIN_EVENTS: int = 2  # ignore sessions with fewer events
    TASTE_PROFILE_DECAY_DAYS: float = 30.0  # half-life for exponential recency weighting
    SCORING_INTERVAL_HOURS: int = 1  # how often to run the scoring/sessionizer worker

    # ------------------------------------------------------------------
    # Event ingestion
    # ------------------------------------------------------------------
    # Maximum events per batch POST
    EVENT_BATCH_MAX: int = 50

    # How long (days) to retain raw events before aggregating
    EVENT_RETENTION_DAYS: int = 365

    # Minimum play percentage to count as a "real" listen (not accidental)
    MIN_PLAY_PERCENTAGE: float = 0.05

    # ------------------------------------------------------------------
    # Media server integration (Navidrome / Plex)
    # ------------------------------------------------------------------
    # Set MEDIA_SERVER_TYPE to "navidrome" or "plex" to enable.
    # When enabled, the library sync maps track_ids to the media server's
    # native IDs so events from clients use the same identifiers.
    MEDIA_SERVER_TYPE: str = ""  # "navidrome" or "plex" (empty = disabled)
    MEDIA_SERVER_URL: str = ""  # e.g. http://192.168.178.49:4533
    MEDIA_SERVER_USER: str = ""  # Navidrome username
    MEDIA_SERVER_PASSWORD: str = ""  # Navidrome password (plaintext or Fernet-encrypted)
    MEDIA_SERVER_TOKEN: str = ""  # Plex X-Plex-Token (plaintext or Fernet-encrypted)
    MEDIA_SERVER_LIBRARY_ID: str = "1"  # Plex library section ID
    MEDIA_SERVER_MUSIC_PATH: str = ""  # Music root as seen by the media server
    # (for path matching if it differs from MUSIC_LIBRARY_PATH)

    # Fernet key for encrypting media server credentials at rest.
    # Generate with: openssl rand -base64 32
    # When set, MEDIA_SERVER_PASSWORD and MEDIA_SERVER_TOKEN are expected
    # to be Fernet-encrypted.
    CREDENTIAL_ENCRYPTION_KEY: str = ""

    # ------------------------------------------------------------------
    # Music discovery (Last.fm + Lidarr)
    # ------------------------------------------------------------------
    LASTFM_API_KEY: str = ""
    LASTFM_API_SECRET: str = ""  # shared secret for authenticated calls (scrobbling)
    LIDARR_URL: str = ""  # e.g. http://lidarr:8686
    LIDARR_API_KEY: str = ""
    LIDARR_QUALITY_PROFILE_ID: int = 1
    LIDARR_METADATA_PROFILE_ID: int = 1
    LIDARR_ROOT_FOLDER: str = "/music"
    DISCOVERY_CRON: str = "0 3 * * *"  # cron schedule (default: 3 AM daily)
    DISCOVERY_MAX_REQUESTS_PER_DAY: int = 500
    DISCOVERY_SIMILAR_LIMIT: int = 20  # similar artists per seed from Last.fm

    # ------------------------------------------------------------------
    # Charts (Last.fm)
    # ------------------------------------------------------------------
    CHARTS_ENABLED: bool = False  # master toggle for periodic chart builds
    CHARTS_CRON: str = "0 3 * * *"  # daily build schedule (default 3 AM UTC). Wall-clock cron so restarts don't defer it.
    CHARTS_INTERVAL_HOURS: int = 24  # freshness window (hours) for the Monitor staleness banner; build cadence is CHARTS_CRON
    CHARTS_TOP_LIMIT: int = 100  # entries per chart (max 200)
    CHARTS_TAGS: str = ""  # comma-separated genre tags, e.g. "rock,electronic,hip-hop"
    CHARTS_COUNTRIES: str = ""  # comma-separated country names, e.g. "germany,united states"
    CHARTS_LIDARR_AUTO_ADD: bool = False  # auto-add chart artists to Lidarr
    CHARTS_LIDARR_MAX_ADDS: int = 50  # max artists to add to Lidarr per build

    # ------------------------------------------------------------------
    # Downloads — spotdl-api, streamrip-api, or Spotizerr (legacy)
    # ------------------------------------------------------------------
    # Toggle: which download backend to use by default.
    # Values: "spotdl" (default), "streamrip", "spotizerr"
    # Only relevant when multiple backends are configured.
    DEFAULT_DOWNLOAD_CLIENT: str = "spotdl"

    # spotdl-api: lightweight REST wrapper around spotDL (YouTube Music audio)
    SPOTDL_API_URL: str = ""  # e.g. http://spotdl-api:8181

    # streamrip-api: REST wrapper around streamrip (Qobuz/Tidal/Deezer lossless)
    STREAMRIP_API_URL: str = ""  # e.g. http://streamrip-api:8282

    # Spotizerr (legacy, librespot-based — kept for backwards compat)
    SPOTIZERR_URL: str = ""  # e.g. http://spotizerr:7171
    SPOTIZERR_USERNAME: str = ""  # only needed if Spotizerr ENABLE_AUTH=true
    SPOTIZERR_PASSWORD: str = ""  # only needed if Spotizerr ENABLE_AUTH=true
    CHARTS_SPOTIZERR_AUTO_ADD: bool = False  # auto-download unmatched chart tracks
    CHARTS_SPOTIZERR_MAX_ADDS: int = 50  # max tracks to download per chart build

    # ------------------------------------------------------------------
    # slskd (Soulseek) — optional peer-to-peer download backend
    # ------------------------------------------------------------------
    SLSKD_URL: str = ""  # e.g. http://slskd:5030
    SLSKD_API_KEY: str = ""  # slskd API key (generate in slskd web UI or via --api-key)
    SLSKD_ENABLED: bool = False  # master toggle
    SLSKD_SEARCH_TIMEOUT: int = 50  # seconds to wait for Soulseek search results; must exceed slskd's ~45s internal search timeout, else results are abandoned before peers respond
    SLSKD_PREFER_LOSSLESS: bool = True  # prefer FLAC over MP3 in result ranking

    # ------------------------------------------------------------------
    # AcousticBrainz Lookup (optional add-on container)
    # ------------------------------------------------------------------
    AB_LOOKUP_URL: str = ""  # e.g. http://acousticbrainz-lookup:8200
    AB_LOOKUP_ENABLED: bool = False
    AB_DISCOVERY_LIMIT: int = 50

    # ------------------------------------------------------------------
    # Fill Library (AB taste-match → Lidarr album download)
    # ------------------------------------------------------------------
    FILL_LIBRARY_ENABLED: bool = False
    FILL_LIBRARY_MAX_ALBUMS: int = 20  # max albums added per run
    FILL_LIBRARY_MAX_DISTANCE: float = 0.15  # max AB distance (lower = stricter)
    FILL_LIBRARY_CRON: str = "0 4 * * *"  # default: 4 AM daily
    FILL_LIBRARY_QUERY_LIMIT: int = 500  # max results per AB query

    # ------------------------------------------------------------------
    # Last.fm per-user integration (profile + scrobbling)
    # ------------------------------------------------------------------
    LASTFM_ENABLED: bool = False  # master toggle
    LASTFM_SCROBBLE_ENABLED: bool = False  # scrobbling (requires session key)
    LASTFM_SESSION_ENCRYPTION_KEY: str = ""  # Fernet key for encrypting session keys at rest
    LASTFM_REFRESH_HOURS: int = 6  # how often to pull Last.fm profiles

    # ------------------------------------------------------------------
    # Recommendation audit & replay (always-on persistence)
    # ------------------------------------------------------------------
    RECO_AUDIT_ENABLED: bool = True  # master switch — disable to skip audit writes entirely
    RECO_AUDIT_RETENTION_DAYS: int = 90  # auto-purge audits older than this
    RECO_AUDIT_MAX_CANDIDATES: int = 200  # cap candidates persisted per request (top-N by raw_score)

    # ------------------------------------------------------------------
    # Per-dial evaluation metrics (discovery-dial novelty/coverage/diversity)
    # ------------------------------------------------------------------
    RECO_DIAL_EVAL_ENABLED: bool = True  # lazily refresh per-dial metrics on the model-stats endpoint
    RECO_DIAL_EVAL_MAX_USERS: int = 8  # cap sample users per dial-mode evaluation (bounds the work)
    RECO_DIAL_EVAL_LIMIT: int = 25  # recommendation list length measured per dial bucket
    RECO_DIAL_EVAL_TTL_MINUTES: int = 60  # reuse the cached dial-mode metrics within this window

    # ------------------------------------------------------------------
    # Mix cache (stale-while-revalidate cache for recommendation-mode requests)
    # ------------------------------------------------------------------
    MIX_CACHE_ENABLED: bool = True  # serve cacheable recommend/mode requests from the SWR cache
    MIX_CACHE_FRESH_SECONDS: int = 120  # within this age the cached mix is served as-is (no rebuild)
    MIX_CACHE_STALE_SECONDS: int = 900  # beyond fresh but within this grace: serve stale + one bg rebuild
    MIX_CACHE_MAX_ENTRIES: int = 512  # cap on total cached mixes (oldest evicted first — memory bound)
    MIX_CACHE_MAX_CONCURRENT_REBUILDS: int = 4  # semaphore cap on background/get_or_build regenerations
    MIX_PREWARM_RATE_LIMIT_PER_MIN: int = 12  # per (api-key, user) cap on prewarm calls
    MIX_PREWARM_MAX_MODES: int = 8  # cap modes warmed per prewarm request (bounds background fan-out)

    # ------------------------------------------------------------------
    # API call logging (per-user HTTP request/response history for the dashboard)
    # ------------------------------------------------------------------
    API_LOG_ENABLED: bool = True  # master switch — disable to skip middleware writes entirely
    API_LOG_RETENTION_DAYS: int = 7  # auto-purge log rows older than this
    API_LOG_INCLUDE_EVENTS: bool = True  # log POST /v1/events (high volume — turn off to save space)
    API_LOG_MAX_BODY_BYTES: int = 4096  # cap captured request/response body size
    API_LOG_MAX_LIST_ITEMS: int = 20  # for list-shaped responses, keep first N entries

    # ------------------------------------------------------------------
    # user_id format enforcement (issue #86)
    # ------------------------------------------------------------------
    # Plex media-server backend is no longer supported. Navidrome is the
    # canonical source of identity. The default regex covers Navidrome's
    # xid (20 chars) and nanoid (21–22 chars) output across versions.
    # Override for non-Navidrome backends or stricter setups.
    USER_ID_PATTERN: str = r"^[A-Za-z0-9]{20,22}$"

    # ------------------------------------------------------------------
    # Personalized news feed (Reddit-sourced)
    # ------------------------------------------------------------------
    NEWS_ENABLED: bool = False
    NEWS_INTERVAL_MINUTES: int = 30
    NEWS_MAX_AGE_HOURS: int = 48
    NEWS_DEFAULT_SUBREDDITS: str = "Music,hiphopheads,indieheads,electronicmusic,popheads,metal,rnb"
    NEWS_MAX_POSTS_PER_SUB: int = 50

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = True  # structured JSON logs for prod; False for dev

    # ------------------------------------------------------------------
    # Parsed list accessors (derived from the raw CSV strings above)
    # ------------------------------------------------------------------
    @property
    def api_keys_list(self) -> list[str]:
        return _split_csv(self.API_KEYS)

    @property
    def allowed_hosts_list(self) -> list[str]:
        return _split_csv(self.ALLOWED_HOSTS)

    @property
    def cors_origins_list(self) -> list[str]:
        return _split_csv(self.CORS_ORIGINS)

    @property
    def audio_extensions_list(self) -> list[str]:
        return _split_csv(self.AUDIO_EXTENSIONS)

    @property
    def charts_tags_list(self) -> list[str]:
        return _split_csv(self.CHARTS_TAGS)

    @property
    def charts_countries_list(self) -> list[str]:
        return _split_csv(self.CHARTS_COUNTRIES)

    @property
    def charts_enabled(self) -> bool:
        return bool(self.CHARTS_ENABLED and self.LASTFM_API_KEY)

    @property
    def charts_schedule_label(self) -> str:
        """Friendly label for CHARTS_CRON (handles the common daily case)."""
        parts = self.CHARTS_CRON.split()
        if len(parts) == 5:
            minute, hour, dom, month, dow = parts
            if dom == "*" and month == "*" and dow == "*" and minute.isdigit() and hour.isdigit():
                return f"daily {int(hour):02d}:{int(minute):02d} UTC"
        return self.CHARTS_CRON

    @property
    def spotdl_enabled(self) -> bool:
        return bool(self.SPOTDL_API_URL)

    @property
    def streamrip_enabled(self) -> bool:
        return bool(self.STREAMRIP_API_URL)

    @property
    def spotizerr_enabled(self) -> bool:
        return bool(self.SPOTIZERR_URL)

    @property
    def slskd_enabled(self) -> bool:
        return bool(self.SLSKD_ENABLED and self.SLSKD_URL and self.SLSKD_API_KEY)

    @property
    def download_enabled(self) -> bool:
        """True if any download backend (spotdl-api, streamrip-api, Spotizerr, or slskd) is configured."""
        return self.spotdl_enabled or self.streamrip_enabled or self.spotizerr_enabled or self.slskd_enabled

    @property
    def ab_lookup_enabled(self) -> bool:
        return bool(self.AB_LOOKUP_ENABLED and self.AB_LOOKUP_URL)

    @property
    def fill_library_enabled(self) -> bool:
        return bool(self.FILL_LIBRARY_ENABLED and self.AB_LOOKUP_URL and self.LIDARR_URL and self.LIDARR_API_KEY)

    @property
    def news_enabled(self) -> bool:
        return bool(self.NEWS_ENABLED)

    @property
    def news_subreddits_list(self) -> list[str]:
        return _split_csv(self.NEWS_DEFAULT_SUBREDDITS)

    @property
    def lyrics_enabled(self) -> bool:
        return bool(self.LYRICS_ENABLED)

    @property
    def lyrics_lrclib_enabled(self) -> bool:
        """Tier-2 LRCLIB lookups are live only when the feature is enabled."""
        return bool(self.LYRICS_ENABLED and self.LYRICS_LRCLIB_ENABLED)

    @property
    def lyrics_asr_enabled(self) -> bool:
        """Tier-3 ASR needs the feature on, the tier enabled, and a sidecar URL."""
        return bool(self.LYRICS_ENABLED and self.LYRICS_ASR_ENABLED and self.LYRICS_API_URL)

    @property
    def lyrics_lrclib_user_agent(self) -> str:
        """LRCLIB asks callers to identify themselves; fall back to a sane default."""
        return self.LYRICS_LRCLIB_USER_AGENT or "GrooveIQ (+https://github.com/Sxx7/GrooveIQ)"

    @property
    def discovery_enabled(self) -> bool:
        return bool(self.LASTFM_API_KEY and self.LIDARR_URL and self.LIDARR_API_KEY)

    @property
    def lastfm_user_enabled(self) -> bool:
        """True when per-user Last.fm features (profile pull, scrobbling) are configured."""
        return bool(self.LASTFM_ENABLED and self.LASTFM_API_KEY and self.LASTFM_API_SECRET)

    @model_validator(mode="after")
    def validate_security_settings(self) -> Settings:
        """Enforce security requirements.

        Production: API_KEYS is mandatory and each key must be at least
        ``_MIN_API_KEY_LENGTH`` characters (use ``openssl rand -base64 32``
        to generate a strong key).
        Development: empty API_KEYS is allowed (all endpoints unprotected)
        but weak keys are still rejected.
        """
        import sys
        import warnings

        is_prod = self.APP_ENV == "production"

        # --- SECRET_KEY enforcement ---
        _placeholder_prefixes = ("CHANGE_ME", "changeme", "replace", "TODO", "FIXME")
        if is_prod and (not self.SECRET_KEY or any(self.SECRET_KEY.startswith(p) for p in _placeholder_prefixes)):
            print(
                "\n❌  FATAL: No SECRET_KEY configured (or placeholder detected) "
                "and APP_ENV=production.\n"
                "   Generate one:  openssl rand -base64 32\n"
                "   Then set SECRET_KEY in your .env file.\n",
                file=sys.stderr,
            )
            raise SystemExit(1)

        # --- API key enforcement ---
        if is_prod and not self.api_keys_list:
            print(
                "\n❌  FATAL: No API_KEYS configured and APP_ENV=production.\n"
                "   Generate a key:  openssl rand -base64 32\n"
                "   Then set API_KEYS in your .env file.\n",
                file=sys.stderr,
            )
            raise SystemExit(1)

        if not is_prod and not self.api_keys_list:
            if not self.DISABLE_AUTH:
                print(
                    "\n❌  FATAL: No API_KEYS configured.\n"
                    "   Either set API_KEYS in your .env file, or explicitly\n"
                    "   set DISABLE_AUTH=true to run without authentication.\n",
                    file=sys.stderr,
                )
                raise SystemExit(1)
            import logging as _logging

            _logging.getLogger("grooveiq.security").warning(
                "Authentication is DISABLED (DISABLE_AUTH=true, no API_KEYS). "
                "All endpoints are open. Do NOT expose this instance to a network."
            )
            warnings.warn(
                "⚠️  Authentication is DISABLED (DISABLE_AUTH=true, no API_KEYS). "
                "All endpoints are open. Do NOT expose this instance to a network.",
                stacklevel=2,
            )

        for key in self.api_keys_list:
            if len(key) < _MIN_API_KEY_LENGTH:
                print(
                    f"\n❌  FATAL: API key is too short ({len(key)} chars, "
                    f"minimum {_MIN_API_KEY_LENGTH}).\n"
                    "   Generate a strong key:  openssl rand -base64 32\n",
                    file=sys.stderr,
                )
                raise SystemExit(1)

        # --- Host / CORS warnings ---
        if is_prod:
            if self.allowed_hosts_list == ["*"]:
                warnings.warn(
                    "⚠️  ALLOWED_HOSTS is set to '*'. Set ALLOWED_HOSTS to your actual domain for security.",
                    stacklevel=2,
                )
            if self.cors_origins_list == ["*"]:
                warnings.warn(
                    "⚠️  CORS_ORIGINS is set to '*'. Set CORS_ORIGINS to your actual frontend origin(s).",
                    stacklevel=2,
                )

        # --- Service URL validation (SSRF prevention) ---
        _validate_service_url(self.MEDIA_SERVER_URL, "MEDIA_SERVER_URL")
        _validate_service_url(self.LIDARR_URL, "LIDARR_URL")
        _validate_service_url(self.SPOTDL_API_URL, "SPOTDL_API_URL")
        _validate_service_url(self.STREAMRIP_API_URL, "STREAMRIP_API_URL")
        _validate_service_url(self.SPOTIZERR_URL, "SPOTIZERR_URL")
        _validate_service_url(self.SLSKD_URL, "SLSKD_URL")
        _validate_service_url(self.AB_LOOKUP_URL, "AB_LOOKUP_URL")

        # --- HTTP cleartext warnings ---
        if self.MEDIA_SERVER_URL and self.MEDIA_SERVER_URL.startswith("http://"):
            warnings.warn(
                "⚠️  MEDIA_SERVER_URL uses plain HTTP. Credentials will be "
                "transmitted in cleartext. Use HTTPS if possible.",
                stacklevel=2,
            )
        if self.LIDARR_URL and self.LIDARR_URL.startswith("http://"):
            warnings.warn(
                "⚠️  LIDARR_URL uses plain HTTP. API key will be transmitted in cleartext. Use HTTPS if possible.",
                stacklevel=2,
            )
        if self.SPOTIZERR_URL and self.SPOTIZERR_URL.startswith("http://"):
            warnings.warn(
                "⚠️  SPOTIZERR_URL uses plain HTTP. Credentials will be "
                "transmitted in cleartext. Use HTTPS if possible.",
                stacklevel=2,
            )
        return self


settings = Settings()

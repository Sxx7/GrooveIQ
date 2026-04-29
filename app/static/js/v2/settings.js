/* settings.js — Settings bucket pages.
 * Session 03: Algorithm wired to the versioned-config shell.
 * Sessions 04: Download Routing, Lidarr Backfill, Connections, Users, Onboarding.
 */

(function () {
    GIQ.pages.settings = GIQ.pages.settings || {};

    const STUBS = ['download-routing', 'lidarr-backfill', 'connections', 'users', 'onboarding'];
    for (const sp of STUBS) {
        GIQ.pages.settings[sp] = function (root) {
            const label = GIQ.router.SUBPAGE_LABELS[sp] || sp;
            root.innerHTML = '<div class="page-stub">'
                + '<div class="eyebrow">SETTINGS</div>'
                + '<h1>' + GIQ.fmt.esc(label) + '</h1>'
                + '<p class="muted">Page: settings → ' + GIQ.fmt.esc(sp) + ' — TBD (sessions 04+)</p>'
                + '</div>';
        };
    }

    /* ---- Algorithm field metadata (ported from old app.js) ---------- */
    /* Each field: { desc, min, max, step, integer? }. The [RETRAIN] suffix in
     * descriptions is detected by the shell to draw per-field RETRAIN badges.
     */
    const ALGO_FIELD_META = {
        track_scoring: {
            w_full_listen: { desc: 'Weight for a full listen (completion ≥ 0.8 or dwell ≥ 30s)', min: -10, max: 10, step: 0.1 },
            w_mid_listen: { desc: 'Weight for a mid-length listen (2s–30s dwell)', min: -10, max: 10, step: 0.1 },
            w_early_skip: { desc: 'Default weight for an early skip (<2s dwell)', min: -10, max: 10, step: 0.1 },
            w_early_skip_playlist: { desc: 'Early skip weight in playlist/album context', min: -10, max: 10, step: 0.1 },
            w_early_skip_radio: { desc: 'Early skip weight in radio/search context', min: -10, max: 10, step: 0.1 },
            w_like: { desc: 'Weight for an explicit like', min: -10, max: 10, step: 0.1 },
            w_dislike: { desc: 'Weight for an explicit dislike', min: -10, max: 10, step: 0.1 },
            w_repeat: { desc: 'Weight for a repeat action', min: -10, max: 10, step: 0.1 },
            w_playlist_add: { desc: 'Weight for adding the track to a playlist', min: -10, max: 10, step: 0.1 },
            w_queue_add: { desc: 'Weight for adding the track to the queue', min: -10, max: 10, step: 0.1 },
            w_heavy_seek: { desc: 'Penalty per excess seek above threshold', min: -10, max: 10, step: 0.1 },
            early_skip_ms: { desc: 'Milliseconds threshold for early skip classification', min: 100, max: 30000, step: 100, integer: true },
            mid_skip_ms: { desc: 'Milliseconds threshold for mid-skip classification', min: 1000, max: 120000, step: 500, integer: true },
            heavy_seek_threshold: { desc: 'Seeks per play above which heavy-seek penalty applies', min: 1, max: 20, step: 1, integer: true },
        },
        reranker: {
            artist_diversity_top_n: { desc: 'Number of top positions to enforce artist diversity in', min: 1, max: 100, step: 1, integer: true },
            artist_max_per_top: { desc: 'Max tracks from same artist in top N', min: 1, max: 20, step: 1, integer: true },
            repeat_window_hours: { desc: 'Hours to suppress recently played tracks', min: 0, max: 168, step: 0.5 },
            freshness_boost: { desc: 'Score multiplier boost for never-played tracks', min: 0, max: 1, step: 0.01 },
            skip_threshold: { desc: 'Early skip count above which skip suppression activates', min: 1, max: 50, step: 1, integer: true },
            skip_demote_factor: { desc: 'Score multiplier for skip-suppressed tracks', min: 0, max: 1, step: 0.05 },
            exploration_fraction: { desc: 'Fraction of slots reserved for under-explored tracks', min: 0, max: 0.5, step: 0.01 },
            exploration_low_plays: { desc: 'Play count below which a track is considered under-explored', min: 1, max: 50, step: 1, integer: true },
            exploration_noise_scale: { desc: 'Noise magnitude for exploration scoring', min: 0, max: 2, step: 0.05 },
            min_duration_car: { desc: 'Min track duration (seconds) in car/speaker mode', min: 0, max: 600, step: 5 },
        },
        candidate_sources: {
            content: { desc: 'FAISS content-based similarity (from seed track)', min: 0, max: 5, step: 0.1 },
            content_profile: { desc: 'FAISS similarity from user taste centroid', min: 0, max: 5, step: 0.1 },
            cf: { desc: 'Collaborative filtering', min: 0, max: 5, step: 0.1 },
            session_skipgram: { desc: 'Session skip-gram behavioural co-occurrence', min: 0, max: 5, step: 0.1 },
            lastfm_similar: { desc: 'Last.fm similar tracks (external CF)', min: 0, max: 5, step: 0.1 },
            sasrec: { desc: 'SASRec transformer next-track prediction', min: 0, max: 5, step: 0.1 },
            popular: { desc: 'Global popularity fallback', min: 0, max: 5, step: 0.1 },
            artist_recall: { desc: 'Recently heard artist tracks', min: 0, max: 5, step: 0.1 },
        },
        taste_profile: {
            timescale_short_days: { desc: 'Short-term taste window (days)', min: 1, max: 90, step: 1 },
            timescale_long_days: { desc: 'Long-term taste window (days)', min: 30, max: 3650, step: 10 },
            top_tracks_limit: { desc: 'Top tracks retained in taste profile', min: 10, max: 500, step: 10, integer: true },
            lastfm_decay_interactions: { desc: 'Interactions at which Last.fm weight reaches ~37%', min: 10, max: 1000, step: 10 },
            onboarding_decay_interactions: { desc: 'Interactions at which onboarding weight reaches ~37%', min: 10, max: 500, step: 10 },
            enrichment_min_weight: { desc: 'Min weight below which enrichment is skipped', min: 0.001, max: 0.5, step: 0.005 },
        },
        ranker: {
            n_estimators: { desc: 'Number of boosting rounds (trees) [RETRAIN]', min: 10, max: 2000, step: 10, integer: true },
            max_depth: { desc: 'Maximum tree depth [RETRAIN]', min: 2, max: 20, step: 1, integer: true },
            learning_rate: { desc: 'Boosting learning rate [RETRAIN]', min: 0.001, max: 1.0, step: 0.005 },
            num_leaves: { desc: 'Max leaves per tree [RETRAIN]', min: 4, max: 256, step: 1, integer: true },
            min_child_samples: { desc: 'Min samples per leaf [RETRAIN]', min: 1, max: 100, step: 1, integer: true },
            subsample: { desc: 'Row subsampling ratio [RETRAIN]', min: 0.1, max: 1.0, step: 0.05 },
            colsample_bytree: { desc: 'Column subsampling ratio [RETRAIN]', min: 0.1, max: 1.0, step: 0.05 },
            reg_alpha: { desc: 'L1 regularisation [RETRAIN]', min: 0, max: 10, step: 0.1 },
            reg_lambda: { desc: 'L2 regularisation [RETRAIN]', min: 0, max: 10, step: 0.1 },
            min_training_samples: { desc: 'Min samples required to train [RETRAIN]', min: 5, max: 1000, step: 5, integer: true },
            weight_disliked: { desc: 'Sample weight for disliked tracks', min: 1, max: 10, step: 0.1 },
            weight_heavy_skip: { desc: 'Sample weight for heavily skipped tracks', min: 1, max: 10, step: 0.1 },
            weight_strong_positive: { desc: 'Sample weight for liked/repeated tracks', min: 1, max: 10, step: 0.1 },
            weight_impression_negative: { desc: 'Sample weight for shown-but-not-played tracks', min: 1, max: 10, step: 0.1 },
        },
        radio: {
            seed_weight: { desc: 'How much the seed anchor influences drift embedding', min: 0, max: 1, step: 0.05 },
            feedback_weight: { desc: 'How much feedback shifts drift embedding', min: 0, max: 1, step: 0.05 },
            profile_weight: { desc: 'How much user global taste contributes', min: 0, max: 1, step: 0.05 },
            source_drift: { desc: 'Score multiplier for drift-FAISS candidates', min: 0, max: 5, step: 0.1 },
            source_seed: { desc: 'Score multiplier for seed-FAISS candidates', min: 0, max: 5, step: 0.1 },
            source_content: { desc: 'Score multiplier for content similarity candidates', min: 0, max: 5, step: 0.1 },
            source_skipgram: { desc: 'Score multiplier for session skip-gram candidates', min: 0, max: 5, step: 0.1 },
            source_lastfm: { desc: 'Score multiplier for Last.fm similar candidates', min: 0, max: 5, step: 0.1 },
            source_cf: { desc: 'Score multiplier for CF candidates', min: 0, max: 5, step: 0.1 },
            source_artist: { desc: 'Score multiplier for same-artist candidates', min: 0, max: 5, step: 0.1 },
            feedback_like_weight: { desc: 'Attraction weight when user likes a track', min: 0, max: 5, step: 0.1 },
            feedback_dislike_weight: { desc: 'Repulsion weight when user dislikes', min: 0, max: 5, step: 0.1 },
            feedback_skip_weight: { desc: 'Mild repulsion weight on skip', min: 0, max: 5, step: 0.1 },
            feedback_decay: { desc: 'Exponential decay applied to older feedback', min: 0.1, max: 1, step: 0.05 },
            session_ttl_hours: { desc: 'Hours of inactivity before session expires', min: 0.5, max: 24, step: 0.5 },
            max_sessions: { desc: 'Maximum concurrent radio sessions', min: 1, max: 500, step: 1, integer: true },
        },
        session_embeddings: {
            embedding_dim: { desc: 'Embedding vector dimensionality [RETRAIN]', min: 16, max: 512, step: 16, integer: true },
            window_size: { desc: 'Context window size (tracks before/after) [RETRAIN]', min: 1, max: 20, step: 1, integer: true },
            min_count: { desc: 'Ignore tracks appearing fewer times [RETRAIN]', min: 1, max: 50, step: 1, integer: true },
            epochs: { desc: 'Training iterations [RETRAIN]', min: 1, max: 100, step: 1, integer: true },
            min_sessions: { desc: 'Minimum sessions required to train', min: 1, max: 500, step: 1, integer: true },
            min_vocab: { desc: 'Minimum unique tracks required to train', min: 2, max: 100, step: 1, integer: true },
        },
    };

    GIQ.pages.settings.algorithm = function renderAlgorithm(root) {
        root.innerHTML = '';
        const host = document.createElement('div');
        host.className = 'vc-shell';
        root.appendChild(host);

        const shell = GIQ.components.versionedConfigShell({
            kind: 'algorithm',
            title: 'Algorithm Config',
            eyebrowPrefix: 'VERSIONED CONFIG',
            retrainGroups: ['ranker', 'session_embeddings'],
            fieldMeta: ALGO_FIELD_META,
            exportName: 'grooveiq-algorithm',
            saveSideEffect: {
                label: 'Pipeline reset triggered',
                onSave: () => GIQ.api.post('/v1/pipeline/reset'),
                jumpHash: '#/monitor/pipeline',
                jumpLabel: 'Pipeline →',
            },
        });
        shell.mount(host);

        return () => shell.dispose();
    };
})();

/* settings.js — Settings bucket pages.
 * Session 03: Algorithm wired to the versioned-config shell.
 * Session 04: Download Routing, Lidarr Backfill, Connections, Users, Onboarding.
 */

(function () {
    GIQ.pages.settings = GIQ.pages.settings || {};
    const esc = GIQ.fmt.esc;

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

    /* ===================================================================
     * Settings → Download Routing
     * =================================================================== */

    const DL_QUALITY_TIERS = [
        { value: '', label: 'No threshold' },
        { value: 'lossy_low', label: 'Lossy (≤192 kbps)' },
        { value: 'lossy_high', label: 'Lossy (256–320 kbps)' },
        { value: 'lossless', label: 'Lossless (16-bit/44.1)' },
        { value: 'hires', label: 'Hi-Res (24-bit/96+)' },
    ];

    const DL_CHAIN_KEYS = ['individual', 'bulk_per_track', 'bulk_album'];

    function dlBackendDot(name) {
        const colors = {
            spotdl: '#4ade80', streamrip: '#fbbf24', spotizerr: '#60a5fa',
            slskd: '#c084fc', lidarr: '#f87171',
        };
        const dot = document.createElement('span');
        dot.className = 'dl-backend-dot';
        dot.style.background = colors[name] || 'var(--ink-3)';
        return dot;
    }

    function dlGroupMeta(ctx, key) {
        return ctx.groupsMeta.find(g => g.key === key) || { backends_eligible: [] };
    }

    function dlRenderChainBody(ctx) {
        const wrap = document.createElement('div');
        wrap.className = 'dl-chain';
        const groupKey = ctx.groupKey;
        const meta = ctx.groupMeta || {};
        const chain = (ctx.working && ctx.working[groupKey]) || [];

        if (!chain.length) {
            const empty = document.createElement('div');
            empty.className = 'dl-chain-empty';
            empty.textContent = 'No backends in this chain. Add one below.';
            wrap.appendChild(empty);
        } else {
            chain.forEach((entry, idx) => {
                wrap.appendChild(dlRenderChainRow(ctx, groupKey, idx, entry, chain.length));
            });
        }

        const eligible = meta.backends_eligible || [];
        const existing = chain.map(e => e.backend);
        const available = eligible.filter(b => existing.indexOf(b) === -1);
        if (available.length) {
            const addRow = document.createElement('div');
            addRow.className = 'dl-chain-add';
            const sel = document.createElement('select');
            sel.className = 'vc-num';
            sel.style.width = 'auto';
            sel.style.padding = '4px 8px';
            available.forEach(b => {
                const opt = document.createElement('option');
                opt.value = b;
                opt.textContent = b;
                sel.appendChild(opt);
            });
            const addBtn = document.createElement('button');
            addBtn.type = 'button';
            addBtn.className = 'vc-btn vc-btn-ghost-sm';
            addBtn.textContent = '+ Add backend';
            addBtn.addEventListener('click', () => {
                const backend = sel.value;
                if (!backend) return;
                const defChain = (ctx.defaults && ctx.defaults[groupKey]) || [];
                const defEntry = defChain.find(e => e.backend === backend);
                const entry = defEntry
                    ? JSON.parse(JSON.stringify(defEntry))
                    : { backend, enabled: true, min_quality: null, timeout_s: 60 };
                ctx.working[groupKey].push(entry);
                ctx.refreshGroup(groupKey);
                ctx.refreshHeader();
            });
            addRow.appendChild(sel);
            addRow.appendChild(addBtn);
            wrap.appendChild(addRow);
        }

        return wrap;
    }

    function dlRenderChainRow(ctx, groupKey, idx, entry, total) {
        const row = document.createElement('div');
        row.className = 'dl-chain-row';

        const rank = document.createElement('span');
        rank.className = 'dl-chain-rank mono';
        rank.textContent = String(idx + 1);
        row.appendChild(rank);

        const arrows = document.createElement('div');
        arrows.className = 'dl-chain-arrows';
        const upBtn = document.createElement('button');
        upBtn.type = 'button';
        upBtn.className = 'vc-spin';
        upBtn.textContent = '▲';
        upBtn.title = 'Move up';
        upBtn.disabled = idx === 0;
        upBtn.addEventListener('click', () => dlMoveEntry(ctx, groupKey, idx, -1));
        const dnBtn = document.createElement('button');
        dnBtn.type = 'button';
        dnBtn.className = 'vc-spin';
        dnBtn.textContent = '▼';
        dnBtn.title = 'Move down';
        dnBtn.disabled = idx === total - 1;
        dnBtn.addEventListener('click', () => dlMoveEntry(ctx, groupKey, idx, 1));
        arrows.appendChild(upBtn);
        arrows.appendChild(dnBtn);
        row.appendChild(arrows);

        const name = document.createElement('span');
        name.className = 'dl-chain-name';
        name.appendChild(dlBackendDot(entry.backend));
        const nameLbl = document.createElement('span');
        nameLbl.textContent = entry.backend;
        name.appendChild(nameLbl);
        row.appendChild(name);

        const enLabel = document.createElement('label');
        enLabel.className = 'dl-chain-toggle';
        const enInput = document.createElement('input');
        enInput.type = 'checkbox';
        enInput.checked = !!entry.enabled;
        enInput.addEventListener('change', () => {
            ctx.working[groupKey][idx].enabled = enInput.checked;
            ctx.refreshHeader();
            ctx.refreshGroupBadge(groupKey);
        });
        const enText = document.createElement('span');
        enText.className = 'muted';
        enText.textContent = 'enabled';
        enLabel.appendChild(enInput);
        enLabel.appendChild(enText);
        row.appendChild(enLabel);

        const qWrap = document.createElement('label');
        qWrap.className = 'dl-chain-qwrap';
        const qLbl = document.createElement('span');
        qLbl.className = 'muted';
        qLbl.textContent = 'min quality:';
        qWrap.appendChild(qLbl);
        const qSel = document.createElement('select');
        qSel.className = 'dl-chain-quality';
        DL_QUALITY_TIERS.forEach(tier => {
            const opt = document.createElement('option');
            opt.value = tier.value;
            opt.textContent = tier.label;
            if ((entry.min_quality || '') === tier.value) opt.selected = true;
            qSel.appendChild(opt);
        });
        qSel.addEventListener('change', () => {
            ctx.working[groupKey][idx].min_quality = qSel.value || null;
            ctx.refreshHeader();
            ctx.refreshGroupBadge(groupKey);
        });
        qWrap.appendChild(qSel);
        row.appendChild(qWrap);

        const tWrap = document.createElement('label');
        tWrap.className = 'dl-chain-twrap';
        const tLbl = document.createElement('span');
        tLbl.className = 'muted';
        tLbl.textContent = 'timeout:';
        tWrap.appendChild(tLbl);
        const tIn = document.createElement('input');
        tIn.type = 'number';
        tIn.className = 'vc-num';
        tIn.style.width = '60px';
        tIn.min = '5';
        tIn.max = '600';
        tIn.step = '5';
        tIn.value = String(entry.timeout_s != null ? entry.timeout_s : 60);
        tIn.addEventListener('change', () => {
            let n = parseInt(tIn.value, 10);
            if (!isFinite(n) || n < 5) n = 5;
            if (n > 600) n = 600;
            ctx.working[groupKey][idx].timeout_s = n;
            tIn.value = String(n);
            ctx.refreshHeader();
            ctx.refreshGroupBadge(groupKey);
        });
        tWrap.appendChild(tIn);
        const tUnit = document.createElement('span');
        tUnit.className = 'muted';
        tUnit.textContent = 's';
        tWrap.appendChild(tUnit);
        row.appendChild(tWrap);

        const spacer = document.createElement('div');
        spacer.style.flex = '1';
        row.appendChild(spacer);

        const rmBtn = document.createElement('button');
        rmBtn.type = 'button';
        rmBtn.className = 'vc-btn vc-btn-ghost-sm dl-chain-remove';
        rmBtn.textContent = '×';
        rmBtn.title = 'Remove backend';
        rmBtn.addEventListener('click', () => {
            ctx.working[groupKey].splice(idx, 1);
            ctx.refreshGroup(groupKey);
            ctx.refreshHeader();
        });
        row.appendChild(rmBtn);

        return row;
    }

    function dlMoveEntry(ctx, groupKey, idx, delta) {
        const arr = ctx.working[groupKey];
        const ni = idx + delta;
        if (ni < 0 || ni >= arr.length) return;
        const tmp = arr[idx];
        arr[idx] = arr[ni];
        arr[ni] = tmp;
        ctx.refreshGroup(groupKey);
        ctx.refreshHeader();
    }

    function dlRenderParallelBody(ctx) {
        const wrap = document.createElement('div');
        wrap.className = 'dl-parallel';
        const eligible = (dlGroupMeta(ctx, 'parallel_search').backends_eligible) || [];
        const enabledList = (ctx.working && ctx.working.parallel_search_backends) || [];

        const checks = document.createElement('div');
        checks.className = 'dl-parallel-checks';
        const checksLbl = document.createElement('span');
        checksLbl.className = 'muted';
        checksLbl.textContent = 'backends:';
        checks.appendChild(checksLbl);
        eligible.forEach(b => {
            const lbl = document.createElement('label');
            lbl.className = 'dl-parallel-check';
            const cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.checked = enabledList.indexOf(b) !== -1;
            cb.addEventListener('change', () => {
                let list = ctx.working.parallel_search_backends || [];
                const idx = list.indexOf(b);
                if (cb.checked && idx === -1) list = list.concat([b]);
                if (!cb.checked && idx !== -1) {
                    list = list.slice();
                    list.splice(idx, 1);
                }
                ctx.working.parallel_search_backends = list;
                ctx.refreshHeader();
                ctx.refreshGroupBadge('parallel_search');
            });
            lbl.appendChild(cb);
            lbl.appendChild(dlBackendDot(b));
            const txt = document.createElement('span');
            txt.textContent = b;
            lbl.appendChild(txt);
            checks.appendChild(lbl);
        });
        wrap.appendChild(checks);

        const tWrap = document.createElement('label');
        tWrap.className = 'dl-parallel-timeout';
        const tLbl = document.createElement('span');
        tLbl.className = 'muted';
        tLbl.textContent = 'timeout:';
        tWrap.appendChild(tLbl);
        const tIn = document.createElement('input');
        tIn.type = 'number';
        tIn.className = 'vc-num';
        tIn.style.width = '80px';
        tIn.min = '500';
        tIn.max = '30000';
        tIn.step = '500';
        tIn.value = String(ctx.working.parallel_search_timeout_ms != null ? ctx.working.parallel_search_timeout_ms : 5000);
        tIn.addEventListener('change', () => {
            let n = parseInt(tIn.value, 10);
            if (!isFinite(n) || n < 500) n = 500;
            if (n > 30000) n = 30000;
            ctx.working.parallel_search_timeout_ms = n;
            tIn.value = String(n);
            ctx.refreshHeader();
            ctx.refreshGroupBadge('parallel_search');
        });
        tWrap.appendChild(tIn);
        const tUnit = document.createElement('span');
        tUnit.className = 'muted';
        tUnit.textContent = 'ms';
        tWrap.appendChild(tUnit);
        wrap.appendChild(tWrap);

        return wrap;
    }

    GIQ.pages.settings['download-routing'] = function renderDownloadRouting(root) {
        root.innerHTML = '';
        const host = document.createElement('div');
        host.className = 'vc-shell';
        root.appendChild(host);

        const shell = GIQ.components.versionedConfigShell({
            kind: 'downloads/routing',
            title: 'Download Routing',
            eyebrowPrefix: 'VERSIONED CONFIG',
            retrainGroups: [],
            exportName: 'grooveiq-routing',
            topRail: () => GIQ.components.relatedRail({
                links: [
                    { prefix: 'Actions', label: 'Search & Download', href: '#/actions/downloads' },
                    { prefix: 'Monitor', label: 'Live queue + telemetry', href: '#/monitor/downloads' },
                ],
            }),
            renderGroupBody: (ctx) => {
                if (ctx.groupKey === 'parallel_search') return dlRenderParallelBody(ctx);
                if (DL_CHAIN_KEYS.indexOf(ctx.groupKey) !== -1) return dlRenderChainBody(ctx);
                return null;
            },
            saveSideEffect: {
                label: 'Routing updated — next download will use the new chain',
            },
        });
        shell.mount(host);

        return () => shell.dispose();
    };

    /* ===================================================================
     * Settings → Lidarr Backfill Config
     * =================================================================== */

    const LBF_FIELD_META = {
        enabled: { desc: 'Master switch — when off, the scheduler tick is a no-op', type: 'bool' },
        dry_run: { desc: 'Match and persist with status=skipped, but never actually download', type: 'bool' },
        max_downloads_per_hour: { desc: 'Sliding-window cap; counts rows in the last 60 minutes', type: 'int', min: 1, max: 100, step: 1 },
        max_batch_size: { desc: 'Hard cap on albums processed per scheduler tick', type: 'int', min: 1, max: 25, step: 1 },
        poll_interval_minutes: { desc: 'How often the scheduler wakes to attempt the next batch', type: 'int', min: 1, max: 60, step: 1 },
        min_quality_floor: { desc: 'Skip the cascade if streamrip’s declared quality is below this tier', type: 'select', options: ['lossy_low', 'lossy_high', 'lossless', 'hires'] },
        service_priority: { desc: 'Streaming services tried in this order', type: 'order', options: ['qobuz', 'tidal', 'deezer', 'soundcloud'] },
        'sources.missing': { desc: 'Drain /api/v1/wanted/missing', type: 'bool' },
        'sources.cutoff_unmet': { desc: 'Drain /api/v1/wanted/cutoff (quality upgrades)', type: 'bool' },
        'sources.monitored_only': { desc: 'Skip albums that are unmonitored in Lidarr', type: 'bool' },
        'sources.queue_order': { desc: 'How to traverse Lidarr’s missing queue. recent_release = newest first; alphabetical hits a Lidarr quirk where non-Latin titles cluster at the top; random samples evenly.', type: 'select', options: ['recent_release', 'oldest_release', 'alphabetical', 'random'] },
        'match.min_artist_similarity': { desc: 'Reject if artist fuzzy ratio is below this (0–1)', type: 'float', min: 0, max: 1, step: 0.01 },
        'match.min_album_similarity': { desc: 'Reject if album-title fuzzy ratio is below this (0–1)', type: 'float', min: 0, max: 1, step: 0.01 },
        'match.require_year_match': { desc: 'Reject if release year differs by more than 1', type: 'bool' },
        'match.require_track_count_match': { desc: 'Reject if track count differs from Lidarr’s expectation', type: 'bool' },
        'match.prefer_album_over_tracks': { desc: 'Album-first: only fall back to per-track downloads when no album hit exists', type: 'bool' },
        'match.allow_structural_fallback': { desc: 'Accept candidates with low album-title similarity if artist matches exactly + same track count + same year (±1).', type: 'bool' },
        'retry.cooldown_hours': { desc: 'Wait this many hours before retrying a failed album', type: 'float', min: 0, max: 720, step: 0.5 },
        'retry.max_attempts': { desc: 'Permanently skip after this many failed attempts', type: 'int', min: 1, max: 20, step: 1 },
        'retry.backoff_multiplier': { desc: 'Cooldown grows on each retry (cooldown × multiplier^attempts)', type: 'float', min: 1.0, max: 10.0, step: 0.1 },
        'import_options.trigger_lidarr_scan': { desc: 'POST DownloadedAlbumsScan after a successful download', type: 'bool' },
        'import_options.scan_path': { desc: 'Path streamrip writes into (must match Lidarr’s view of that mount)', type: 'text' },
        'filters.artist_allowlist': { desc: 'If non-empty, only artists on this list are processed (one per line)', type: 'textarea' },
        'filters.artist_denylist': { desc: 'Artists on this list are skipped', type: 'textarea' },
    };

    function lbfFieldHtml(ctx, fieldKey, container) {
        // fieldKey may be a top-level path ("enabled") or a sub-object ("sources").
        // For sub-objects, recursively expand each child.
        const val = ctx.pathGet(ctx.defaults, fieldKey);
        if (val != null && typeof val === 'object' && !Array.isArray(val)) {
            const sub = document.createElement('div');
            sub.className = 'lbf-subgroup';
            const heading = document.createElement('div');
            heading.className = 'lbf-subgroup-head';
            heading.textContent = fieldKey.replace(/_/g, ' ');
            sub.appendChild(heading);
            for (const k of Object.keys(val)) {
                lbfFieldHtml(ctx, fieldKey + '.' + k, sub);
            }
            container.appendChild(sub);
            return;
        }
        const meta = LBF_FIELD_META[fieldKey] || { desc: fieldKey.replace(/_/g, ' '), type: 'text' };
        const cur = ctx.pathGet(ctx.working, fieldKey);
        const saved = ctx.pathGet(ctx.saved, fieldKey);
        const defVal = ctx.pathGet(ctx.defaults, fieldKey);
        const dirty = JSON.stringify(cur) !== JSON.stringify(saved);

        const row = document.createElement('div');
        row.className = 'vc-field' + (dirty ? ' dirty' : '');
        const head = document.createElement('div');
        head.className = 'vc-field-head';
        const lbl = document.createElement('label');
        lbl.className = 'vc-field-label';
        const lblText = (fieldKey.split('.').pop() || fieldKey).replace(/_/g, ' ');
        lbl.textContent = lblText;
        head.appendChild(lbl);
        row.appendChild(head);

        const ctrls = document.createElement('div');
        ctrls.className = 'vc-field-controls';

        function bumpDirty(v) {
            ctx.setWorking(fieldKey, v);
            const newSaved = ctx.pathGet(ctx.saved, fieldKey);
            row.classList.toggle('dirty', JSON.stringify(v) !== JSON.stringify(newSaved));
            ctx.refreshHeader();
            ctx.refreshGroupBadge(ctx.groupKey);
        }

        if (meta.type === 'bool') {
            const toggleLbl = document.createElement('label');
            toggleLbl.className = 'lbf-toggle-inline';
            const cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.checked = !!cur;
            cb.addEventListener('change', () => bumpDirty(cb.checked));
            const stateText = document.createElement('span');
            stateText.className = 'muted';
            stateText.textContent = cb.checked ? 'on' : 'off';
            cb.addEventListener('change', () => { stateText.textContent = cb.checked ? 'on' : 'off'; });
            toggleLbl.appendChild(cb);
            toggleLbl.appendChild(stateText);
            ctrls.appendChild(toggleLbl);
        } else if (meta.type === 'select') {
            const sel = document.createElement('select');
            sel.className = 'lbf-select';
            (meta.options || []).forEach(o => {
                const opt = document.createElement('option');
                opt.value = o;
                opt.textContent = o;
                if (cur === o) opt.selected = true;
                sel.appendChild(opt);
            });
            sel.addEventListener('change', () => bumpDirty(sel.value));
            ctrls.appendChild(sel);
        } else if (meta.type === 'order') {
            const orderWrap = document.createElement('div');
            orderWrap.className = 'lbf-order';
            const arr = Array.isArray(cur) ? cur.slice() : (meta.options || []).slice();
            arr.forEach((item, idx) => {
                const itemRow = document.createElement('div');
                itemRow.className = 'lbf-order-item';
                const itemLbl = document.createElement('span');
                itemLbl.className = 'lbf-order-label';
                itemLbl.textContent = item;
                itemRow.appendChild(itemLbl);
                const up = document.createElement('button');
                up.type = 'button';
                up.className = 'vc-spin';
                up.textContent = '▲';
                up.disabled = idx === 0;
                up.addEventListener('click', () => {
                    if (idx === 0) return;
                    const newArr = arr.slice();
                    [newArr[idx - 1], newArr[idx]] = [newArr[idx], newArr[idx - 1]];
                    bumpDirty(newArr);
                    ctx.refreshGroup(ctx.groupKey);
                });
                const dn = document.createElement('button');
                dn.type = 'button';
                dn.className = 'vc-spin';
                dn.textContent = '▼';
                dn.disabled = idx === arr.length - 1;
                dn.addEventListener('click', () => {
                    if (idx === arr.length - 1) return;
                    const newArr = arr.slice();
                    [newArr[idx + 1], newArr[idx]] = [newArr[idx], newArr[idx + 1]];
                    bumpDirty(newArr);
                    ctx.refreshGroup(ctx.groupKey);
                });
                itemRow.appendChild(up);
                itemRow.appendChild(dn);
                orderWrap.appendChild(itemRow);
            });
            ctrls.appendChild(orderWrap);
        } else if (meta.type === 'textarea') {
            const ta = document.createElement('textarea');
            ta.className = 'lbf-textarea';
            ta.rows = 3;
            ta.placeholder = 'One artist per line';
            ta.value = Array.isArray(cur) ? cur.join('\n') : (cur || '');
            ta.addEventListener('blur', () => {
                const lines = ta.value.split('\n').map(s => s.trim()).filter(s => s.length > 0);
                bumpDirty(lines);
            });
            ctrls.appendChild(ta);
        } else if (meta.type === 'int' || meta.type === 'float') {
            const slider = document.createElement('input');
            slider.type = 'range';
            slider.className = 'vc-slider';
            slider.min = String(meta.min);
            slider.max = String(meta.max);
            slider.step = String(meta.step);
            slider.value = String(cur);
            const numWrap = document.createElement('div');
            numWrap.className = 'vc-num-wrap';
            const minus = document.createElement('button');
            minus.type = 'button';
            minus.className = 'vc-spin';
            minus.textContent = '−';
            const num = document.createElement('input');
            num.type = 'number';
            num.className = 'vc-num';
            num.min = String(meta.min);
            num.max = String(meta.max);
            num.step = String(meta.step);
            num.value = String(cur);
            const plus = document.createElement('button');
            plus.type = 'button';
            plus.className = 'vc-spin';
            plus.textContent = '+';
            numWrap.appendChild(minus);
            numWrap.appendChild(num);
            numWrap.appendChild(plus);

            function setVal(v) {
                let n = (meta.type === 'int') ? Math.round(v) : v;
                if (n < meta.min) n = meta.min;
                if (n > meta.max) n = meta.max;
                if (meta.type !== 'int') {
                    const decimals = (String(meta.step).split('.')[1] || '').length;
                    n = parseFloat(n.toFixed(Math.max(decimals, 0)));
                }
                slider.value = String(n);
                num.value = String(n);
                bumpDirty(n);
            }
            slider.addEventListener('input', () => {
                const v = (meta.type === 'int') ? parseInt(slider.value, 10) : parseFloat(slider.value);
                if (!Number.isNaN(v)) setVal(v);
            });
            num.addEventListener('change', () => {
                const v = (meta.type === 'int') ? parseInt(num.value, 10) : parseFloat(num.value);
                if (Number.isNaN(v)) { num.value = String(cur); return; }
                setVal(v);
            });
            minus.addEventListener('click', () => setVal((parseFloat(num.value) || 0) - meta.step));
            plus.addEventListener('click', () => setVal((parseFloat(num.value) || 0) + meta.step));

            ctrls.appendChild(slider);
            ctrls.appendChild(numWrap);
        } else {
            const ti = document.createElement('input');
            ti.type = 'text';
            ti.className = 'vc-num';
            ti.style.width = '100%';
            ti.value = cur != null ? String(cur) : '';
            ti.addEventListener('change', () => bumpDirty(ti.value));
            ctrls.appendChild(ti);
        }

        row.appendChild(ctrls);

        const info = document.createElement('div');
        info.className = 'vc-field-info';
        const desc = document.createElement('span');
        desc.className = 'vc-field-desc';
        desc.textContent = meta.desc || '';
        info.appendChild(desc);
        const defaultIndicator = document.createElement('span');
        defaultIndicator.className = 'vc-field-default';
        if (JSON.stringify(cur) !== JSON.stringify(defVal)) {
            const defText = (typeof defVal === 'object') ? JSON.stringify(defVal) : String(defVal);
            defaultIndicator.textContent = 'default: ' + defText;
        }
        info.appendChild(defaultIndicator);
        row.appendChild(info);
        container.appendChild(row);
    }

    function lbfRenderGroupBody(ctx) {
        const wrap = document.createElement('div');
        wrap.className = 'vc-fields lbf-fields';
        const fields = (ctx.groupMeta && ctx.groupMeta.fields) || [];
        fields.forEach(fk => lbfFieldHtml(ctx, fk, wrap));
        return wrap;
    }

    function lbfHeaderExtras(ctx) {
        const wrap = document.createElement('div');
        wrap.className = 'lbf-master-toggle';
        const enabled = !!(ctx.working && ctx.working.enabled);
        if (!enabled) wrap.classList.add('off');

        const lbl = document.createElement('div');
        lbl.className = 'lbf-master-label';
        const eb = document.createElement('div');
        eb.className = 'eyebrow';
        eb.textContent = 'MASTER SWITCH';
        const title = document.createElement('div');
        title.className = 'lbf-master-title';
        title.textContent = enabled ? 'Backfill engine ENABLED' : 'Backfill engine DISABLED';
        const sub = document.createElement('div');
        sub.className = 'lbf-master-sub muted';
        sub.textContent = enabled
            ? 'Scheduler ticks at the configured cadence and dispatches downloads.'
            : 'Scheduler ticks are a no-op. Re-enable to resume backfill.';
        lbl.appendChild(eb);
        lbl.appendChild(title);
        lbl.appendChild(sub);
        wrap.appendChild(lbl);

        const toggleWrap = document.createElement('label');
        toggleWrap.className = 'lbf-master-switch';
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.checked = enabled;
        cb.addEventListener('change', () => {
            ctx.working.enabled = cb.checked;
            ctx.refresh();
        });
        const slot = document.createElement('span');
        slot.className = 'lbf-master-slot';
        toggleWrap.appendChild(cb);
        toggleWrap.appendChild(slot);
        wrap.appendChild(toggleWrap);

        return wrap;
    }

    function lbfPreviewMatch(ctx) {
        const body = document.createElement('div');
        body.className = 'lbf-preview';
        const filter = document.createElement('div');
        filter.className = 'lbf-preview-filter';
        const lblLimit = document.createElement('label');
        lblLimit.textContent = 'Limit:';
        const inLimit = document.createElement('input');
        inLimit.type = 'number';
        inLimit.className = 'vc-num';
        inLimit.style.width = '80px';
        inLimit.min = '1';
        inLimit.max = '100';
        inLimit.value = '20';
        const runBtn = document.createElement('button');
        runBtn.type = 'button';
        runBtn.className = 'vc-btn vc-btn-primary';
        runBtn.textContent = 'Run preview';
        const note = document.createElement('span');
        note.className = 'muted';
        note.textContent = 'Uses the working (unsaved) config. Nothing is persisted.';
        filter.appendChild(lblLimit);
        filter.appendChild(inLimit);
        filter.appendChild(runBtn);
        filter.appendChild(note);
        body.appendChild(filter);

        const results = document.createElement('div');
        results.className = 'lbf-preview-results';
        results.innerHTML = '<div class="vc-empty">Click "Run preview" to compare candidates against the working config.</div>';
        body.appendChild(results);

        runBtn.addEventListener('click', async () => {
            results.innerHTML = '<div class="vc-empty">Running preview…</div>';
            try {
                const limit = parseInt(inLimit.value, 10) || 20;
                const res = await GIQ.api.post('/v1/lidarr-backfill/preview', {
                    limit,
                    config_override: ctx.working,
                });
                if (res && res.error) {
                    results.innerHTML = '';
                    const err = document.createElement('div');
                    err.className = 'vc-empty';
                    err.textContent = res.error;
                    results.appendChild(err);
                    return;
                }
                const cands = (res && res.candidates) || [];
                if (!cands.length) {
                    results.innerHTML = '<div class="vc-empty">No missing albums in Lidarr to preview.</div>';
                    return;
                }
                results.innerHTML = '';
                const table = document.createElement('table');
                table.className = 'vc-table';
                table.innerHTML = '<thead><tr>'
                    + '<th>Decision</th><th>Artist</th><th>Album</th><th>Score</th><th>Service</th><th>Reasons</th>'
                    + '</tr></thead>';
                const tbody = document.createElement('tbody');
                cands.forEach(c => {
                    const tr = document.createElement('tr');
                    const decisionCls = c.decision === 'would_queue' ? 'vc-badge-active'
                        : c.decision === 'no_match' ? 'vc-badge-modified' : 'vc-badge-retrain';
                    const reasons = (c.reasons || []).join(', ');
                    tr.innerHTML = '<td><span class="vc-badge ' + decisionCls + '">'
                        + esc(c.decision) + '</span></td>'
                        + '<td>' + esc(c.artist || '?') + '</td>'
                        + '<td>' + esc(c.album || '?')
                            + (c.matched_album ? '<br><span class="muted mono">match: ' + esc(c.matched_album) + '</span>' : '')
                            + '</td>'
                        + '<td class="mono">' + (c.match_score != null ? (c.match_score * 100).toFixed(0) + '%' : '—') + '</td>'
                        + '<td>' + esc(c.picked_service || '—') + '</td>'
                        + '<td class="muted">' + esc(reasons) + '</td>';
                    tbody.appendChild(tr);
                });
                table.appendChild(tbody);
                results.appendChild(table);
            } catch (e) {
                results.innerHTML = '';
                const err = document.createElement('div');
                err.className = 'vc-empty';
                err.style.color = 'var(--wine)';
                err.textContent = 'Preview failed: ' + e.message;
                results.appendChild(err);
            }
        });

        const closeBtn = document.createElement('button');
        closeBtn.type = 'button';
        closeBtn.className = 'vc-btn vc-btn-ghost';
        closeBtn.textContent = 'Close';
        const m = GIQ.components.modal({
            title: 'Preview Match (top N)',
            body,
            footer: [closeBtn],
            width: 'lg',
        });
        closeBtn.addEventListener('click', () => m.close());
    }

    GIQ.pages.settings['lidarr-backfill'] = function renderLidarrBackfill(root) {
        root.innerHTML = '';
        const host = document.createElement('div');
        host.className = 'vc-shell';
        root.appendChild(host);

        let shell;
        shell = GIQ.components.versionedConfigShell({
            kind: 'lidarr-backfill',
            title: 'Lidarr Backfill Config',
            eyebrowPrefix: 'VERSIONED CONFIG',
            retrainGroups: [],
            exportName: 'grooveiq-lidarr-backfill',
            bodyClass: (ctx) => (ctx.working && ctx.working.enabled === false) ? 'lbf-disabled' : '',
            topRail: () => GIQ.components.relatedRail({
                links: [
                    { prefix: 'Actions', label: 'Backfill queue', href: '#/actions/discovery' },
                    { prefix: 'Monitor', label: 'Backfill stats', href: '#/monitor/lidarr-backfill' },
                ],
            }),
            headerExtras: (ctx) => lbfHeaderExtras(ctx),
            renderGroupBody: (ctx) => lbfRenderGroupBody(ctx),
            extraButtons: (ctx) => [{
                label: 'Preview Match →',
                kind: 'ghost',
                onClick: () => lbfPreviewMatch(ctx),
            }],
            saveSideEffect: {
                label: 'Backfill policy updated — takes effect on next tick',
                jumpHash: '#/monitor/lidarr-backfill',
                jumpLabel: 'Backfill stats →',
            },
        });
        shell.mount(host);

        return () => shell.dispose();
    };

    /* ===================================================================
     * Settings → Connections (snapshot)
     * =================================================================== */

    const CONN_ORDER = [
        { key: 'media_server', label: 'Media Server', icon: '♪', desc: 'Navidrome or Plex — source of track IDs and library metadata.' },
        { key: 'lidarr', label: 'Lidarr', icon: '⤓', desc: 'Automatic music discovery and download management.' },
        { key: 'spotdl_api', label: 'spotdl-api', icon: '◇', desc: 'YouTube Music downloads matched via Spotify metadata.' },
        { key: 'streamrip_api', label: 'streamrip-api', icon: '◆', desc: 'Qobuz / Tidal / Deezer / SoundCloud lossless downloads.' },
        { key: 'slskd', label: 'Soulseek (slskd)', icon: '∴', desc: 'Peer-to-peer music downloads via the Soulseek network.' },
        { key: 'lastfm', label: 'Last.fm', icon: '♫', desc: 'Scrobbling, taste enrichment, similar tracks, and charts.' },
        { key: 'acousticbrainz_lookup', label: 'AcousticBrainz Lookup', icon: '∇', desc: 'Audio-feature similarity search across 29.5M tracks.' },
    ];

    GIQ.pages.settings.connections = function renderConnections(root) {
        root.innerHTML = '';
        const header = GIQ.components.pageHeader({
            eyebrow: 'SETTINGS',
            title: 'Connections',
        });
        root.appendChild(header);

        const note = document.createElement('div');
        note.className = 'conn-snapshot-note muted';
        note.textContent = 'Read-only snapshot of integration configuration. Live health probes live on Monitor → Integrations.';
        root.appendChild(note);

        const grid = document.createElement('div');
        grid.className = 'conn-grid';
        grid.innerHTML = '<div class="vc-loading">Loading integration status…</div>';
        root.appendChild(grid);

        let cancelled = false;
        GIQ.api.get('/v1/integrations/status').then(data => {
            if (cancelled) return;
            grid.innerHTML = '';
            const integrations = (data && data.integrations) || {};
            CONN_ORDER.forEach(o => {
                const s = integrations[o.key] || {};
                grid.appendChild(GIQ.components.integrationCard({
                    name: o.label,
                    icon: o.icon,
                    description: o.desc,
                    type: s.type || null,
                    version: s.version || null,
                    configured: !!s.configured,
                    details: extractDetails(s),
                    snapshot: true,
                }));
            });
        }).catch(e => {
            if (cancelled) return;
            grid.innerHTML = '<div class="conn-error-msg">Failed to load integration status: ' + esc(e.message) + '</div>';
        });

        return () => { cancelled = true; };
    };

    function extractDetails(s) {
        const out = [];
        if (s.url) out.push({ label: 'URL', value: s.url, mono: true });
        if (s.scrobbling !== undefined) {
            out.push({ label: 'Scrobbling', value: s.scrobbling ? 'Enabled' : 'Disabled' });
        }
        if (s.details && typeof s.details === 'object') {
            Object.keys(s.details).forEach(k => {
                const v = s.details[k];
                if (v == null || v === '') return;
                const lbl = k.replace(/_/g, ' ').replace(/([A-Z])/g, ' $1').trim();
                out.push({ label: lbl.charAt(0).toUpperCase() + lbl.slice(1), value: String(v), mono: true });
            });
        }
        return out;
    }

    /* ===================================================================
     * Settings → Users (list)
     * =================================================================== */

    GIQ.pages.settings.users = function renderUsersPage(root, params) {
        if (params && params.user) {
            return renderUserDetail(root, params.user);
        }
        return renderUsersList(root);
    };

    function renderUsersList(root) {
        root.innerHTML = '';

        const addBtn = document.createElement('button');
        addBtn.type = 'button';
        addBtn.className = 'vc-btn vc-btn-primary';
        addBtn.textContent = '+ Add user';
        addBtn.addEventListener('click', () => showAddUserModal(() => loadUsers()));

        const header = GIQ.components.pageHeader({
            eyebrow: 'SETTINGS',
            title: 'Users',
            right: addBtn,
        });
        root.appendChild(header);

        const wrap = document.createElement('div');
        wrap.className = 'panel users-panel';
        wrap.innerHTML = '<div class="vc-loading">Loading users…</div>';
        root.appendChild(wrap);

        let cancelled = false;
        function loadUsers() {
            wrap.innerHTML = '<div class="vc-loading">Loading users…</div>';
            GIQ.api.get('/v1/users?limit=200').then(users => {
                if (cancelled) return;
                renderUsersTable(wrap, users || []);
            }).catch(e => {
                if (cancelled) return;
                wrap.innerHTML = '<div class="vc-empty">Failed to load users: ' + esc(e.message) + '</div>';
            });
        }
        loadUsers();
        return () => { cancelled = true; };
    }

    function renderUsersTable(wrap, users) {
        wrap.innerHTML = '';
        if (!users.length) {
            wrap.innerHTML = '<div class="vc-empty">No users yet. Users are auto-created when events are ingested, or click "+ Add user" above.</div>';
            return;
        }
        const table = document.createElement('table');
        table.className = 'vc-table users-table';
        table.innerHTML = '<thead><tr>'
            + '<th>UID</th><th>Username</th><th>Display Name</th>'
            + '<th>Events</th><th>Last seen</th><th>Created</th><th></th>'
            + '</tr></thead>';
        const tbody = document.createElement('tbody');
        users.forEach(u => {
            const tr = document.createElement('tr');
            tr.className = 'users-row';
            tr.innerHTML = '<td class="mono muted">' + esc(u.uid) + '</td>'
                + '<td><strong>' + esc(u.user_id) + '</strong></td>'
                + '<td>' + esc(u.display_name || '—') + '</td>'
                + '<td class="mono">' + (u.event_count != null ? u.event_count : '—') + '</td>'
                + '<td class="mono muted">' + esc(GIQ.fmt.timeAgo(u.last_seen)) + '</td>'
                + '<td class="mono muted">' + esc(GIQ.fmt.fmtTime(u.created_at)) + '</td>';
            const actionsTd = document.createElement('td');
            actionsTd.className = 'vc-row-actions';
            const link = GIQ.components.jumpLink({
                label: 'View',
                href: '#/settings/users?user=' + encodeURIComponent(u.user_id),
            });
            actionsTd.appendChild(link);
            tr.appendChild(actionsTd);
            tr.addEventListener('click', (e) => {
                if (e.target.closest('a, button')) return;
                GIQ.router.navigate('settings', 'users', { user: u.user_id });
            });
            tbody.appendChild(tr);
        });
        table.appendChild(tbody);
        wrap.appendChild(table);
    }

    function showAddUserModal(onCreated) {
        const body = document.createElement('div');
        body.innerHTML = ''
            + '<div class="form-field"><label>Username (user_id)</label>'
            + '<input type="text" id="u-add-id" placeholder="e.g. simon"></div>'
            + '<div class="form-field"><label>Display name (optional)</label>'
            + '<input type="text" id="u-add-name" placeholder="Display name"></div>'
            + '<p class="muted" style="font-size:11px;margin-top:6px">Users are also auto-created when events are ingested for a new user_id.</p>';
        const cancel = document.createElement('button');
        cancel.type = 'button';
        cancel.className = 'vc-btn vc-btn-ghost';
        cancel.textContent = 'Cancel';
        const create = document.createElement('button');
        create.type = 'button';
        create.className = 'vc-btn vc-btn-primary';
        create.textContent = 'Create';
        const m = GIQ.components.modal({
            title: 'Add user',
            body,
            footer: [cancel, create],
            width: 'sm',
        });
        cancel.addEventListener('click', () => m.close());
        create.addEventListener('click', async () => {
            const id = (body.querySelector('#u-add-id')?.value || '').trim();
            const name = (body.querySelector('#u-add-name')?.value || '').trim();
            if (!id) {
                GIQ.toast('Username is required', 'warning');
                return;
            }
            try {
                await GIQ.api.post('/v1/users', { user_id: id, display_name: name || null });
                GIQ.toast('User created: ' + id, 'success');
                m.close();
                if (typeof onCreated === 'function') onCreated();
            } catch (e) {
                GIQ.toast('Create failed: ' + e.message, 'error');
            }
        });
    }

    /* ===================================================================
     * Settings → User detail (#/settings/users?user=<id>)
     * =================================================================== */

    function renderUserDetail(root, userId) {
        root.innerHTML = '';

        const eyebrow = document.createElement('div');
        eyebrow.className = 'eyebrow';
        eyebrow.textContent = 'SETTINGS · USER';

        const header = document.createElement('header');
        header.className = 'page-header user-detail-header';
        const left = document.createElement('div');
        left.appendChild(eyebrow);
        const titleRow = document.createElement('div');
        titleRow.className = 'user-detail-title-row';
        const back = document.createElement('a');
        back.className = 'vc-btn vc-btn-ghost-sm';
        back.href = '#/settings/users';
        back.textContent = '← Users';
        titleRow.appendChild(back);
        const title = document.createElement('h1');
        title.className = 'page-title';
        title.textContent = userId;
        titleRow.appendChild(title);
        const uidBadge = document.createElement('span');
        uidBadge.className = 'vc-badge vc-badge-active mono user-uid';
        uidBadge.textContent = '…';
        titleRow.appendChild(uidBadge);
        left.appendChild(titleRow);
        header.appendChild(left);

        const right = document.createElement('div');
        right.className = 'page-header-right user-detail-actions';
        right.appendChild(GIQ.components.jumpLink({
            prefix: 'Monitor',
            label: 'View diagnostics',
            href: '#/monitor/user-diagnostics?user=' + encodeURIComponent(userId),
        }));
        header.appendChild(right);
        root.appendChild(header);

        const body = document.createElement('div');
        body.className = 'user-detail-body';
        body.innerHTML = '<div class="vc-loading">Loading user…</div>';
        root.appendChild(body);

        let cancelled = false;
        function load() {
            body.innerHTML = '<div class="vc-loading">Loading user…</div>';
            GIQ.api.get('/v1/users/' + encodeURIComponent(userId) + '/profile').then(profile => {
                if (cancelled) return;
                renderUserDetailBody(body, profile, load);
                if (profile && profile.uid != null) uidBadge.textContent = 'UID ' + profile.uid;
            }).catch(e => {
                if (cancelled) return;
                body.innerHTML = '<div class="vc-empty">Failed to load user: ' + esc(e.message) + '</div>';
            });
        }
        load();
        return () => { cancelled = true; };
    }

    function renderUserDetailBody(host, profile, reload) {
        host.innerHTML = '';
        const userId = profile.user_id;

        // Identity card
        const idCard = document.createElement('section');
        idCard.className = 'panel user-id-card';
        idCard.innerHTML = ''
            + '<div class="panel-head"><div class="panel-head-left">'
            + '<div class="panel-title-row"><div class="panel-title">Identity</div></div>'
            + '<div class="panel-sub">' + (profile.profile_updated_at
                ? 'Profile updated ' + esc(GIQ.fmt.timeAgo(profile.profile_updated_at))
                : 'Profile not yet computed') + '</div>'
            + '</div></div>';
        const idBody = document.createElement('div');
        idBody.className = 'panel-body user-id-body';
        idBody.innerHTML = ''
            + '<div class="user-id-row"><span class="muted">user_id</span>'
            + '<span class="mono">' + esc(userId) + '</span></div>'
            + '<div class="user-id-row"><span class="muted">display name</span>'
            + '<span>' + esc(profile.display_name || '—') + '</span></div>'
            + '<div class="user-id-row"><span class="muted">UID</span>'
            + '<span class="mono">' + esc(profile.uid) + '</span></div>';
        const idActions = document.createElement('div');
        idActions.className = 'user-id-actions';
        const editBtn = document.createElement('button');
        editBtn.type = 'button';
        editBtn.className = 'vc-btn vc-btn-ghost';
        editBtn.textContent = 'Edit user';
        editBtn.addEventListener('click', () => showEditUserModal(profile, () => reload()));
        idActions.appendChild(editBtn);
        const onbBtn = document.createElement('a');
        onbBtn.className = 'vc-btn vc-btn-ghost';
        onbBtn.textContent = 'Edit onboarding →';
        onbBtn.href = '#/settings/onboarding?user=' + encodeURIComponent(userId);
        idActions.appendChild(onbBtn);
        idBody.appendChild(idActions);
        idCard.appendChild(idBody);
        host.appendChild(idCard);

        // Last.fm card
        host.appendChild(buildLastfmCard(profile, reload));
    }

    function buildLastfmCard(profile, reload) {
        const userId = profile.user_id;
        const lfm = profile.lastfm || null;

        const card = document.createElement('section');
        card.className = 'panel user-lastfm-card';
        const head = document.createElement('div');
        head.className = 'panel-head';
        const headLeft = document.createElement('div');
        headLeft.className = 'panel-head-left';
        const titleRow = document.createElement('div');
        titleRow.className = 'panel-title-row';
        const t = document.createElement('div');
        t.className = 'panel-title';
        t.textContent = 'Last.fm';
        titleRow.appendChild(t);
        const badge = document.createElement('span');
        badge.className = 'vc-badge ' + (lfm ? 'vc-badge-active' : 'vc-badge-modified');
        badge.textContent = lfm ? 'connected' : 'not connected';
        titleRow.appendChild(badge);
        headLeft.appendChild(titleRow);
        const sub = document.createElement('div');
        sub.className = 'panel-sub muted';
        sub.textContent = lfm
            ? 'Username @' + (lfm.username || '?') + ' · synced ' + GIQ.fmt.timeAgo(lfm.synced_at)
            : 'Connect a Last.fm account to scrobble plays and enrich the user\'s taste profile.';
        headLeft.appendChild(sub);
        head.appendChild(headLeft);
        card.appendChild(head);

        const body = document.createElement('div');
        body.className = 'panel-body user-lastfm-body';

        if (!lfm) {
            const form = document.createElement('div');
            form.className = 'lastfm-connect-form';
            const userInput = document.createElement('input');
            userInput.type = 'text';
            userInput.className = 'vc-num';
            userInput.style.width = '180px';
            userInput.placeholder = 'Last.fm username';
            const tokInput = document.createElement('input');
            tokInput.type = 'text';
            tokInput.className = 'vc-num';
            tokInput.style.width = '220px';
            tokInput.placeholder = 'session token (optional)';
            const connectBtn = document.createElement('button');
            connectBtn.type = 'button';
            connectBtn.className = 'vc-btn vc-btn-primary';
            connectBtn.textContent = 'Connect';
            connectBtn.addEventListener('click', async () => {
                const username = userInput.value.trim();
                if (!username) {
                    GIQ.toast('Username is required', 'warning');
                    return;
                }
                try {
                    await GIQ.api.post('/v1/users/' + encodeURIComponent(userId) + '/lastfm/connect', {
                        username,
                        session_key: tokInput.value.trim() || null,
                    });
                    GIQ.toast('Last.fm connected for ' + userId, 'success');
                    reload();
                } catch (e) {
                    GIQ.toast('Connect failed: ' + e.message, 'error');
                }
            });
            form.appendChild(userInput);
            form.appendChild(tokInput);
            form.appendChild(connectBtn);
            body.appendChild(form);
        } else {
            const stats = document.createElement('div');
            stats.className = 'lastfm-stats';
            stats.innerHTML = ''
                + '<div class="lastfm-stat"><div class="muted">Username</div><div class="mono">' + esc(lfm.username || '—') + '</div></div>'
                + '<div class="lastfm-stat"><div class="muted">Scrobbling</div><div>'
                    + (lfm.scrobbling_enabled
                        ? '<span class="vc-badge vc-badge-active">active</span>'
                        : '<span class="vc-badge vc-badge-modified">read-only</span>') + '</div></div>'
                + '<div class="lastfm-stat"><div class="muted">Last sync</div><div class="mono">' + esc(GIQ.fmt.timeAgo(lfm.synced_at)) + '</div></div>';
            body.appendChild(stats);

            const actions = document.createElement('div');
            actions.className = 'lastfm-actions';
            actions.appendChild(_userActionBtn('Sync now', 'primary', async () => {
                try {
                    await GIQ.api.post('/v1/users/' + encodeURIComponent(userId) + '/lastfm/sync', {});
                    GIQ.toast('Last.fm sync triggered for ' + userId, 'success');
                    reload();
                } catch (e) { GIQ.toast('Sync failed: ' + e.message, 'error'); }
            }));
            actions.appendChild(_userActionBtn('Backfill scrobbles', 'ghost', async () => {
                if (!confirm('Scan all past plays and enqueue missed scrobbles? This may take a while for large histories.')) return;
                try {
                    const data = await GIQ.api.post('/v1/users/' + encodeURIComponent(userId) + '/lastfm/backfill', {});
                    GIQ.toast('Backfill: ' + (data.enqueued || 0) + ' enqueued · ' + (data.already_queued || 0) + ' already queued', 'success');
                } catch (e) { GIQ.toast('Backfill failed: ' + e.message, 'error'); }
            }));
            actions.appendChild(_userActionBtn('Disconnect', 'ghost', async () => {
                if (!confirm('Disconnect ' + userId + ' from Last.fm? Scrobbling will stop.')) return;
                try {
                    await GIQ.api.del('/v1/users/' + encodeURIComponent(userId) + '/lastfm');
                    GIQ.toast('Last.fm disconnected', 'success');
                    reload();
                } catch (e) { GIQ.toast('Disconnect failed: ' + e.message, 'error'); }
            }));
            body.appendChild(actions);
        }

        card.appendChild(body);
        return card;
    }

    function _userActionBtn(label, kind, onClick) {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'vc-btn vc-btn-' + (kind || 'ghost');
        b.textContent = label;
        b.addEventListener('click', onClick);
        return b;
    }

    function showEditUserModal(profile, onSaved) {
        const body = document.createElement('div');
        body.innerHTML = ''
            + '<div class="form-field"><label>Username (user_id)</label>'
            + '<input type="text" id="u-edit-id" value="' + esc(profile.user_id) + '"></div>'
            + '<div class="form-field"><label>Display name</label>'
            + '<input type="text" id="u-edit-name" value="' + esc(profile.display_name || '') + '"></div>'
            + '<p class="muted" style="font-size:11px;margin-top:6px">Renaming the username cascades to all events, sessions, and interactions.</p>';
        const cancel = document.createElement('button');
        cancel.type = 'button';
        cancel.className = 'vc-btn vc-btn-ghost';
        cancel.textContent = 'Cancel';
        const save = document.createElement('button');
        save.type = 'button';
        save.className = 'vc-btn vc-btn-primary';
        save.textContent = 'Save';
        const m = GIQ.components.modal({
            title: 'Edit user · UID ' + profile.uid,
            body,
            footer: [cancel, save],
            width: 'sm',
        });
        cancel.addEventListener('click', () => m.close());
        save.addEventListener('click', async () => {
            const newId = (body.querySelector('#u-edit-id')?.value || '').trim();
            const newName = (body.querySelector('#u-edit-name')?.value || '').trim();
            if (!newId) {
                GIQ.toast('Username cannot be empty', 'warning');
                return;
            }
            const patch = { user_id: newId, display_name: newName || null };
            try {
                const updated = await GIQ.api.patch('/v1/users/' + profile.uid, patch);
                m.close();
                GIQ.toast('User updated', 'success');
                if (newId !== profile.user_id) {
                    GIQ.router.navigate('settings', 'users', { user: updated.user_id });
                } else if (typeof onSaved === 'function') {
                    onSaved();
                }
            } catch (e) {
                GIQ.toast('Save failed: ' + e.message, 'error');
            }
        });
    }

    /* ===================================================================
     * Settings → Onboarding (#/settings/onboarding?user=<id>)
     * =================================================================== */

    GIQ.pages.settings.onboarding = function renderOnboardingPage(root, params) {
        root.innerHTML = '';
        const userId = params && params.user;
        if (!userId) {
            return renderOnboardingPicker(root);
        }
        return renderOnboardingEditor(root, userId);
    };

    function renderOnboardingPicker(root) {
        const header = GIQ.components.pageHeader({
            eyebrow: 'SETTINGS',
            title: 'Onboarding',
        });
        root.appendChild(header);
        const note = document.createElement('div');
        note.className = 'conn-snapshot-note muted';
        note.textContent = 'Pick a user to edit their onboarding preferences.';
        root.appendChild(note);

        const wrap = document.createElement('div');
        wrap.className = 'panel onboarding-picker-panel';
        wrap.innerHTML = '<div class="vc-loading">Loading users…</div>';
        root.appendChild(wrap);

        let cancelled = false;
        GIQ.api.get('/v1/users?limit=200').then(users => {
            if (cancelled) return;
            wrap.innerHTML = '';
            if (!users.length) {
                wrap.innerHTML = '<div class="vc-empty">No users yet. Create one in Settings → Users.</div>';
                return;
            }
            const list = document.createElement('div');
            list.className = 'onboarding-picker-list';
            users.forEach(u => {
                const a = document.createElement('a');
                a.className = 'onboarding-picker-item';
                a.href = '#/settings/onboarding?user=' + encodeURIComponent(u.user_id);
                a.innerHTML = '<span class="mono muted">UID ' + esc(u.uid) + '</span>'
                    + '<strong>' + esc(u.user_id) + '</strong>'
                    + '<span class="muted">' + esc(u.display_name || '') + '</span>'
                    + '<span class="onboarding-picker-arrow">→</span>';
                list.appendChild(a);
            });
            wrap.appendChild(list);
        }).catch(e => {
            if (cancelled) return;
            wrap.innerHTML = '<div class="vc-empty">Failed to load users: ' + esc(e.message) + '</div>';
        });
        return () => { cancelled = true; };
    }

    function renderOnboardingEditor(root, userId) {
        const header = document.createElement('header');
        header.className = 'page-header';
        const left = document.createElement('div');
        const eb = document.createElement('div');
        eb.className = 'eyebrow';
        eb.textContent = 'SETTINGS · ONBOARDING';
        left.appendChild(eb);
        const title = document.createElement('div');
        title.className = 'user-detail-title-row';
        const back = document.createElement('a');
        back.className = 'vc-btn vc-btn-ghost-sm';
        back.href = '#/settings/users?user=' + encodeURIComponent(userId);
        back.textContent = '← User';
        title.appendChild(back);
        const h1 = document.createElement('h1');
        h1.className = 'page-title';
        h1.textContent = userId;
        title.appendChild(h1);
        left.appendChild(title);
        header.appendChild(left);
        root.appendChild(header);

        const wrap = document.createElement('div');
        wrap.className = 'panel onboarding-editor-panel';
        wrap.innerHTML = '<div class="vc-loading">Loading onboarding preferences…</div>';
        root.appendChild(wrap);

        let cancelled = false;
        GIQ.api.get('/v1/users/' + encodeURIComponent(userId) + '/onboarding').then(data => {
            if (cancelled) return;
            renderOnboardingForm(wrap, userId, data || {});
        }).catch(e => {
            if (cancelled) return;
            wrap.innerHTML = '<div class="vc-empty">Failed to load onboarding: ' + esc(e.message) + '</div>';
        });
        return () => { cancelled = true; };
    }

    function renderOnboardingForm(wrap, userId, prefs) {
        wrap.innerHTML = '';
        const state = {
            favourite_artists: Array.isArray(prefs.favourite_artists) ? prefs.favourite_artists.join('\n') : '',
            favourite_genres: Array.isArray(prefs.favourite_genres) ? prefs.favourite_genres.join('\n') : '',
            favourite_tracks: Array.isArray(prefs.favourite_tracks) ? prefs.favourite_tracks.join('\n') : '',
            mood_preferences: Array.isArray(prefs.mood_preferences) ? prefs.mood_preferences.slice() : [],
            listening_contexts: Array.isArray(prefs.listening_contexts) ? prefs.listening_contexts.slice() : [],
            device_types: Array.isArray(prefs.device_types) ? prefs.device_types.slice() : [],
            energy_preference: prefs.energy_preference != null ? prefs.energy_preference : 0.5,
            danceability_preference: prefs.danceability_preference != null ? prefs.danceability_preference : 0.5,
        };

        const sec = (label, sub, body) => {
            const block = document.createElement('div');
            block.className = 'onboarding-section';
            const h = document.createElement('div');
            h.className = 'onboarding-section-head';
            const title = document.createElement('div');
            title.className = 'onboarding-section-title';
            title.textContent = label;
            h.appendChild(title);
            if (sub) {
                const s = document.createElement('div');
                s.className = 'onboarding-section-sub muted';
                s.textContent = sub;
                h.appendChild(s);
            }
            block.appendChild(h);
            block.appendChild(body);
            return block;
        };

        const _ta = (key, placeholder) => {
            const ta = document.createElement('textarea');
            ta.className = 'lbf-textarea onboarding-textarea';
            ta.rows = 4;
            ta.placeholder = placeholder;
            ta.value = state[key];
            ta.addEventListener('input', () => { state[key] = ta.value; });
            return ta;
        };

        const _chips = (key, options) => {
            const wrap2 = document.createElement('div');
            wrap2.className = 'onboarding-chips';
            options.forEach(opt => {
                const chip = document.createElement('button');
                chip.type = 'button';
                chip.className = 'onboarding-chip';
                if (state[key].indexOf(opt) !== -1) chip.classList.add('selected');
                chip.textContent = opt;
                chip.addEventListener('click', () => {
                    const arr = state[key];
                    const i = arr.indexOf(opt);
                    if (i === -1) arr.push(opt); else arr.splice(i, 1);
                    chip.classList.toggle('selected');
                });
                wrap2.appendChild(chip);
            });
            return wrap2;
        };

        const _slider = (key) => {
            const wrap2 = document.createElement('div');
            wrap2.className = 'onboarding-slider-wrap';
            const slider = document.createElement('input');
            slider.type = 'range';
            slider.className = 'vc-slider';
            slider.min = '0';
            slider.max = '1';
            slider.step = '0.05';
            slider.value = String(state[key]);
            const num = document.createElement('span');
            num.className = 'mono onboarding-slider-num';
            num.textContent = (+state[key]).toFixed(2);
            slider.addEventListener('input', () => {
                state[key] = parseFloat(slider.value);
                num.textContent = state[key].toFixed(2);
            });
            wrap2.appendChild(slider);
            wrap2.appendChild(num);
            return wrap2;
        };

        wrap.appendChild(sec('Favourite artists',
            'One per line. Matched against the local library by name.',
            _ta('favourite_artists', 'e.g.\nKendrick Lamar\nFleet Foxes\nDaft Punk')));
        wrap.appendChild(sec('Favourite genres',
            'One per line. Free text — no enum.',
            _ta('favourite_genres', 'e.g.\nrock\nelectronic\njazz')));
        wrap.appendChild(sec('Favourite tracks (track_id)',
            'One library track_id per line.',
            _ta('favourite_tracks', 'e.g.\n3a7e9c01...\n8b2f0d83...')));
        wrap.appendChild(sec('Preferred moods',
            'Pick the moods you reach for most.',
            _chips('mood_preferences',
                ['happy', 'sad', 'aggressive', 'relaxed', 'party', 'acoustic', 'electronic', 'energetic'])));
        wrap.appendChild(sec('Typical listening contexts',
            'Where do you usually listen?',
            _chips('listening_contexts',
                ['home', 'work', 'gym', 'commute', 'sleep', 'study', 'driving'])));
        wrap.appendChild(sec('Typical devices',
            'What you usually listen on.',
            _chips('device_types',
                ['mobile', 'desktop', 'speaker', 'car', 'web'])));
        wrap.appendChild(sec('Energy preference (0=calm, 1=intense)',
            null, _slider('energy_preference')));
        wrap.appendChild(sec('Danceability (0=not danceable, 1=very danceable)',
            null, _slider('danceability_preference')));

        const actions = document.createElement('div');
        actions.className = 'onboarding-actions';
        const saveBtn = document.createElement('button');
        saveBtn.type = 'button';
        saveBtn.className = 'vc-btn vc-btn-primary';
        saveBtn.textContent = 'Save preferences';
        saveBtn.addEventListener('click', async () => {
            const body = {
                favourite_artists: state.favourite_artists.split('\n').map(s => s.trim()).filter(Boolean),
                favourite_genres: state.favourite_genres.split('\n').map(s => s.trim()).filter(Boolean),
                favourite_tracks: state.favourite_tracks.split('\n').map(s => s.trim()).filter(Boolean),
                mood_preferences: state.mood_preferences.length ? state.mood_preferences : null,
                listening_contexts: state.listening_contexts.length ? state.listening_contexts : null,
                device_types: state.device_types.length ? state.device_types : null,
                energy_preference: state.energy_preference,
                danceability_preference: state.danceability_preference,
            };
            // Drop empty arrays so the API doesn't 422 on "all None".
            Object.keys(body).forEach(k => {
                if (Array.isArray(body[k]) && body[k].length === 0) body[k] = null;
            });
            const allNull = Object.values(body).every(v => v == null);
            if (allNull) {
                GIQ.toast('Provide at least one preference before saving.', 'warning');
                return;
            }
            try {
                const res = await GIQ.api.post('/v1/users/' + encodeURIComponent(userId) + '/onboarding', body);
                const matched = (res && (res.matched_tracks || 0));
                GIQ.toast('Onboarding saved · ' + (res.preferences_saved || 0) + ' fields'
                    + (matched ? ', ' + matched + ' tracks matched' : ''), 'success');
            } catch (e) {
                GIQ.toast('Save failed: ' + e.message, 'error');
            }
        });
        actions.appendChild(saveBtn);
        wrap.appendChild(actions);
    }

    /* ===================================================================
     * Integration card component (used by Settings → Connections + later by
     * Monitor → Integrations in session 07).
     * =================================================================== */

    GIQ.components.integrationCard = function integrationCard(opts) {
        const { name, icon, type, version, configured, details, description, snapshot, error, configurePath } = opts || {};
        const card = document.createElement('section');
        card.className = 'conn-card' + (configured ? ' configured' : ' unconfigured');

        const head = document.createElement('div');
        head.className = 'conn-card-head';
        const iconEl = document.createElement('span');
        iconEl.className = 'conn-card-icon';
        iconEl.textContent = icon || '◇';
        head.appendChild(iconEl);

        const titleGroup = document.createElement('div');
        titleGroup.className = 'conn-card-title-group';
        const tName = document.createElement('div');
        tName.className = 'conn-card-name';
        tName.textContent = name || '';
        titleGroup.appendChild(tName);
        const subBits = [];
        if (type) subBits.push(type);
        if (version) subBits.push('v' + version);
        if (subBits.length) {
            const tSub = document.createElement('div');
            tSub.className = 'conn-card-meta muted mono';
            tSub.textContent = subBits.join(' · ');
            titleGroup.appendChild(tSub);
        }
        head.appendChild(titleGroup);

        const badge = document.createElement('span');
        badge.className = 'vc-badge ' + (configured ? 'vc-badge-active' : 'vc-badge-modified');
        badge.textContent = configured ? 'configured' : 'not configured';
        head.appendChild(badge);
        card.appendChild(head);

        if (description) {
            const desc = document.createElement('div');
            desc.className = 'conn-card-desc muted';
            desc.textContent = description;
            card.appendChild(desc);
        }

        if (configured && details && details.length) {
            const dl = document.createElement('div');
            dl.className = 'conn-card-details';
            details.forEach(d => {
                if (!d) return;
                const row = document.createElement('div');
                row.className = 'conn-card-detail-row';
                const lbl = document.createElement('span');
                lbl.className = 'conn-card-detail-label muted';
                lbl.textContent = d.label;
                const val = document.createElement('span');
                val.className = 'conn-card-detail-value' + (d.mono ? ' mono' : '');
                val.textContent = d.value == null ? '—' : String(d.value);
                row.appendChild(lbl);
                row.appendChild(val);
                dl.appendChild(row);
            });
            card.appendChild(dl);
        }

        if (!configured) {
            const hint = document.createElement('div');
            hint.className = 'conn-card-hint';
            hint.innerHTML = 'Set the required env vars in your <code>.env</code> file to enable this integration.';
            card.appendChild(hint);
        } else if (snapshot) {
            const note = document.createElement('div');
            note.className = 'conn-card-snapshot-note muted';
            note.textContent = 'Live health probe lives on Monitor → Integrations.';
            card.appendChild(note);
        }

        if (error) {
            const errEl = document.createElement('div');
            errEl.className = 'conn-card-error';
            errEl.textContent = error;
            card.appendChild(errEl);
        }

        if (configurePath) {
            const link = GIQ.components.jumpLink({
                label: 'Configure',
                href: configurePath,
            });
            card.appendChild(link);
        }

        return card;
    };
})();

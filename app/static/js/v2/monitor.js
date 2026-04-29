/* monitor.js — Monitor bucket pages. Stubs for session 01.
 * Sessions 02, 06, 07, 08 replace these with real pages.
 */

(function () {
    const SUBS = [
        'overview', 'pipeline', 'models', 'system-health', 'recs-debug',
        'user-diagnostics', 'integrations', 'downloads', 'lidarr-backfill',
        'discovery', 'charts',
    ];
    GIQ.pages.monitor = GIQ.pages.monitor || {};
    for (const sp of SUBS) {
        GIQ.pages.monitor[sp] = function (root) {
            const label = GIQ.router.SUBPAGE_LABELS[sp] || sp;
            root.innerHTML = '<div class="page-stub">'
                + '<div class="eyebrow">MONITOR</div>'
                + '<h1>' + GIQ.fmt.esc(label) + '</h1>'
                + '<p class="muted">Page: monitor → ' + GIQ.fmt.esc(sp) + ' — TBD</p>'
                + '</div>';
        };
    }
})();

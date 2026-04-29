/* settings.js — Settings bucket pages. Stubs for session 01.
 * Sessions 03, 04 replace these with real pages.
 */

(function () {
    const SUBS = [
        'algorithm', 'download-routing', 'lidarr-backfill',
        'connections', 'users', 'onboarding',
    ];
    GIQ.pages.settings = GIQ.pages.settings || {};
    for (const sp of SUBS) {
        GIQ.pages.settings[sp] = function (root) {
            const label = GIQ.router.SUBPAGE_LABELS[sp] || sp;
            root.innerHTML = '<div class="page-stub">'
                + '<div class="eyebrow">SETTINGS</div>'
                + '<h1>' + GIQ.fmt.esc(label) + '</h1>'
                + '<p class="muted">Page: settings → ' + GIQ.fmt.esc(sp) + ' — TBD</p>'
                + '</div>';
        };
    }
})();

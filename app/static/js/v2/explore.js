/* explore.js — Explore bucket pages. Stubs for session 01.
 * Sessions 09–11 replace these with real pages.
 */

(function () {
    const SUBS = [
        'recommendations', 'radio', 'playlists', 'tracks',
        'text-search', 'music-map', 'charts', 'artists', 'news',
    ];
    GIQ.pages.explore = GIQ.pages.explore || {};
    for (const sp of SUBS) {
        GIQ.pages.explore[sp] = function (root) {
            const label = GIQ.router.SUBPAGE_LABELS[sp] || sp;
            root.innerHTML = '<div class="page-stub">'
                + '<div class="eyebrow">EXPLORE</div>'
                + '<h1>' + GIQ.fmt.esc(label) + '</h1>'
                + '<p class="muted">Page: explore → ' + GIQ.fmt.esc(sp) + ' — TBD</p>'
                + '</div>';
        };
    }
})();

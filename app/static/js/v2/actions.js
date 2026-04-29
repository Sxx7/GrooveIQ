/* actions.js — Actions bucket pages. Stubs for session 01.
 * Session 05 replaces these with real pages.
 */

(function () {
    const SUBS = [
        'pipeline-ml', 'library', 'discovery', 'charts', 'downloads',
    ];
    GIQ.pages.actions = GIQ.pages.actions || {};
    for (const sp of SUBS) {
        GIQ.pages.actions[sp] = function (root) {
            const label = GIQ.router.SUBPAGE_LABELS[sp] || sp;
            root.innerHTML = '<div class="page-stub">'
                + '<div class="eyebrow">ACTIONS</div>'
                + '<h1>' + GIQ.fmt.esc(label) + '</h1>'
                + '<p class="muted">Page: actions → ' + GIQ.fmt.esc(sp) + ' — TBD</p>'
                + '</div>';
        };
    }
})();

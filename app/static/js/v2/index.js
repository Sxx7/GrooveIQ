/* index.js — boot.
 * Loaded last; ties together core, router, shell, and page modules.
 */

(function () {
    function boot() {
        GIQ.apiKey.load();

        // Render the shell scaffold first so the router has a #page-root to populate.
        GIQ.shell.init();

        // Start activity polling immediately — it gracefully no-ops without a key.
        if (GIQ.activity?.start) GIQ.activity.start();

        // If a key is already present, validate it and kick off SSE + activity.
        if (GIQ.state.apiKey) {
            GIQ.api.validateKey().then(ok => {
                GIQ.state.apiKeyValid = ok;
                GIQ.shell.renderSidebar();
                if (ok && GIQ.sse?.connect) GIQ.sse.connect();
                if (GIQ.activity?.refresh) GIQ.activity.refresh();
            });
        }

        // Normalize hash (empty → default) then dispatch.
        if (!window.location.hash) {
            window.location.hash = '#/monitor/overview';
        } else {
            GIQ.router.dispatch();
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', boot);
    } else {
        boot();
    }
})();

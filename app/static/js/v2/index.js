/* index.js — boot.
 * Loaded last; ties together core, router, shell, and page modules.
 */

(function () {
    function boot() {
        GIQ.apiKey.load();

        // Render the shell scaffold first so the router has a #page-root to populate.
        GIQ.shell.init();

        // If no key, validate (it'll just be /health without auth) so we know the
        // server is reachable; this also catches a server-down case at boot.
        if (GIQ.state.apiKey) {
            GIQ.api.validateKey().then(ok => {
                GIQ.state.apiKeyValid = ok;
                GIQ.shell.renderSidebar();
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

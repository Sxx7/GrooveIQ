# Code conventions for the rebuild

Every session follows these. They're locked.

## Stack

- **Vanilla JS, no React, no build step.** The design hand-off uses JSX as a prototype tool only.
- **No bundler, no transpiler.** Code runs as-loaded in the browser. ES2020 features (optional chaining, nullish coalescing, arrow functions, template literals) are fine — modern browsers only.
- **No external runtime deps.** Everything in `/static/`. Existing dependencies (e.g. fonts) come from CDN via `<link>` tags only.

## File layout

The new dashboard lives alongside the old one until cutover (session 12).

```
app/static/
├── index.html                 # OLD — untouched until session 12
├── dashboard-v2.html          # NEW — built across sessions 1-11
├── css/
│   ├── style.css              # OLD — untouched
│   ├── tokens.css             # NEW — palette, type, spacing, radius (session 01)
│   ├── shell.css              # NEW — sidebar, topbar, activity pill (session 01)
│   ├── components.css         # NEW — panels, stats, tables, modals (session 02 onwards)
│   └── pages.css              # NEW — page-specific overrides where needed
└── js/
    ├── app.js                 # OLD — untouched
    └── v2/                    # NEW
        ├── core.js            # utilities, state, api wrappers, formatters (session 01)
        ├── router.js          # bucket→subpage hash routing (session 01)
        ├── shell.js           # sidebar / topbar / activity pill (session 01-02)
        ├── components.js      # shared components (session 02 onwards)
        ├── explore.js         # all Explore sub-pages (sessions 09-11)
        ├── actions.js         # all Action group pages (session 05)
        ├── monitor.js         # all Monitor surfaces (sessions 02, 06, 07, 08)
        ├── settings.js        # all Settings pages (sessions 03, 04)
        └── index.js           # entry point — wires it all together
```

Multiple `<script>` tags in `dashboard-v2.html` load these in dependency order. No imports, no modules — files share state via `window.GIQ` (a single namespace).

## Globals

One global namespace, populated incrementally:

```js
window.GIQ = window.GIQ || {};
GIQ.state = { /* mutable runtime state */ };
GIQ.api = { /* fetch wrappers */ };
GIQ.fmt = { /* formatters */ };
GIQ.router = { /* navigation */ };
GIQ.components = { /* shared components */ };
GIQ.pages = { /* per-page render functions */ };
```

Do not pollute `window` directly. Inline `onclick="..."` handlers should go through `GIQ.handle.foo(...)` not bare `foo(...)`.

## Component pattern

Each component is a function that returns a DOM element (not a string):

```js
GIQ.components.statTile = function statTile({ label, value, delta, deltaKind }) {
    const el = document.createElement('div');
    el.className = 'stat-tile';
    el.innerHTML = `
        <div class="eyebrow">${GIQ.fmt.esc(label)}</div>
        <div class="stat-value">${GIQ.fmt.esc(value)}</div>
        ${delta ? `<div class="stat-delta delta-${deltaKind}">${GIQ.fmt.esc(delta)}</div>` : ''}
    `;
    return el;
};
```

Build components with `createElement` + `innerHTML` for simple static content. For interactive content, attach event listeners explicitly (no inline handlers in template strings — they break with strict CSP and are hard to debug).

**Always escape user / API content** via `GIQ.fmt.esc(...)` before injecting into `innerHTML`.

## Page pattern

Each "page" is a render function that takes a context container and populates it:

```js
GIQ.pages.monitorOverview = function (root, params) {
    root.innerHTML = '';
    root.appendChild(GIQ.components.pageHeader({ eyebrow: 'Monitor', title: 'Overview' }));
    // ... etc
    // Subscribe to data sources; return a cleanup function the router can call on nav-away
    const refresh = setInterval(() => fetchOverviewData(), 10000);
    return () => clearInterval(refresh);
};
```

The router calls the page function on navigation. The cleanup function (returned by the page) is called when the user navigates away, so no leaked timers / SSE connections.

## CSS

- Tokens via CSS custom properties (lifted from `design_handoff/styles.css`). See [components.md](components.md) for the locked palette.
- Use semantic class names: `.panel`, `.panel-title`, `.stat-tile`, `.btn-primary`.
- Avoid utility classes like `.flex`, `.gap-3` — those were design-tool scaffolding. Use proper CSS rules.
- Theme switching via `[data-theme="dark"]` on `<html>`. Default = dark. Light is supported but secondary.
- Mobile via media queries; breakpoints are `700px` (small) and `1100px` (large). See [components.md → responsive](components.md).

## Routing

Hash-based: `#/<bucket>/<subpage>` (e.g. `#/monitor/overview`, `#/explore/recommendations`). Router parses on `hashchange` and dispatches to the right page render function.

Bucket landing pages: `#/explore` → recommendations · `#/actions` → pipeline-ml · `#/monitor` → overview · `#/settings` → algorithm.

Empty hash on first load defaults to `#/monitor/overview`.

## Accessibility

- Every interactive element is a real `<button>` or `<a>`, not a clickable `<div>`.
- Form fields have `<label>`s.
- Dropdown menus and modals trap focus and close on `Escape`.
- Colour is never the only signal — pair with text / icon / shape.

## Browser support

Target: latest stable Chrome / Safari / Firefox at the time of building. No IE / legacy support.

## Don't

- Don't add npm / build steps.
- Don't introduce React.
- Don't introduce a new font.
- Don't expand the colour palette beyond the locked tokens.
- Don't create new `/v1/...` endpoints.
- Don't write multi-line code comments. Naming + structure should be self-explanatory. One short why-comment per non-obvious workaround is fine.
- Don't write verbose docstrings on JS functions.
- Don't try to be clever about state management — direct mutation of `GIQ.state.*` is fine for this app's scale.

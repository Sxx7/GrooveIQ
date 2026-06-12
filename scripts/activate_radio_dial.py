#!/usr/bin/env python3
"""Activate the single-user radio dial on a running GrooveIQ instance.

The radio-dial capability (audit doc §8) ships in code, but the persisted
algorithm config predates the four new ``PresetConfig`` fields, so they load as
their no-op *field* defaults (``0.0`` / ``0.6``) and radio runs exactly as before.
This script publishes a new config version that carries the §8.4 per-tier values.

It is a **surgical merge**: it reads the active config, sets *only* the four new
fields per preset, and re-publishes — every other live value (the owner's tuned
``novelty_weight``, ``novelty_strength``, source multipliers, etc.) is preserved.
Re-running is a no-op once the values are in place (idempotent).

Usage::

    GROOVEIQ_API_KEY=<admin-key> python3 scripts/activate_radio_dial.py
    GROOVEIQ_URL=http://<grooveiq-host>:8000 GROOVEIQ_API_KEY=<admin-key> \
        python3 scripts/activate_radio_dial.py

Env:
    GROOVEIQ_URL      base URL (default ``http://localhost:8000``)
    GROOVEIQ_API_KEY  bearer token — must be an **admin** key
                      (``PUT /v1/algorithm/config`` is admin-gated)

Ordering: deploy the radio-dial code *first*, then run this — ``PUT`` validates
against the live schema, so the new fields only "take" on the new code.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

# §8.4 per-tier values for the four radio-dial levers. Only these fields are
# touched; everything else in the persisted config is preserved verbatim.
TARGETS: dict[str, dict[str, float]] = {
    "familiar":       {"proven_recall_mult": 1.5, "ranker_blend": 0.80, "familiarity_weight": 0.40, "cooldown_alpha": 0.35},
    "balanced":       {"proven_recall_mult": 0.8, "ranker_blend": 0.65, "familiarity_weight": 0.0,  "cooldown_alpha": 0.40},
    "discovery":      {"proven_recall_mult": 0.3, "ranker_blend": 0.50, "familiarity_weight": 0.0,  "cooldown_alpha": 0.25},
    "deep_discovery": {"proven_recall_mult": 0.0, "ranker_blend": 0.40, "familiarity_weight": 0.0,  "cooldown_alpha": 0.15},
}
_FIELDS = ["proven_recall_mult", "ranker_blend", "familiarity_weight", "cooldown_alpha"]


def _request(method: str, url: str, api_key: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {api_key}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        sys.exit(f"ERROR: {method} {url} -> HTTP {e.code}\n{detail}")
    except urllib.error.URLError as e:
        sys.exit(f"ERROR: {method} {url} -> {e.reason} (is GROOVEIQ_URL reachable?)")


def main() -> None:
    base = os.environ.get("GROOVEIQ_URL", "http://localhost:8000").rstrip("/")
    api_key = os.environ.get("GROOVEIQ_API_KEY")
    if not api_key:
        sys.exit("ERROR: set GROOVEIQ_API_KEY (an admin bearer token) in the environment.")

    cfg_url = f"{base}/v1/algorithm/config"

    active = _request("GET", cfg_url, api_key)
    config = active["config"]
    modes = config.get("modes")
    if not isinstance(modes, dict):
        sys.exit("ERROR: active config has no 'modes' group — is this the radio-dial code?")

    # Snapshot the before-values and apply the surgical patch.
    before: dict[str, dict[str, float]] = {}
    changed = False
    for preset, targets in TARGETS.items():
        if preset not in modes:
            sys.exit(f"ERROR: preset '{preset}' missing from config['modes'] — unexpected schema.")
        before[preset] = {f: modes[preset].get(f) for f in _FIELDS}
        for field, value in targets.items():
            if modes[preset].get(field) != value:
                modes[preset][field] = value
                changed = True

    if not changed:
        print(f"Radio dial already active at v{active['version']} — no change needed.")
        _print_table(before, before)
        return

    updated = _request("PUT", cfg_url, api_key, {"name": "radio dial activation", "config": config})
    after = {p: {f: updated["config"]["modes"][p].get(f) for f in _FIELDS} for p in TARGETS}

    print(f"Activated radio dial: v{active['version']} -> v{updated['version']}")
    _print_table(before, after)


def _print_table(before: dict, after: dict) -> None:
    width = max(len(p) for p in TARGETS)
    header = f"{'preset':<{width}}  " + "  ".join(f"{f:>18}" for f in _FIELDS)
    print(header)
    print("-" * len(header))
    for preset in TARGETS:
        cells = []
        for f in _FIELDS:
            b, a = before[preset][f], after[preset][f]
            cells.append(f"{b} -> {a}" if b != a else f"{a}")
        print(f"{preset:<{width}}  " + "  ".join(f"{c:>18}" for c in cells))


if __name__ == "__main__":
    main()

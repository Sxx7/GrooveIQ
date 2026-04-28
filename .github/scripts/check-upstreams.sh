#!/usr/bin/env bash
# Check upstream GitHub releases and post / update tracking issues.
# Driven by the upstream-watcher.yml workflow. To add a new upstream,
# append a row to UPSTREAMS below.
#
# Format per row: <owner/repo>|<friendly_name>|<label>
# - The label is auto-created if missing.
# - Each upstream gets exactly one tracking issue (filtered by label).
# - Issue title encodes the last-seen tag: "[upstream] <name> @ <tag>".
# - On a new release, the workflow comments with the changelog snippet
#   and updates the title's tag.
# - Closing a tracking issue stops the watcher for that upstream
#   (next run will re-create the issue with the current latest tag).

set -euo pipefail

declare -a UPSTREAMS=(
  "Lidarr/Lidarr|Lidarr|upstream-lidarr"
  "nathom/streamrip|streamrip|upstream-streamrip"
  "spotDL/spotify-downloader|spotDL|upstream-spotdl"
)

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

for entry in "${UPSTREAMS[@]}"; do
  IFS='|' read -r repo name label <<< "$entry"
  echo "::group::$name ($repo)"

  if ! gh api "repos/$repo/releases/latest" > "$WORK/latest.json" 2>/dev/null; then
    echo "  unreachable or no published releases — skipping"
    echo "::endgroup::"
    continue
  fi

  latest_tag=$(jq -r '.tag_name' "$WORK/latest.json")
  latest_url=$(jq -r '.html_url' "$WORK/latest.json")
  jq -r '.body // "(no release notes)"' "$WORK/latest.json" \
    | head -c 5000 > "$WORK/notes.md"
  echo "  latest: $latest_tag"

  # Ensure label exists (idempotent)
  gh label create "$label" \
    --description "Upstream release tracker" \
    --color BFE5BF >/dev/null 2>&1 || true

  existing=$(gh issue list \
    --label "$label" \
    --state open \
    --json number,title \
    --jq 'first // empty')

  if [ -z "$existing" ]; then
    echo "  no tracking issue; creating"
    cat > "$WORK/body.md" <<EOF
Tracking releases of [\`$repo\`](https://github.com/$repo/releases).

**Current:** \`$latest_tag\` · [Release notes]($latest_url)

The \`upstream-watcher\` workflow runs every Monday and posts a comment
here whenever this project ships a new release. Read the changelog,
decide whether GrooveIQ needs to adapt, and act if it does.

Close this issue to stop watching this upstream — the next workflow
run will recreate it pinned to whatever tag is current at that point.
EOF
    gh issue create \
      --title "[upstream] $name @ $latest_tag" \
      --label "$label" \
      --body-file "$WORK/body.md" >/dev/null
    echo "  ✓ created"
  else
    issue_num=$(jq -r '.number' <<< "$existing")
    issue_title=$(jq -r '.title' <<< "$existing")
    current_tag="${issue_title##*@ }"

    if [ "$current_tag" = "$latest_tag" ]; then
      echo "  unchanged ($current_tag)"
    else
      echo "  bump: $current_tag → $latest_tag"
      cat > "$WORK/comment.md" <<EOF
## $latest_tag released

[Release notes]($latest_url) — was \`$current_tag\`

<details>
<summary>Changelog snippet</summary>

$(cat "$WORK/notes.md")
</details>
EOF
      gh issue comment "$issue_num" --body-file "$WORK/comment.md" >/dev/null
      gh issue edit "$issue_num" --title "[upstream] $name @ $latest_tag" >/dev/null
      echo "  ✓ commented + retitled #$issue_num"
    fi
  fi

  echo "::endgroup::"
done

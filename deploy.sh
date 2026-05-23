#!/usr/bin/env bash
# deploy.sh — push the freshly-scraped times.json to GitHub Pages.
#
# Designed to be invoked from refresh.sh after a successful scrape. Exits 0
# even on push failures so the alert pipeline still runs — networking glitches
# shouldn't break the local workflow.
#
# Auth: relies on a personal access token cached in the macOS keychain by
# git's osxkeychain credential helper. The token is entered ONCE manually
# (the first time `git push` is run interactively); after that, launchd can
# push silently.

set -uo pipefail
cd "$(dirname "$0")"

# Only push if anything actually changed (avoid empty commits).
if git diff --quiet -- times.json 2>/dev/null && git diff --cached --quiet -- times.json 2>/dev/null; then
    # Nothing to deploy
    exit 0
fi

# Quiet log line so it's visible in .refresh.log without dominating
echo "[$(date '+%Y-%m-%d %H:%M:%S')] → deploy: pushing times.json to origin"

git add times.json

# Use a stable commit message so the history is browsable. Date in ISO + scrape
# time + slot count for at-a-glance grep.
SLOTS=$( ./.venv/bin/python -c "import json; print(len(json.load(open('times.json'))['tee_times']))" 2>/dev/null || echo "?" )
MSG="auto: refresh $(date '+%Y-%m-%d %H:%M:%S') ($SLOTS slots)"

git commit -m "$MSG" -q

# Push, but don't fail the parent script if the push fails (network blip,
# rebase needed, etc). The next cycle will retry.
if ! git push origin main 2>&1 | tail -3; then
    echo "  ⚠ push failed; will retry next cycle"
fi

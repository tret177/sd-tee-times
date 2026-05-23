#!/usr/bin/env bash
# refresh.sh — run the scraper and drop the result next to the dashboard.
#
# Designed to be invoked from a terminal OR from launchd/cron (no shell init
# required — uses the venv's python directly).
#
# Usage:
#   ./refresh.sh                          # tomorrow, 4 players
#   ./refresh.sh 2026-05-20               # specific date
#   ./refresh.sh 2026-05-20 2             # specific date + player count
#
# Background scheduling: see com.tret177.teetimes.plist for the launchd setup
# that runs this every 15 minutes.

set -euo pipefail
cd "$(dirname "$0")"

# Args:
#   $1 (optional): scrape window — either a single ISO date (back-compat) OR
#                  an integer N meaning "the next N days" (default: 14).
#   $2 (optional): players (default: 4).
WINDOW="${1:-14}"
PLAYERS="${2:-4}"
DASHBOARD_DIR="${DASHBOARD_DIR:-.}"

# Use the venv's python by absolute path so this works from launchd/cron where
# no venv is activated.
PY="./.venv/bin/python"

# If WINDOW is a pure integer, treat it as --days N. Otherwise treat it as a
# single ISO date for backward compatibility with ./refresh.sh 2026-06-01.
if [[ "$WINDOW" =~ ^[0-9]+$ ]]; then
    SCRAPE_ARG="--days $WINDOW"
    LABEL="next $WINDOW days"
else
    SCRAPE_ARG="--date $WINDOW"
    LABEL="$WINDOW"
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] → Scraping $LABEL for $PLAYERS players..."
"$PY" scraper.py $SCRAPE_ARG --players "$PLAYERS" --json > "$DASHBOARD_DIR/times.json.tmp"

# Snapshot the previous successful scrape before overwriting, so alert.py
# can diff (current vs previous) and only notify on newly-matching slots.
if [ -f "$DASHBOARD_DIR/times.json" ]; then
    cp "$DASHBOARD_DIR/times.json" "$DASHBOARD_DIR/.times.prev.json"
fi

# Atomic move so the dashboard never reads a half-written file
mv "$DASHBOARD_DIR/times.json.tmp" "$DASHBOARD_DIR/times.json"

# Quick summary
"$PY" -c "
import json
d = json.load(open('$DASHBOARD_DIR/times.json'))
print(f\"  ✓ {len(d['tee_times'])} tee times saved to times.json\")
if d['errors']:
    print(f\"  ⚠ {len(d['errors'])} errors:\")
    for e in d['errors']:
        print(f\"    · {e}\")
"

# Fire alerts (silent on the happy path — only logs when something is sent
# or when something's misconfigured).
if [ -f "./alert.py" ]; then
    "$PY" alert.py --current "$DASHBOARD_DIR/times.json" --previous "$DASHBOARD_DIR/.times.prev.json" || true
fi

# Push the fresh times.json to GitHub Pages (no-op until deploy.sh is wired
# up to a real remote). Never let a deploy failure break the local cycle.
if [ -x "./deploy.sh" ]; then
    ./deploy.sh || true
fi

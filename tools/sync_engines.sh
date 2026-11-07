#!/bin/bash
# Copy the analysis engines from the desktop app (unchanged) into the Android app's Python sources.
# Run after changing them on the Mac:  tools/sync_engines.sh [path-to-desktop-repo]
set -euo pipefail
SRC="${1:-$HOME/projects/Analyse-NSE-Indices-For-A-month}"
DST="$(cd "$(dirname "$0")/.." && pwd)/app/src/main/python"
FILES="config.py data_fetcher.py yahoo_lite.py sqlite_store.py market_hours.py shoonya_feed.py market_data.py
options.py options_service.py indicators.py risk_engine.py book_risk.py market_watch.py watch_service.py
news_service.py fii_analysis.py banknifty_service.py trades_service.py trades_engine.py api_routes.py"
for f in $FILES; do cp "$SRC/$f" "$DST/$f"; done
( cd "$SRC" && echo "synced from $(git rev-parse --short HEAD 2>/dev/null || echo '?') on $(date '+%Y-%m-%d %H:%M')" ) > "$DST/ENGINES_VERSION"
echo "Copied $(echo $FILES | wc -w | tr -d ' ') engine files from $SRC"

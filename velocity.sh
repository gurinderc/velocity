#!/bin/bash
# velocity.sh
# Runs the Velocity tracker then commits and pushes the report branch to GitHub.
# GitHub Pages serves the report branch at yourusername.github.io/velocity

set -euo pipefail

SCRIPT_DIR="$HOME/velocity"
REPO="$HOME/repos/velocity"          # single repo, checked out on report branch

LOG="[velocity $(date '+%Y-%m-%d %H:%M')]"
echo "$LOG Starting"

# Load API key from .env file if present
# Format: GROQ_API_KEY=gsk_...
[ -f "$SCRIPT_DIR/.env" ] && { set -a; source "$SCRIPT_DIR/.env"; set +a; }

# Run tracker — outputs directly to repo root (report branch)
python3 "$SCRIPT_DIR/velocity.py" \
    --db         "$SCRIPT_DIR/velocity.db" \
    --report-dir "$REPO"

echo "$LOG Tracker done"

# Commit and push report branch
cd "$REPO"
git add .
git diff --staged --quiet && { echo "$LOG No changes — skipping push"; exit 0; }
git commit -m "Velocity $(date '+%Y-%m-%d')"
git push origin report

echo "$LOG Pushed — GitHub Pages updating"

#!/bin/bash
# Run AFTER `gh auth login`. Creates a PRIVATE GitHub repo and wires it as the
# 'origin' remote of the VM workspace so the sync_github MCP tool can push.
# Firefox logins/cookies are NOT synced — only /data/workspace files.
set -euo pipefail

REPO="${1:-chatgpt-compvm-data}"
WS=~/chatgpt-compvm/data/workspace

if ! gh auth status >/dev/null 2>&1; then
  echo "GitHub not authenticated. Run:  gh auth login   then re-run this script." >&2
  exit 1
fi

cd "$WS"
git add -A
git diff --cached --quiet || git commit -q -m "initial workspace"

if git remote get-url origin >/dev/null 2>&1; then
  echo "origin already set: $(git remote get-url origin)"
else
  # Create the repo private and push. gh sets the remote for us.
  gh repo create "$REPO" --private --source=. --remote=origin --push
fi
echo "GitHub sync ready. Repo: $(git remote get-url origin)"

#!/bin/bash
# Boot/self-heal helper (run as ExecStartPre of the MCP service): make sure the
# 50GB /data volume is mounted, then make sure the VM container is running — WITHOUT
# recreating a healthy one. Never fails hard, so it can't block the MCP server.
DATA=~/chatgpt-compvm/data
CONT="${COMP_CONTAINER:-chatgpt-compvm}"

mountpoint -q "$DATA" || sudo mount "$DATA" 2>/dev/null || true

state=$(docker inspect -f '{{.State.Status}}' "$CONT" 2>/dev/null)
case "$state" in
  running)                 : ;;                                   # already up
  exited|created|paused)   docker start "$CONT" >/dev/null 2>&1 || true ;;
  *)                       bash ~/chatgpt-compvm/run-container.sh >/dev/null 2>&1 || true ;;
esac
exit 0

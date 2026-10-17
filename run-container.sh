#!/bin/bash
# (Re)create the disposable VM container. Called at first start and by reset_vm().
# Honours COMP_WIPE=1 to erase /data for a fully fresh machine.
set -uo pipefail

DIR=~/chatgpt-compvm
DATA="$DIR/data"
CONT="${COMP_CONTAINER:-chatgpt-compvm}"
VNC_PORT="${COMP_VNC_PORT:-8977}"

docker rm -f "$CONT" >/dev/null 2>&1 || true

if [ "${COMP_WIPE:-0}" = "1" ]; then
  echo "wiping $DATA ..."
  sudo find "$DATA" -mindepth 1 -maxdepth 1 -exec rm -rf {} + 2>/dev/null || true
fi
mkdir -p "$DATA"

docker run -d --name "$CONT" \
  --memory 2g --memory-swap 2g --shm-size 512m \
  --restart always \
  -e SCREEN="${COMP_SCREEN:-1440x900x24}" \
  -v "$DATA":/data \
  -p 127.0.0.1:"${VNC_PORT}":5900 \
  chatgpt-compvm:latest >/dev/null

sleep 1
if [ "$(docker inspect -f '{{.State.Running}}' "$CONT" 2>/dev/null)" = "true" ]; then
  echo "started $CONT (2GB RAM, /data 50GB, VNC on 127.0.0.1:$VNC_PORT)"
else
  echo "FAILED to start $CONT"; docker logs --tail 20 "$CONT" 2>&1 || true; exit 1
fi

#!/bin/bash
set -euo pipefail

# Deploy qemu + ycmd + nginx to Hetzner (production emulator + code completion)
# Config is read from .env (QEMU_SERVER, QEMU_SSH_KEY)
# Usage: ./deploy_hetzner.sh [--no-cache]

NO_CACHE=""
if [[ "${1:-}" == "--no-cache" ]]; then
  NO_CACHE="--no-cache"
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Load .env
if [ -f "$SCRIPT_DIR/.env" ]; then
  set -a
  source "$SCRIPT_DIR/.env"
  set +a
fi

: "${QEMU_SERVER:?Set QEMU_SERVER in .env (e.g. root@1.2.3.4)}"
: "${QEMU_SSH_KEY:?Set QEMU_SSH_KEY in .env (e.g. ~/.ssh/id_exe)}"

SSH="ssh -i $QEMU_SSH_KEY $QEMU_SERVER"

echo "==> Syncing code to $QEMU_SERVER..."
rsync -avz --delete \
  --exclude='.git' \
  --exclude='.env' \
  --exclude='.env.local' \
  --exclude='.DS_Store' \
  -e "ssh -i $QEMU_SSH_KEY" \
  "$SCRIPT_DIR/" "$QEMU_SERVER":~/cloudpebble/

echo "==> Building images..."
$SSH "cd ~/cloudpebble && docker compose --profile emulator --profile codecomplete build $NO_CACHE qemu ycmd && docker compose build $NO_CACHE nginx"

echo "==> Restarting services..."
$SSH "cd ~/cloudpebble && docker compose --profile emulator --profile codecomplete down && docker compose --profile emulator --profile codecomplete up -d"

echo "==> Waiting for services to start..."
sleep 3

echo "==> Container status:"
$SSH "cd ~/cloudpebble && docker compose --profile emulator --profile codecomplete ps"

echo ""
echo "==> Deploy complete."

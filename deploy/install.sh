#!/usr/bin/env bash
# SENTRA Backend — Pi 5 install/bootstrap script (Phase 19)
#
# Run ON the Raspberry Pi 5, from the repo root:
#   sudo bash deploy/install.sh
#
# What it does:
#   1. creates the `sentra` system user (with gpio/i2c/spi access)
#   2. copies the backend to /opt/sentra
#   3. creates a virtualenv and installs requirements.txt
#   4. installs .env (created from deploy/env.template if absent) and
#      deploy/sentra-backend.service into systemd
#   5. enables + starts the service and waits for the health check
#
# Idempotent: safe to re-run; the .env is never overwritten.

set -euo pipefail

DEST=/opt/sentra
SVC=sentra-backend.service
RUN_USER=sentra

if [[ $EUID -ne 0 ]]; then
  echo "error: run with sudo (needs useradd, systemctl, /opt writes)" >&2
  exit 1
fi

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
echo "==> installing from ${SRC_DIR} to ${DEST}"

# ── 1. system user with hardware access ──────────────────────────────────────
if ! id "$RUN_USER" &>/dev/null; then
  useradd --system --create-home --home-dir "$DEST" --shell /usr/sbin/nologin "$RUN_USER"
  echo "==> created system user $RUN_USER"
fi
usermod -aG gpio,i2c,spi,video "$RUN_USER" 2>/dev/null \
  || echo "warn: gpio/i2c/spi/video groups missing (run under Raspberry Pi OS with interfaces enabled)"

# ── 2. copy backend ──────────────────────────────────────────────────────────
mkdir -p "$DEST" ~/sentra_data
rsync -a --delete \
  --exclude ".git" --exclude "__pycache__" --exclude "venv" --exclude ".env" \
  "$SRC_DIR"/ "$DEST"/
chown -R "$RUN_USER:$RUN_USER" "$DEST"

# ── 3. virtualenv ────────────────────────────────────────────────────────────
if [[ ! -x "$DEST/venv/bin/python" ]]; then
  python3 -m venv "$DEST/venv"
fi
"$DEST/venv/bin/pip" install --upgrade pip >/dev/null
"$DEST/venv/bin/pip" install -r "$DEST/requirements.txt"
chown -R "$RUN_USER:$RUN_USER" "$DEST/venv"

# ── 4. env + systemd unit ────────────────────────────────────────────────────
if [[ ! -f "$DEST/.env" ]]; then
  cp "$SRC_DIR/deploy/env.template" "$DEST/.env"
  # refuse to boot with the shipped JWT default: generate a real secret now
  JWT=$(python3 -c "import secrets; print(secrets.token_hex(32))")
  sed -i "s|<long-random-secret>|$JWT|" "$DEST/.env"
  echo "==> created $DEST/.env (JWT secret auto-generated)"
else
  echo "==> keeping existing $DEST/.env"
fi
install -m 644 "$SRC_DIR/deploy/$SVC" "/etc/systemd/system/$SVC"
systemctl daemon-reload

# ── 5. enable + start + wait for health ──────────────────────────────────────
systemctl enable --now "$SVC"
echo "==> waiting for /api/v1/ping ..."
for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8080/api/v1/ping >/dev/null 2>&1; then
    echo "==> SENTRA backend is UP: $(curl -fsS http://127.0.0.1:8080/api/v1/ping)"
    echo "==> next: edit $DEST/.env (relay URL, unit id), then: sudo systemctl restart $SVC"
    exit 0
  fi
  sleep 2
done
echo "error: service did not become healthy in 60 s — journalctl -u $SVC -n 50" >&2
exit 1

# SENTRA Deployment (Phase 19)

Two deployment targets: the **Pi 5 unit** (this backend) and the optional
**relay VPS** (Phase 16 broker for remote access). The mode (REAL vs
SIMULATION) is fixed at process start via `.env` — see Phase 18.

## A. Pi 5 unit

```bash
# on the Pi, from the repo root
sudo bash deploy/install.sh
```

The script: creates the `sentra` system user (gpio/i2c/spi groups), copies the
backend to `/opt/sentra`, builds `venv`, generates a real JWT secret into a
fresh `.env`, installs the systemd unit, enables + starts it, and waits for
`/api/v1/ping`.

Manual equivalent:

```bash
sudo useradd --system --create-home --home-dir /opt/sentra --shell /usr/sbin/nologin sentra
sudo usermod -aG gpio,i2c,spi sentra
sudo rsync -a --exclude .git --exclude __pycache__ --exclude venv --exclude .env ./ /opt/sentra/
cd /opt/sentra && sudo python3 -m venv venv && sudo ./venv/bin/pip install -r requirements.txt
sudo cp deploy/env.template /opt/sentra/.env       # then edit: secrets, relay, unit id
sudo cp deploy/sentra-backend.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now sentra-backend.service
```

Operations:

```bash
systemctl status sentra-backend
journalctl -u sentra-backend -f          # live logs (banner, preflight, events)
curl -s http://127.0.0.1:8080/api/v1/system/status | python3 -m json.tool
curl -s http://127.0.0.1:8080/api/v1/simulation
```

Updates: `git pull && sudo bash deploy/install.sh` (idempotent; `.env` kept).

## B. Relay VPS (optional — remote access)

```bash
sudo useradd --system --home /opt/sentra-relay --shell /usr/sbin/nologin sentra-relay
sudo mkdir -p /opt/sentra-relay && sudo chown sentra-relay:sentra-relay /opt/sentra-relay
# copy repo → /opt/sentra-relay, then:
cd /opt/sentra-relay && python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
echo 'SENTRA_RELAY_UNIT_SECRET=<long-random-secret>' | sudo tee /opt/sentra-relay/.env
sudo cp deploy/sentra-relay.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now sentra-relay.service
```

TLS: terminate `wss://` at nginx/Caddy and proxy to `127.0.0.1:8765` (the unit
binds loopback via `--host 127.0.0.1`). Point the Pi's `SENTRA_RELAY_URL` at
the public `wss://` URL and put the same secret in both `.env` files.

Scaling: the relay is stateless — run N replicas behind the LB, move the
in-memory unit/app registries to Redis pub/sub (protocol unchanged, §32).

## Production checklist

- [ ] `JWT_SECRET_KEY` overridden (never the shipped default)
- [ ] `SENTRA_SIMULATION=false` only with hardware attached; `true` for dev/emulator
- [ ] Relay secret differs from the JWT secret; relay on `wss://` in production
- [ ] `~/sentra_data/` on persistent storage (devices, notifications, patrol routes)
- [ ] `journalctl -u sentra-backend` shows `Mode: REAL` or the SIMULATION banner as intended
- [ ] No `SECURITY: [...]` WARNING lines at startup (JWT/relay secrets set, auth on) — Phase 20 checks log findings loudly
- [ ] Firewall: unit exposes 8080/8888 on LAN only; relay VPS exposes 443 only

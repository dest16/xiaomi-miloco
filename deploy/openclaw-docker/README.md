# Dockerized OpenClaw development initialization

This override keeps MiLoCo in the existing OpenClaw runtime, matching the
plugin's native `miloco-cli service restart/stop` lifecycle. It does not add a
second backend container or change the existing OpenClaw network, ports,
gateway token, config, workspace, or auth mounts.

The current official OpenClaw runtime already contains Python 3, curl, git,
npm, and pnpm. The thin derivative image adds only `rsync`, which
`scripts/build.sh` uses while staging `miloco-miot`.

## Persistent paths

- `/home/node/.openclaw` remains the existing OpenClaw state mount. MiLoCo uses
  `/home/node/.openclaw/miloco` for `config.json`, databases, models, logs, and
  other runtime data.
- `/home/node/.local` must be mounted persistently. The official installer puts
  `uv` and command shims in `.local/bin`; uv tool environments, downloaded
  Python runtimes, and the writable fork mirror are stored below `.local`.
- The host fork checkout is mounted read-only at
  `/opt/xiaomi-miloco-source`. It is copied to
  `/home/node/.local/src/xiaomi-miloco` before each dev installation so the
  build can write `dist`, package-manager state, and temporary build files
  without changing host ownership or polluting the checkout.

## Apply the override

Run these commands on the Unraid host from the directory containing the
existing official OpenClaw `docker-compose.yml`. Replace the two `/path/...`
values and edit the copied env file before building.

```bash
cp /path/to/xiaomi-miloco/deploy/openclaw-docker/.env.example ./miloco-dev.env
vi ./miloco-dev.env

# Create the bind-mounted user-local directory for OpenClaw's uid 1000.
# Use the OPENCLAW_LOCAL_DIR value selected in miloco-dev.env.
mkdir -p /mnt/user/appdata/openclaw/.local
chown -R 1000:1000 /mnt/user/appdata/openclaw/.local

docker compose --env-file ./miloco-dev.env \
  -f ./docker-compose.yml \
  -f /path/to/xiaomi-miloco/deploy/openclaw-docker/compose.override.yml \
  stop openclaw-gateway

docker compose --env-file ./miloco-dev.env \
  -f ./docker-compose.yml \
  -f /path/to/xiaomi-miloco/deploy/openclaw-docker/compose.override.yml \
  build openclaw-gateway

docker compose --env-file ./miloco-dev.env \
  -f ./docker-compose.yml \
  -f /path/to/xiaomi-miloco/deploy/openclaw-docker/compose.override.yml \
  up -d openclaw-gateway
```

The override adds volumes; Compose retains the existing `/home/node/.openclaw`,
workspace, auth, and `ai-core` network configuration from the base file.

## Install this fork

First verify the read-only checkout, then create an exact writable mirror and
run the repository's official interactive dev installer. Do not use the Xiaomi
release installer URL: that would install upstream instead of this fork.

```bash
docker compose --env-file ./miloco-dev.env \
  -f ./docker-compose.yml \
  -f /path/to/xiaomi-miloco/deploy/openclaw-docker/compose.override.yml \
  exec openclaw-gateway test -f /opt/xiaomi-miloco-source/scripts/install.sh

docker compose --env-file ./miloco-dev.env \
  -f ./docker-compose.yml \
  -f /path/to/xiaomi-miloco/deploy/openclaw-docker/compose.override.yml \
  exec openclaw-gateway bash -lc '
    mkdir -p /home/node/.local/src/xiaomi-miloco &&
    rsync -a --delete \
      --exclude=.venv/ --exclude=backend/.venv/ --exclude=node_modules/ \
      --exclude=dist/ --exclude=build/ --exclude=__pycache__/ \
      /opt/xiaomi-miloco-source/ /home/node/.local/src/xiaomi-miloco/ &&
    cd /home/node/.local/src/xiaomi-miloco &&
    bash scripts/install.sh --dev
  '
```

Complete the official interactive Mi Home account binding and Omni model
configuration. The installer builds all fork packages, installs `miloco`,
`miloco-cli`, and supervisor with uv, and installs the generated OpenClaw
plugin. It intentionally stops the temporary backend when installation exits.

Start MiLoCo and restart the gateway so the freshly installed plugin is loaded:

```bash
docker compose --env-file ./miloco-dev.env \
  -f ./docker-compose.yml \
  -f /path/to/xiaomi-miloco/deploy/openclaw-docker/compose.override.yml \
  exec openclaw-gateway miloco-cli service start

docker compose --env-file ./miloco-dev.env \
  -f ./docker-compose.yml \
  -f /path/to/xiaomi-miloco/deploy/openclaw-docker/compose.override.yml \
  restart openclaw-gateway
```

## Configure external RTSP video

The value is one JSON array. Identity is an exact physical DID and channel;
channels not listed continue to use MIoT video.

```bash
docker compose --env-file ./miloco-dev.env \
  -f ./docker-compose.yml \
  -f /path/to/xiaomi-miloco/deploy/openclaw-docker/compose.override.yml \
  exec openclaw-gateway miloco-cli config set camera.external_streams \
  '[{"physical_did":"<CAMERA_1_DID>","channel":0,"url":"rtsp://10.10.0.1:8554/camera1"},{"physical_did":"<CAMERA_2_DID>","channel":0,"url":"rtsp://10.10.0.1:8554/camera2"}]'
```

`config set` restarts a running backend. External RTSP credentials and query
tokens are redacted by `config show`, `config get`, set results, and runtime
logs. `config show --unmasked` is the existing explicit debugging escape hatch.

## Verify

```bash
docker compose --env-file ./miloco-dev.env \
  -f ./docker-compose.yml \
  -f /path/to/xiaomi-miloco/deploy/openclaw-docker/compose.override.yml \
  exec openclaw-gateway bash -lc '
    openclaw --version &&
    openclaw plugins list &&
    miloco-cli --version &&
    miloco-cli service status &&
    miloco-cli config show &&
    miloco-cli device list &&
    curl --fail --silent http://127.0.0.1:1810/health
  '

docker compose --env-file ./miloco-dev.env \
  -f ./docker-compose.yml \
  -f /path/to/xiaomi-miloco/deploy/openclaw-docker/compose.override.yml \
  exec openclaw-gateway miloco-cli service logs --lines 300
```

For each override, logs must show the external-source selection and RTSP
connection. They must not show an MIoT video subscription for that exact DID
and channel. Disconnecting RTSP should produce RTSP reconnect messages while
the source-selection decision remains external; it never falls back to MIoT.

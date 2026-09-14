# OpenClaw runtime with an isolated MiLoCo builder

This deployment keeps the existing official OpenClaw runtime as the pinned base.
MiLoCo is built in a separate, disposable builder container. The builder
writes a complete `dist/` bundle to the host; the runtime sees only that bundle
and the official installer scripts, both read-only.

A thin local runtime layer installs Debian's FFmpeg, VAAPI userspace library,
and Mesa driver. This is required because PyAV's bundled FFmpeg does not expose
VAAPI and the official OpenClaw image has no `ffmpeg` executable. No OpenClaw
files are replaced.

No second MiLoCo backend container is introduced. The existing OpenClaw state,
workspace, auth, network, ports, and gateway token continue to come from the
base Compose file.

## Why the split is required

`scripts/build.sh` requires `rsync`, Python, uv, npm, and pnpm. Those are build
dependencies, not OpenClaw runtime dependencies. The previous derivative image
installed `rsync` into OpenClaw and then built the fork inside the runtime.
The builder remains isolated. The runtime layer added here contains only media
runtime packages; it never runs the MiLoCo source build.

`scripts/install.py --local-dist` performs the same install, model extraction,
service initialization, account binding, model configuration, and plugin
installation as the normal official installer. It only changes the artifact
source: it uses the adjacent `dist/`, skips `scripts/build.sh`, and never
downloads a release fallback. Before changing the installation it requires the
platform-specific MIoT wheel, MiLoCo wheel, CLI wheel, models archive, and
OpenClaw plugin package.

## Persistent paths

- The base deployment's `/home/node/.openclaw` mount remains authoritative for
  OpenClaw and MiLoCo runtime state. MiLoCo uses
  `/home/node/.openclaw/miloco`.
- `${OPENCLAW_LOCAL_DIR}` is mounted at `/home/node/.local` and persists uv,
  downloaded Python runtimes, command shims, and uv tool environments.
- `${MILOCO_DIST_DIR}` is the builder's persistent output. It is writable only
  in the builder and read-only at `/opt/xiaomi-miloco-installer/dist` in
  OpenClaw.
- Only `${MILOCO_SOURCE_DIR}/scripts` is mounted into OpenClaw, read-only. The
  fork source tree is never mounted into the runtime.

## One-time setup

Run from the directory containing the existing official OpenClaw
`docker-compose.yml`. Replace `/path/to/xiaomi-miloco`, copy the example, then
set `OPENCLAW_IMAGE` to the exact official image reference already used by the
deployment. Do not use `latest`. Set `MILOCO_BUILD_VERSION` to the official
baseline version used by the fork; this avoids ambiguous `0.0` development
versions when a deployment checkout has no Git tags.

```bash
cp /path/to/xiaomi-miloco/deploy/openclaw-docker/.env.example ./miloco.env
vi ./miloco.env

mkdir -p /mnt/user/appdata/xiaomi-miloco-dist
mkdir -p /mnt/user/appdata/openclaw/.local
chown -R 1000:1000 /mnt/user/appdata/xiaomi-miloco-dist
chown -R 1000:1000 /mnt/user/appdata/openclaw/.local

dc() {
  docker compose --env-file ./miloco.env \
    -f ./docker-compose.yml \
    -f /path/to/xiaomi-miloco/deploy/openclaw-docker/compose.override.yml \
    "$@"
}
```

Confirm that Compose resolves both OpenClaw services to the same pinned
official image before starting:

```bash
dc config --images
```

## Build flow

The source checkout is read-only in the builder. It is copied to disposable
container storage because the official `build.sh` legitimately writes package
metadata and temporary build files. Only the completed `dist/` is copied to
the host output directory.

```bash
dc --profile build build miloco-builder
dc --profile build run --rm miloco-builder

find /mnt/user/appdata/xiaomi-miloco-dist -maxdepth 1 -type f -printf '%f\n' | sort
```

The output must include a `miloco_miot` wheel matching the runtime
architecture, a `miloco` wheel, a `miloco_cli` wheel,
`miloco-models-*.tar.gz`, and `miloco-openclaw-plugin-*.tgz`.

## Start the unchanged runtime

```bash
dc up -d --force-recreate openclaw-gateway
```

The override builds `miloco-openclaw-runtime:local` from `Dockerfile.runtime`;
both OpenClaw services use that image. The gateway additionally receives only
the configured `${MILOCO_VAAPI_DEVICE}` render node.

## Install and initialize from local dist

Run the official installer interactively inside the running official image:

```bash
dc exec openclaw-gateway \
  bash /opt/xiaomi-miloco-installer/scripts/install.sh \
  --local-dist --agent-platform openclaw
```

Complete the normal Mi Home account binding and Omni model configuration.
This is the complete official initialization flow; it is not the reduced
`sync-to-remote.sh --install-only` path. The temporary MiLoCo service is
stopped when installation exits.

Start MiLoCo and restart OpenClaw so it loads the newly installed plugin:

```bash
dc exec openclaw-gateway miloco-cli service start
dc restart openclaw-gateway
```

## Configure external RTSP video

Existing camera configuration is unchanged. If this fork is already configured
for external streams, keep the current value. To set it for the first time:

```bash
dc exec openclaw-gateway miloco-cli config set camera.external_streams \
  '[{"physical_did":"<CAMERA_1_DID>","channel":0,"url":"rtsp://10.10.0.1:8554/camera1"}]'
```

Enable automatic VAAPI perception decode (with PyAV software fallback):

```bash
dc exec openclaw-gateway miloco-cli config set camera.rtsp_decode \
  '{"backend":"auto","ffmpeg_path":"ffmpeg","vaapi_device":"/dev/dri/renderD128"}'
```

Use the same render node configured as `MILOCO_VAAPI_DEVICE` in `miloco.env`.
Browser preview of external RTSP cameras is compressed H.264/H.265 passthrough;
the decoded path is used only by perception and temporary short-MP4 recording.

## Verify

```bash
dc exec openclaw-gateway bash -lc '
  openclaw --version &&
  openclaw plugins list &&
  miloco-cli --version &&
  miloco-cli service status &&
  curl --fail --silent http://127.0.0.1:1810/health &&
  MILOCO_PYTHON="$(miloco-cli config get server.python_bin --value-only)" &&
  "$MILOCO_PYTHON" -c "from miloco.perception.collect.rtsp_camera_stream import RtspCameraVideoStreamSource; from miloco.perception.collect.camera_stream_selector import ConfiguredCameraVideoStreamSource; print(\"fork imports ok\")"
'

dc exec openclaw-gateway miloco-cli service logs --lines 300
```

Also inspect the runtime rather than the builder when checking for leaked build
tools:

```bash
dc exec openclaw-gateway sh -lc '
  if command -v rsync >/dev/null 2>&1; then
    echo "unexpected: rsync exists in runtime" >&2
    exit 1
  fi
  echo "runtime has no rsync"
'
```

## Upgrade flow

1. Update the fork checkout on the host.
2. Rebuild the disposable builder and replace the persistent dist bundle.
3. Re-run the complete local-dist installer.
4. Restart OpenClaw.

```bash
git -C /mnt/user/appdata/xiaomi-miloco pull --ff-only
dc --profile build build --pull miloco-builder
dc --profile build run --rm miloco-builder
dc exec openclaw-gateway \
  bash /opt/xiaomi-miloco-installer/scripts/install.sh \
  --local-dist --agent-platform openclaw
dc exec openclaw-gateway miloco-cli service start
dc restart openclaw-gateway
```

Changing OpenClaw itself is a separate operation. When intentionally upgrading
it, first change `OPENCLAW_IMAGE` to the chosen exact official tag, then run:

```bash
dc pull openclaw-gateway openclaw-cli
dc up -d --force-recreate openclaw-gateway
```

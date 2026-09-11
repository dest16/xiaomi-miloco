FROM node:24-bookworm

USER root

# Build-only dependencies. None of these packages are added to OpenClaw runtime.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        python3 \
        rsync \
    && rm -rf /var/lib/apt/lists/* \
    && corepack enable \
    && corepack prepare pnpm@11.5.0 --activate \
    && curl -LsSf https://astral.sh/uv/install.sh \
        | env UV_INSTALL_DIR=/usr/local/bin sh

USER node
WORKDIR /tmp

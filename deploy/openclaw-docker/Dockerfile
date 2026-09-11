ARG OPENCLAW_IMAGE=ghcr.io/openclaw/openclaw:latest
FROM ${OPENCLAW_IMAGE}

USER root

# The official runtime already provides Python 3, curl, git, npm, and pnpm.
# MiLoCo's source build additionally uses rsync when staging miloco-miot.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends rsync \
    && rm -rf /var/lib/apt/lists/* \
    && install -d -m 0755 -o node -g node /home/node/.local/src

ENV PATH="/home/node/.local/bin:${PATH}"

USER node

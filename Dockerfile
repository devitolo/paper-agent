ARG PYTHON_BASE_IMAGE=python:3.12-slim
ARG NODE_BASE_IMAGE=node:22.23.2-bookworm-slim
FROM ${NODE_BASE_IMAGE} AS gemini-node
FROM ${PYTHON_BASE_IMAGE} AS base

ARG BUILD_DATE=unknown
ARG VCS_REF=unknown
ARG VERSION=dev

LABEL org.opencontainers.image.title="Project Paper" \
      org.opencontainers.image.description="Local-first research paper discovery and review app" \
      org.opencontainers.image.source="https://github.com/devitolo/paper-agent" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="${VERSION}"

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 paper \
    && mkdir -p /app/data /app/config \
    && chown paper:paper /app/data /app/config

COPY requirements.txt ./
RUN if [ -s requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; fi

COPY LICENSE NOTICE ./
COPY paper_agents ./paper_agents
COPY scripts ./scripts
COPY sql ./sql
COPY deploy/templates ./paper_agents/package_templates

USER paper
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["python", "-m", "paper_agents.package_runtime"]
CMD ["start"]

# Explicit opt-in target. Default final runtime retains fresh-install dependencies/settings.
FROM base AS migration
USER root
ARG PYTHON_BASE_IMAGE
ARG SOURCE_BUNDLE_SHA256=
ARG VCS_REF
LABEL org.projectpaper.source-bundle-sha256="${SOURCE_BUNDLE_SHA256}"
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    curl ca-certificates bash util-linux coreutils tzdata \
    && rm -rf /var/lib/apt/lists/*
COPY requirements-telemetry.txt ./
RUN pip install --no-cache-dir -r requirements-telemetry.txt \
    && python scripts/image_runtime_inventory.py "${PYTHON_BASE_IMAGE}" /app/image-runtime-inputs.json "${SOURCE_BUNDLE_SHA256}" "${VCS_REF}"
LABEL org.projectpaper.runtime="migration-foundations-unqualified"
USER paper

FROM migration AS migration-gemini
USER root
ARG NODE_BASE_IMAGE
COPY --from=gemini-node /usr/local/bin/node /usr/local/bin/node
COPY --from=gemini-node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
# Exact top-level version; first build resolves transitive dependencies into the
# retained lock, then installs that lock. Capture/review it before reproducible rebuilds.
RUN mkdir -p /opt/paper-gemini \
    && cd /opt/paper-gemini \
    && printf '%s\n' '{"name":"paper-gemini-runtime","private":true,"dependencies":{"@google/gemini-cli":"0.52.0"}}' > package.json \
    && node /usr/local/lib/node_modules/npm/bin/npm-cli.js install --package-lock-only --ignore-scripts --no-audit --no-fund \
    && node /usr/local/lib/node_modules/npm/bin/npm-cli.js ci --ignore-scripts --no-audit --no-fund \
    && node /usr/local/lib/node_modules/npm/bin/npm-cli.js ls --all --json > installed-dependencies.json \
    && cd /app \
    && python scripts/gemini_runtime_inventory.py "${NODE_BASE_IMAGE}"
LABEL org.projectpaper.runtime="migration-gemini-unqualified"
USER paper

FROM base AS runtime

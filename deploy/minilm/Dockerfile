# Build only on an approved development/CI builder. Supply a pinned Python3.12 base.
ARG MINILM_BASE_IMAGE
FROM ${MINILM_BASE_IMAGE}
WORKDIR /app
COPY requirements-minilm.txt /app/requirements-minilm.txt
COPY deploy/minilm/requirements-production.txt /app/requirements-production.txt
RUN pip install --no-cache-dir -r requirements-production.txt \
    && useradd --uid 10001 --create-home paper
COPY paper_agents /app/paper_agents
ARG VCS_REF=unknown
ARG SOURCE_BUNDLE_SHA256=
LABEL org.opencontainers.image.revision="${VCS_REF}" \
      org.projectpaper.source-bundle-sha256="${SOURCE_BUNDLE_SHA256}"
USER 10001:10001
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 TOKENIZERS_PARALLELISM=false \
    PAPER_AGENT_MINILM_MODEL_DIR=/models/minilm
ENTRYPOINT ["python", "-m", "paper_agents.minilm_service"]
CMD ["--host", "0.0.0.0", "--port", "8091"]

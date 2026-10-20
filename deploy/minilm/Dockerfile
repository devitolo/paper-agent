# Build only on an approved development/CI builder. Supply a pinned Python3.12 base.
ARG MINILM_BASE_IMAGE
FROM ${MINILM_BASE_IMAGE}
WORKDIR /app
COPY requirements-minilm.txt /app/requirements-minilm.txt
RUN pip install --no-cache-dir -r requirements-minilm.txt \
    && useradd --uid 10001 --create-home paper
COPY paper_agents /app/paper_agents
USER 10001:10001
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 TOKENIZERS_PARALLELISM=false \
    PAPER_AGENT_MINILM_MODEL_DIR=/models/minilm
ENTRYPOINT ["python", "-m", "paper_agents.minilm_service"]
CMD ["--host", "0.0.0.0", "--port", "8091"]

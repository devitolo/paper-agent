FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 paper \
    && mkdir -p /app/data /app/config \
    && chown paper:paper /app/data /app/config

COPY requirements.txt ./
RUN if [ -s requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; fi

COPY paper_agents ./paper_agents
COPY scripts ./scripts
COPY sql ./sql
COPY deploy/templates ./paper_agents/package_templates

USER paper
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["python", "-m", "paper_agents.package_runtime"]
CMD ["start"]

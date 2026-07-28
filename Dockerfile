FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN if [ -s requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; fi

COPY paper_agents ./paper_agents
COPY data ./data
COPY scripts ./scripts

ENTRYPOINT ["python", "-m", "paper_agents.cli"]

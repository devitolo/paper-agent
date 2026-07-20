# Personal Paper Agents

A tiny three-agent prototype for research-paper discovery and curation.

## What it does

1. `ResearchScout` searches arXiv for recent papers that match your interests, then asks OpenAI to score and explain the candidates.
2. `ResearchCurator` reviews the Scout's candidates and picks the best 2-3 papers to spend time on.
3. `FeedbackAgent` reads your natural-language feedback and updates `data/profile.json`.

The next run uses the updated profile.

## Setup

Add your API key to `.env`:

```bash
OPENAI_API_KEY=your_api_key
OPENAI_MODEL=gpt-5.1
```

### Docker setup

Build the container:

```bash
docker compose build
```

The `.env` file is mounted read-only into the container at runtime. It is not copied
into the Docker image.

## Run

Discover and curate papers:

```bash
docker compose run --rm paper-agents run
```

Give feedback after reviewing the recommendations:

```bash
docker compose run --rm paper-agents feedback "This was too theoretical. I want more practical real-world incident management papers."
```

Inspect the stored preference profile:

```bash
docker compose run --rm paper-agents profile
```

You can still run locally with Python if you want:

```bash
python3 -m paper_agents.cli run
```

## Project structure

- `paper_agents/cli.py`: command-line entry point.
- `paper_agents/scout.py`: Research Scout agent and arXiv discovery.
- `paper_agents/curator.py`: Research Curator agent.
- `paper_agents/feedback.py`: Feedback Agent.
- `paper_agents/openai_helpers.py`: small OpenAI Responses API wrapper.
- `paper_agents/store.py`: JSON profile loading and saving.
- `data/profile.json`: editable preference profile and feedback history.

This version intentionally avoids a UI, scheduler, database, vector store, and full-paper ingestion.

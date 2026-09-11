# Contributing to Project Paper

Project Paper is currently stabilizing its first local-first release. Small, focused fixes and documentation improvements are welcome.

Before opening a change:

1. Search existing issues and open a focused issue for behavior changes.
2. Keep the Scout → Curator → review → feedback workflow intact.
3. Do not change ranking behavior without a repeated, evidenced failure and an isolated evaluation plan.
4. Keep Topic Agent redesign, multi-user support, deep reading, new sources, and broad platform work out of unrelated changes.
5. Do not commit `.env`, databases, feedback, downloaded papers, model files, logs, or credentials.

Run the test suite:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q paper_agents tests
git diff --check
```

For packaging changes, also validate both Compose paths and the installer:

```bash
bash -n scripts/install_project_paper.sh
PAPER_APP_IMAGE=ghcr.io/devitolo/paper-agent:v0.1.0 PAPER_OLLAMA_IMAGE=ollama/ollama:test docker compose -f docker-compose.yml config --quiet
PAPER_APP_IMAGE=project-paper:local-arm64 PAPER_OLLAMA_IMAGE=ollama/ollama:test docker compose -f docker-compose.yml -f docker-compose.dev.yml config --quiet
```

Pull requests should explain the concrete problem, resulting behavior, tests run, and any operational or privacy effect. Never paste private user data into an issue or test fixture.

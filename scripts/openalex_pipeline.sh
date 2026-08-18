#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${PAPER_AGENT_REPO:-$HOME/workspace/paper-agent}"
cd "$REPO_DIR"

mkdir -p logs

git pull --ff-only

topics=(
  "AIOps observability incident response"
  "cloud operations anomaly detection remediation"
  "microservice diagnosis distributed systems debugging"
  "software reliability engineering production incidents"
  "LLM operations root cause analysis logs traces"
)

if [[ -n "${PAPER_AGENT_OPENALEX_TOPIC:-}" ]]; then
  topic="$PAPER_AGENT_OPENALEX_TOPIC"
else
  day_of_year="$(date +%j)"
  topic="${topics[$((10#$day_of_year % ${#topics[@]}))]}"
fi

echo "OpenAlex topic: $topic"

python3 -m paper_agents.cli pipeline-daily \
  --source openalex \
  --topic "$topic" \
  --quick \
  --fetch "${PAPER_AGENT_OPENALEX_FETCH:-30}" \
  --keep "${PAPER_AGENT_OPENALEX_KEEP:-2}" \
  --max-scout-attempts 1 \
  --request-delay "${PAPER_AGENT_REQUEST_DELAY:-5}" \
  --retries "${PAPER_AGENT_RETRIES:-4}" \
  --source-timeout "${PAPER_AGENT_SOURCE_TIMEOUT:-90}"

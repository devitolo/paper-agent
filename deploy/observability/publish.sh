#!/bin/sh
set -eu
exec python3 -m paper_agents.observability_publisher "$@"

#!/bin/sh
set -eu
exec python3 /opt/project-paper-observability/host_resource_collector.py collect --root "${PROJECT_PAPER_RESOURCE_ROOT:?PROJECT_PAPER_RESOURCE_ROOT is required}"

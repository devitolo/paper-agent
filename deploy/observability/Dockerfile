ARG BASE_IMAGE=python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704
FROM ${BASE_IMAGE}

RUN addgroup --system --gid 10001 collector \
 && adduser --system --uid 10001 --ingroup collector collector
WORKDIR /opt/project-paper-observability
COPY scripts/host_resource_collector.py ./host_resource_collector.py
COPY deploy/observability/entrypoint.sh ./entrypoint.sh
COPY paper_agents/__init__.py paper_agents/aws_container_status.py ./paper_agents/
RUN chmod 0555 host_resource_collector.py entrypoint.sh
USER 10001:10001
ENTRYPOINT ["/opt/project-paper-observability/entrypoint.sh"]

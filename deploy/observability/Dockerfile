ARG BASE_IMAGE=python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704
FROM ${BASE_IMAGE}
ARG VCS_REF
LABEL org.opencontainers.image.revision=$VCS_REF

RUN addgroup --system --gid 10001 collector \
 && adduser --system --uid 10001 --ingroup collector collector
RUN addgroup --system --gid 10002 publisher \
 && adduser --system --uid 10002 --ingroup publisher publisher
WORKDIR /opt/project-paper-observability
COPY scripts/host_resource_collector.py ./host_resource_collector.py
COPY deploy/observability/entrypoint.sh ./entrypoint.sh
COPY paper_agents/__init__.py paper_agents/observability_transport.py paper_agents/observability_publisher.py paper_agents/container_telemetry.py ./paper_agents/
COPY deploy/observability/publish.sh ./publish.sh
RUN chmod 0555 host_resource_collector.py entrypoint.sh publish.sh
USER 10001:10001
ENTRYPOINT ["/opt/project-paper-observability/entrypoint.sh"]

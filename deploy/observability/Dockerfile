ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

RUN addgroup --system --gid 10001 collector \
 && adduser --system --uid 10001 --ingroup collector collector
WORKDIR /opt/project-paper-observability
COPY scripts/host_resource_collector.py ./host_resource_collector.py
COPY deploy/observability/entrypoint.sh ./entrypoint.sh
RUN chmod 0555 host_resource_collector.py entrypoint.sh
USER 10001:10001
ENTRYPOINT ["/opt/project-paper-observability/entrypoint.sh"]

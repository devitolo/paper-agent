# Synthetic serving UI only. Build on an approved builder, never on the Mini.
ARG MINI_NONPROD_BASE_IMAGE
FROM ${MINI_NONPROD_BASE_IMAGE}
WORKDIR /app
RUN useradd --uid 10001 --create-home paper
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY LICENSE NOTICE ./
COPY paper_agents ./paper_agents
COPY scripts ./scripts
COPY sql ./sql
COPY deploy/templates ./paper_agents/package_templates
ARG VCS_REF
ARG SOURCE_BUNDLE_SHA256
LABEL org.opencontainers.image.revision="${VCS_REF}" \
      org.projectpaper.source-bundle-sha256="${SOURCE_BUNDLE_SHA256}" \
      org.projectpaper.environment="mini-nonproduction"
USER 10001:10001
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "-m", "paper_agents.package_runtime"]
CMD ["start"]

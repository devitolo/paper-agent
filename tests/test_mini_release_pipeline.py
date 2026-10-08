from __future__ import annotations

from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


class MiniReleasePipelineTests(unittest.TestCase):
    def test_docker_workflow_builds_and_accepts_mini_target(self):
        workflow = (ROOT / ".github/workflows/docker.yml").read_text(encoding="utf-8")
        self.assertIn("MINI_TARGET: mini-production", workflow)
        self.assertIn("target: ${{ env.MINI_TARGET }}", workflow)
        self.assertIn("publish_candidate", workflow)
        self.assertIn('"mini-candidate-*"', workflow)
        self.assertIn("^mini-candidate-[0-9a-f]{7,40}$", workflow)
        self.assertIn("scripts/mini_release_acceptance.sh", workflow)
        self.assertIn("SOURCE_BUNDLE_SHA256=", workflow)
        self.assertIn("image_ref=${REGISTRY}/${IMAGE_NAME}@", workflow)

    def test_mini_production_workflow_uses_hosted_build_and_self_hosted_deploy(self):
        workflow = (ROOT / ".github/workflows/mini-production.yml").read_text(encoding="utf-8")
        self.assertIn("pull_request:", workflow)
        self.assertIn("branches:\n      - mini-production", workflow)
        self.assertIn("platforms: linux/amd64", workflow)
        self.assertIn("image_ref=${REGISTRY}/${IMAGE_NAME}@", workflow)
        self.assertIn("github.event_name == 'push' && github.ref == 'refs/heads/mini-production'", workflow)
        self.assertIn("- self-hosted", workflow)
        self.assertIn("- project-paper-mini", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn("bash scripts/mini_production_update.sh", workflow)
        self.assertIn('PAPER_MINI_ARXIV_PROGRESS: "1"', workflow)
        self.assertIn('PAPER_MINI_OPENALEX_CURSOR: "1"', workflow)
        self.assertIn('PAPER_MINI_SEMANTIC_SCHOLAR_PROGRESS: "1"', workflow)
        self.assertIn("cache-from: type=gha,scope=mini-production", workflow)
        self.assertIn("cache-to: type=gha,mode=min,scope=mini-production", workflow)

    def test_dockerfile_keeps_public_and_mini_targets_explicit(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("# syntax=docker/dockerfile:", dockerfile)
        self.assertIn("--mount=type=cache,target=/root/.cache/pip", dockerfile)
        self.assertIn("--mount=type=cache,target=/root/.npm", dockerfile)
        self.assertIn("FROM migration-gemini AS mini-production", dockerfile)
        self.assertIn('org.projectpaper.runtime="mini-production"', dockerfile)
        self.assertIn("FROM base AS runtime", dockerfile)

    def test_dockerfile_copies_source_after_stable_mini_runtime_layers(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        mini_target = dockerfile.index("FROM migration-gemini AS mini-production")
        npm_install = dockerfile.index("npm-cli.js ci")
        copy_app = dockerfile.index("COPY paper_agents ./paper_agents", mini_target)
        source_inventory = dockerfile.index("image-runtime-inputs.json", mini_target)
        self.assertLess(npm_install, mini_target)
        self.assertLess(mini_target, copy_app)
        self.assertLess(copy_app, source_inventory)
        self.assertNotIn("SOURCE_BUNDLE_SHA256", dockerfile[:mini_target])

    def test_mini_curator_packages_and_verifies_pinned_minilm(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        compose = (ROOT / "docker-compose.mini-migration.yml").read_text(encoding="utf-8")
        update = (ROOT / "scripts" / "mini_production_update.sh").read_text(encoding="utf-8")
        requirements = (ROOT / "requirements-minilm.txt").read_text(encoding="utf-8")
        self.assertIn("onnxruntime==1.23.2", requirements)
        self.assertIn("transformers==4.57.1", requirements)
        self.assertIn("pip install -r requirements-minilm.txt", dockerfile)
        self.assertIn("!requirements-minilm.txt", (ROOT / ".dockerignore").read_text(encoding="utf-8"))
        self.assertIn("minilm-model:/models:ro", compose)
        self.assertIn("PAPER_AGENT_MINILM_ENABLED: ${PAPER_AGENT_MINILM_ENABLED:-1}", compose)
        self.assertIn("PAPER_AGENT_CURATOR_MODEL: ${PAPER_AGENT_CURATOR_MODEL:-qwen3:4b}", compose)
        self.assertIn('> "$RELEASE_DIR/minilm-check.json"', update)
        self.assertIn("MiniLM Curator interest-fit verification failed", update)
        self.assertIn('DEPLOY_CHECK=${PAPER_DEPLOY_CHECK:-light}', update)
        self.assertIn('if [[ "$DEPLOY_CHECK" == full ]]', update)
        self.assertIn('> "$RELEASE_DIR/qwen3-curator-check.json"', update)
        self.assertIn("Qwen3 Curator verification failed", update)
        self.assertIn("timeout=240", update)
        self.assertIn('--keep "${PAPER_ZENML_PILOT_KEEP:-5}" --dry-run', update)
        self.assertIn('echo "- ZenML deploy check:', update)

    def test_mini_compose_uses_short_stop_grace_after_job_drain(self):
        compose = (ROOT / "docker-compose.mini-migration.yml").read_text(encoding="utf-8")
        self.assertIn("stop_grace_period: 12s", compose)
        self.assertNotIn("stop_grace_period: 40s", compose)

    def test_packaged_web_app_handles_sigterm_for_fast_container_stop(self):
        web = (ROOT / "paper_agents/web.py").read_text(encoding="utf-8")
        self.assertIn("signal.signal(signal.SIGTERM, stop_from_sigterm)", web)
        self.assertIn("raise KeyboardInterrupt", web)
        self.assertIn("signal.signal(signal.SIGTERM, previous_sigterm)", web)

    def test_cron_template_uses_container_launcher_only(self):
        template = (ROOT / "deploy/project-paper.crontab").read_text(encoding="utf-8")
        self.assertIn("PAPER_MIGRATION_ENV_FILE=", template)
        self.assertIn("PAPER_MIGRATION_EXTRA_COMPOSE_FILES=$HOME/paper-mini-rehearsal/production-cutover/docker-compose.production.yml", template)
        self.assertNotIn("docker-compose.mini-rehearsal.yml", template)
        self.assertIn("scripts/mini_container_job.sh openalex", template)
        self.assertIn("scripts/mini_container_job.sh arxiv", template)
        self.assertIn("scripts/mini_container_job.sh semantic", template)
        self.assertIn("30 23 * * 0", template)
        self.assertIn("scripts/mini_container_job.sh zenml", template)
        self.assertIn("scripts/mini_container_job.sh backup", template)
        self.assertNotIn(".venv", template)
        self.assertNotIn("paper_agents.cli pipeline-daily", template)
        self.assertNotIn("scripts/openalex_pipeline.sh", template)
        self.assertNotIn("scripts/nightly_pipeline.sh", template)
        self.assertNotIn("scripts/semantic_scholar_pipeline.sh", template)
        update = (ROOT / "scripts/mini_production_update.sh").read_text(encoding="utf-8")
        self.assertIn("verification_zenml_cron", update)

    def test_release_scripts_are_shell_syntax_valid(self):
        for name in (
            "scripts/mini_release_acceptance.sh",
            "scripts/mini_production_update.sh",
            "scripts/install_project_paper_cron.sh",
        ):
            with self.subTest(name=name):
                result = subprocess.run(["bash", "-n", str(ROOT / name)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_acceptance_overrides_package_entrypoint_for_tool_checks(self):
        script = (ROOT / "scripts/mini_release_acceptance.sh").read_text(encoding="utf-8")
        self.assertIn("--entrypoint python", script)
        self.assertIn("--entrypoint node", script)
        self.assertIn("--entrypoint sh", script)

    def test_production_update_writes_rollback_before_mutation(self):
        script = (ROOT / "scripts/mini_production_update.sh").read_text(encoding="utf-8")
        rollback = script.index('cat > "$RELEASE_DIR/rollback.sh"')
        env_mutation = script.index('cp "$tmp_env" "$ENV_FILE"')
        cron_mutation = script.index('crontab "$RELEASE_DIR/crontab.next"')
        app_recreate = script.index('"${compose[@]}" up -d --no-deps --pull never --force-recreate app')
        self.assertLess(rollback, env_mutation)
        self.assertLess(rollback, cron_mutation)
        self.assertLess(rollback, app_recreate)

    def test_production_update_restores_previous_app_after_failed_deploy(self):
        script = (ROOT / "scripts/mini_production_update.sh").read_text(encoding="utf-8")
        self.assertIn("trap on_exit EXIT", script)
        self.assertIn("restore_previous_production()", script)
        self.assertIn('cp "$RELEASE_DIR/production.env.before" "$ENV_FILE"', script)
        self.assertIn('crontab "$RELEASE_DIR/crontab.before" || true', script)
        self.assertIn('APP_REPLACEMENT_STARTED=1', script)
        self.assertIn('auto-restore-app.log', script)
        self.assertIn('python -m paper_agents.package_runtime check-app', script)
        self.assertIn('restore_result=pass', script)

    def test_production_update_canonicalizes_ollama_image_digest(self):
        script = (ROOT / "scripts/mini_production_update.sh").read_text(encoding="utf-8")
        self.assertIn('old_ollama_image=$(sed -n', script)
        self.assertIn('if [[ "$ollama_image" == sha256:* ]]; then', script)
        self.assertIn("docker image inspect \"$ollama_image\" --format '{{index .RepoDigests 0}}'", script)
        self.assertIn('PAPER_MIGRATION_OLLAMA_IMAGE={ollama_image}', script)

    def test_production_update_can_replace_unavailable_app_container(self):
        script = (ROOT / "scripts/mini_production_update.sh").read_text(encoding="utf-8")
        self.assertIn('if "${compose[@]}" exec -T app true', script)
        self.assertIn('Existing app container is unavailable; skipping in-container runtime lease drain.', script)
        self.assertIn('Existing app container backup command unavailable; using one-shot image backup.', script)
        self.assertIn('--volumes-from paper-mini-production-app-1 --user 10001:10001', script)
        self.assertIn('scripts/backup_db.sh /app/data/paper_agent.db /backups', script)
        self.assertNotIn('\u201d', script)

    def test_production_update_preflights_persisted_state_before_app_replacement(self):
        script = (ROOT / "scripts/mini_production_update.sh").read_text(encoding="utf-8")
        preflight = script.index('phase_start "preflight persisted state"')
        stop = script.index('phase_start "stop app"')
        recreate = script.index('phase_start "recreate app"')
        self.assertLess(preflight, stop)
        self.assertLess(preflight, recreate)
        self.assertIn('--volumes-from paper-mini-production-app-1', script)
        self.assertIn('-m paper_agents.package_runtime check-state', script)
        self.assertIn('persisted-state-preflight.json', script)
        self.assertIn('persisted-state-preflight.log', script)

    def test_production_update_can_persist_openalex_cursor_flag(self):
        script = (ROOT / "scripts/mini_production_update.sh").read_text(encoding="utf-8")
        self.assertIn('OPENALEX_CURSOR=${PAPER_MINI_OPENALEX_CURSOR:-}', script)
        self.assertIn('PAPER_MINI_OPENALEX_CURSOR must be 0 or 1 when set', script)
        self.assertIn('ARXIV_PROGRESS=${PAPER_MINI_ARXIV_PROGRESS:-}', script)
        self.assertIn('PAPER_MINI_ARXIV_PROGRESS must be 0 or 1 when set', script)
        self.assertIn('arxiv_progress = sys.argv[5]', script)
        self.assertIn('PAPER_ARXIV_PROGRESS={arxiv_progress}', script)
        self.assertIn('openalex_cursor = sys.argv[6]', script)
        self.assertIn('PAPER_OPENALEX_CURSOR={openalex_cursor}', script)
        self.assertIn('if openalex_cursor and not found_openalex_cursor:', script)

    def test_production_env_key_overrides_ambient_shell_key(self):
        script = (ROOT / "scripts/mini_production_update.sh").read_text(encoding="utf-8")
        self.assertGreaterEqual(script.count("unset SEMANTIC_SCHOLAR_API_KEY"), 2)
        self.assertLess(
            script.index("unset SEMANTIC_SCHOLAR_API_KEY"),
            script.index("compose=("),
        )

    def test_production_update_reports_post_deploy_verification(self):
        script = (ROOT / "scripts/mini_production_update.sh").read_text(encoding="utf-8")
        self.assertIn('post-deploy-verification.txt', script)
        self.assertIn('timing.tsv', script)
        self.assertIn('phase_start "pull image"', script)
        self.assertIn('phase_start "recreate app"', script)
        self.assertIn('phase_start "readiness"', script)
        self.assertIn('### Deploy phase timing', script)
        self.assertIn('scripts/minilm_eval_after_pipeline.sh', script)
        self.assertIn('scripts/minilm_shadow_after_pipeline.sh', script)
        self.assertIn('minilm_runner_sha256=', script)
        self.assertIn('PAPER_MINILM_EVAL_ENABLED=0', script)
        self.assertIn('PAPER_MINILM_SHADOW_ENABLED=0', script)
        self.assertIn('verification_revision=$(docker inspect', script)
        self.assertIn('verification_status=$(docker inspect', script)
        self.assertIn('image-metadata.env', script)
        self.assertIn('image-layer-diff.txt', script)
        self.assertIn('previous-app-image-inspect.json', script)
        self.assertIn('image_size_mib=', script)
        self.assertIn('image_layer_count=', script)
        self.assertIn('reused_image_layer_count=', script)
        self.assertIn('new_image_layer_count=', script)
        self.assertIn('removed_image_layer_count=', script)
        self.assertIn('verification_arxiv_progress=$("${compose[@]}" exec -T app sh -lc', script)
        self.assertIn('Post-deploy verification failed: PAPER_ARXIV_PROGRESS=', script)
        self.assertIn('verified_arxiv_progress=%s', script)
        self.assertIn('verification_openalex_cursor=$("${compose[@]}" exec -T app sh -lc', script)
        self.assertIn('Post-deploy verification failed: PAPER_OPENALEX_CURSOR=', script)
        self.assertIn('verified_openalex_cursor=%s', script)
        self.assertIn('PAPER_OPENALEX_CURSOR: \\`', script)
        self.assertIn('SEMANTIC_SCHOLAR_PROGRESS=${PAPER_MINI_SEMANTIC_SCHOLAR_PROGRESS:-}', script)
        self.assertIn('Post-deploy verification failed: PAPER_SEMANTIC_SCHOLAR_PROGRESS=', script)
        self.assertIn('verified_semantic_scholar_progress=%s', script)
        self.assertIn('verified_image_size_mib=%s', script)
        self.assertIn('verified_reused_image_layer_count=%s', script)
        self.assertIn('Image size: \\`', script)
        self.assertIn('Image layers: \\`', script)
        self.assertIn('Layer diff: \\`', script)
        self.assertIn('if [[ "$DEPLOY_CHECK" == full ]]', script)
        self.assertIn('qwen3-curator-check.json', script)
        self.assertIn('assess_evidence(candidate', script)
        self.assertIn('timeout=240', script)

    def test_runbook_documents_no_git_pull_production_deployment(self):
        runbook = (ROOT / "docs/mini-release-runbook.md").read_text(encoding="utf-8")
        self.assertIn("Do not deploy application code by `git pull`", runbook)
        self.assertIn("PAPER_MINI_APP_IMAGE='ghcr.io/devitolo/paper-agent@sha256:<tested-digest>'", runbook)
        self.assertIn("bash scripts/mini_production_update.sh", runbook)
        self.assertIn("schema/state-changing release", runbook)


if __name__ == "__main__":
    unittest.main()

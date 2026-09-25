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
        self.assertIn("scripts/mini_release_acceptance.sh", workflow)
        self.assertIn("SOURCE_BUNDLE_SHA256=", workflow)
        self.assertIn("image_ref=${REGISTRY}/${IMAGE_NAME}@", workflow)

    def test_dockerfile_keeps_public_and_mini_targets_explicit(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("FROM migration-gemini AS mini-production", dockerfile)
        self.assertIn('LABEL org.projectpaper.runtime="mini-production"', dockerfile)
        self.assertRegex(dockerfile, r"FROM base AS runtime\s*$")

    def test_cron_template_uses_container_launcher_only(self):
        template = (ROOT / "deploy/project-paper.crontab").read_text(encoding="utf-8")
        self.assertIn("PAPER_MIGRATION_ENV_FILE=", template)
        self.assertIn("PAPER_MIGRATION_EXTRA_COMPOSE_FILES=$HOME/paper-mini-rehearsal/production-cutover/docker-compose.production.yml", template)
        self.assertNotIn("docker-compose.mini-rehearsal.yml", template)
        self.assertIn("scripts/mini_container_job.sh openalex", template)
        self.assertIn("scripts/mini_container_job.sh arxiv", template)
        self.assertIn("scripts/mini_container_job.sh semantic", template)
        self.assertIn("scripts/mini_container_job.sh backup", template)
        self.assertNotIn(".venv", template)
        self.assertNotIn("paper_agents.cli pipeline-daily", template)
        self.assertNotIn("scripts/openalex_pipeline.sh", template)
        self.assertNotIn("scripts/nightly_pipeline.sh", template)
        self.assertNotIn("scripts/semantic_scholar_pipeline.sh", template)

    def test_release_scripts_are_shell_syntax_valid(self):
        for name in (
            "scripts/mini_release_acceptance.sh",
            "scripts/mini_production_update.sh",
            "scripts/install_project_paper_cron.sh",
        ):
            with self.subTest(name=name):
                result = subprocess.run(["bash", "-n", str(ROOT / name)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_production_update_writes_rollback_before_mutation(self):
        script = (ROOT / "scripts/mini_production_update.sh").read_text(encoding="utf-8")
        rollback = script.index('cat > "$RELEASE_DIR/rollback.sh"')
        env_mutation = script.index('cp "$tmp_env" "$ENV_FILE"')
        cron_mutation = script.index('crontab "$RELEASE_DIR/crontab.next"')
        app_recreate = script.index('"${compose[@]}" up -d --no-deps --pull never --force-recreate app')
        self.assertLess(rollback, env_mutation)
        self.assertLess(rollback, cron_mutation)
        self.assertLess(rollback, app_recreate)

    def test_runbook_documents_no_git_pull_production_deployment(self):
        runbook = (ROOT / "docs/mini-release-runbook.md").read_text(encoding="utf-8")
        self.assertIn("Do not deploy application code by `git pull`", runbook)
        self.assertIn("PAPER_MINI_APP_IMAGE='ghcr.io/devitolo/paper-agent@sha256:<tested-digest>'", runbook)
        self.assertIn("bash scripts/mini_production_update.sh", runbook)
        self.assertIn("schema/state-changing release", runbook)


if __name__ == "__main__":
    unittest.main()

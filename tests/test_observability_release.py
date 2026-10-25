from pathlib import Path
import unittest


ROOT = Path(__file__).parents[1]


class ObservabilityReleaseContractTests(unittest.TestCase):
    def test_workflow_hands_artifact_to_deploy_and_verifies_checksum(self):
        text = (ROOT / ".github/workflows/observability-release.yml").read_text()
        self.assertIn("actions/upload-artifact", text)
        self.assertIn("actions/download-artifact", text)
        self.assertIn("actual=$(sha256sum", text)
        self.assertIn("test \"$actual\" = \"$expected\"", text)
        self.assertIn("observability-mini-production", text)
        self.assertIn("observability-aws-production", text)
        self.assertIn("cancel-in-progress: false", text)

    def test_installer_has_unique_manifest_backups_and_fail_closed_target(self):
        text = (ROOT / "scripts/install_resource_collector_cron.sh").read_text()
        self.assertIn("--target=mini-production", text)
        self.assertIn("collector-wrapper.previous", text)
        self.assertIn("collector-cron.previous", text)
        self.assertIn("INSTALL-MANIFEST", text)
        self.assertIn("PROJECT_PAPER_RESOURCE_EXPORT_DIR", text)
        self.assertIn("mini-production-export", text)

    def test_rollback_requires_manifest_and_does_not_remove_shared_prefix(self):
        text = (ROOT / "scripts/remove_resource_collector_cron.sh").read_text()
        self.assertIn("owned manifest missing; refusing rollback", text)
        self.assertNotIn("rm -rf /opt/project-paper-observability", text)

    def test_wrapper_loads_exact_production_env_and_distinguishes_lock(self):
        text = (ROOT / "scripts/resource_collector_job.sh").read_text()
        self.assertIn("/etc/project-paper-observability/mini-production/mini-production.env", text)
        self.assertIn("write_state skipped", text)
        self.assertIn("write_state failed", text)
        self.assertIn("timeout --signal=TERM", text)

    def test_installer_owns_failure_cleanup_and_rollback_is_idempotent(self):
        installer = (ROOT / "scripts/install_resource_collector_cron.sh").read_text()
        rollback = (ROOT / "scripts/remove_resource_collector_cron.sh").read_text()
        self.assertIn("restore_on_error()", installer)
        self.assertIn("trap restore_on_error ERR", installer)
        self.assertIn("manifest.restored", rollback)
        self.assertIn("required in", installer)


if __name__ == "__main__":
    unittest.main()

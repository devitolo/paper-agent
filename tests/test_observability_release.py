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
        self.assertIn("environment: observability-production", text)
        self.assertIn("cancel-in-progress: false", text)

    def test_installer_has_unique_manifest_backups_and_fail_closed_target(self):
        text = (ROOT / "scripts/install_resource_collector_cron.sh").read_text()
        self.assertIn("--target=mini-production", text)
        self.assertIn("collector-wrapper.previous", text)
        self.assertIn("collector-cron.previous", text)
        self.assertIn("INSTALL-MANIFEST", text)
        self.assertIn("PROJECT_PAPER_RESOURCE_EXPORT_DIR", text)

    def test_rollback_requires_manifest_and_does_not_remove_shared_prefix(self):
        text = (ROOT / "scripts/remove_resource_collector_cron.sh").read_text()
        self.assertIn("owned manifest missing; refusing rollback", text)
        self.assertNotIn("rm -rf /opt/project-paper-observability", text)


if __name__ == "__main__":
    unittest.main()

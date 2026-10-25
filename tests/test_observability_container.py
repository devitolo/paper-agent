from pathlib import Path
import unittest


ROOT = Path(__file__).parents[1]


class ObservabilityContainerContractTests(unittest.TestCase):
    def test_image_and_launcher_are_least_privilege(self):
        dockerfile = (ROOT / "deploy/observability/Dockerfile").read_text()
        launcher = (ROOT / "deploy/observability/run-container.sh").read_text()
        self.assertIn("USER 10001:10001", dockerfile)
        for required in ("--network none", "--read-only", "--cap-drop ALL", "no-new-privileges", "--memory 64m", "--cpus 0.10", "--pids-limit 32"):
            self.assertIn(required, launcher)
        self.assertNotIn("docker.sock", launcher)
        self.assertIn("timeout --signal=TERM --kill-after=5s 45s", launcher)
        self.assertIn("env_file=", launcher)
        self.assertIn("grep -Eq '@sha256:[0-9a-f]{64}$'", launcher)

    def test_release_builds_amd64_digest_and_deploys_container(self):
        workflow = (ROOT / ".github/workflows/observability-release.yml").read_text()
        self.assertIn("--platform linux/amd64 --push", workflow)
        self.assertIn("docker buildx imagetools inspect", workflow)
        self.assertIn("@${{ needs.package.outputs.image_digest }}", workflow)
        self.assertIn("install_container_cron.sh", workflow)
        self.assertIn("remove_container_cron.sh", workflow)
        self.assertIn("-f deploy/observability/Dockerfile", workflow)
        self.assertIn(" .", workflow)

    def test_cron_isolated_from_paper_pipeline(self):
        cron = (ROOT / "deploy/observability/cron.example").read_text()
        self.assertIn("project-paper-observability-container", cron)
        self.assertIn("/run/lock/project-paper-observability.lock", cron)
        self.assertNotIn("mini_container_job.sh", cron)

    def test_installer_uses_collector_uid_and_transaction_trap(self):
        installer = (ROOT / "deploy/observability/install_container_cron.sh").read_text()
        self.assertIn("install -d -o 10001 -g 10001 -m 0755", installer)
        self.assertIn("install -d -o 10001 -g 10001 -m 0700", installer)
        self.assertIn("restore_on_error()", installer)
        self.assertIn("trap restore_on_error ERR", installer)


if __name__ == "__main__":
    unittest.main()

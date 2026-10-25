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

    def test_release_builds_amd64_digest_and_deploys_container(self):
        workflow = (ROOT / ".github/workflows/observability-release.yml").read_text()
        self.assertIn("--platform linux/amd64 --push", workflow)
        self.assertIn("docker buildx imagetools inspect", workflow)
        self.assertIn("@${{ needs.package.outputs.image_digest }}", workflow)
        self.assertIn("install_container_cron.sh", workflow)
        self.assertIn("remove_container_cron.sh", workflow)

    def test_cron_isolated_from_paper_pipeline(self):
        cron = (ROOT / "deploy/observability/cron.example").read_text()
        self.assertIn("project-paper-observability-container", cron)
        self.assertIn("/run/lock/project-paper-observability.lock", cron)
        self.assertNotIn("mini_container_job.sh", cron)


if __name__ == "__main__":
    unittest.main()

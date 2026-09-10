from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paper_agents import curator_agent, package_runtime, runtime_config, topics, web

ROOT = Path(__file__).resolve().parents[1]


class PackageRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copytree(ROOT / "sql", self.root / "sql")

    def initialize(self):
        package_runtime.initialize(self.root, ROOT / "deploy/templates")

    def test_empty_install_and_restart_preserve_state(self):
        self.initialize()
        profile = self.root / "data/profile.json"
        config = self.root / "config/topics.yaml"
        self.assertEqual(json.loads(profile.read_text())["interests"], [])
        self.assertEqual(topics.load_topic_config(config), [])
        profile.write_text(profile.read_text().replace('"interests": []', '"interests": ["customer topic"]'))
        topics.save_topic_config([topics.TopicEntry("mine", "Mine", "my research", ["arxiv"])], config)
        before = (profile.read_bytes(), config.read_bytes())
        connection = sqlite3.connect(self.root / "data/paper_agent.db")
        connection.execute("INSERT INTO papers(canonical_key,title) VALUES('test:1','Saved paper')")
        connection.commit()
        connection.close()
        self.initialize()
        self.assertEqual(before, (profile.read_bytes(), config.read_bytes()))
        self.assertTrue(package_runtime.app_status(self.root / "data/paper_agent.db")["ready"])
        connection = sqlite3.connect(self.root / "data/paper_agent.db")
        self.assertEqual(connection.execute("SELECT title FROM papers").fetchone()[0], "Saved paper")
        connection.close()

    def test_bad_state_is_not_reseeded_and_newer_schema_is_rejected(self):
        self.initialize()
        config = self.root / "config/topics.yaml"
        config.write_text("broken customer configuration")
        with self.assertRaisesRegex(RuntimeError, "Invalid existing topics"):
            self.initialize()
        self.assertEqual(config.read_text(), "broken customer configuration")
        (self.root / "data/package-version.json").write_text('{"schema_version": 999}')
        with self.assertRaisesRegex(RuntimeError, "Unsupported package schema"):
            self.initialize()

    def test_packaged_topics_are_empty_strict_and_arxiv_default(self):
        self.initialize()
        with patch.dict(os.environ, {"PAPER_AGENT_PACKAGED": "1"}):
            self.assertEqual(runtime_config.default_sources(), ["arxiv"])
            self.assertEqual(topics.create_topic_from_fast_path("New research").sources, ["arxiv"])
            self.assertEqual(topics.load_topic_config_or_seed(self.root / "config/topics.yaml"), [])
            with self.assertRaises(FileNotFoundError):
                topics.load_topic_config_or_seed(self.root / "missing.yaml")
            with self.assertRaises(FileNotFoundError):
                topics.select_topics_for_source("arxiv", path=self.root / "missing.yaml")

    def test_every_imported_default_uses_configured_ollama(self):
        code = """
from paper_agents import cli, curator_agent, pipeline, local_extract, curator_evidence, topic_agent, reviewer_agent
expected = 'http://ollama:11434/api/generate'
assert local_extract.DEFAULT_OLLAMA_URL == expected
assert pipeline.DEFAULT_OLLAMA_URL == expected
assert curator_evidence.DEFAULT_OLLAMA_URL == expected
assert cli.DEFAULT_OLLAMA_URL == expected
assert reviewer_agent.ReviewerConfig().ollama_url == expected
assert curator_agent.CuratorConfig().evidence_ollama_url == expected
assert topic_agent.topic_ollama_url() == expected
"""
        env = {**os.environ, "PAPER_AGENT_OLLAMA_URL": "http://ollama:11434"}
        env.pop("PAPER_AGENT_TOPIC_OLLAMA_URL", None)
        subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, check=True)

    def test_curator_config_preserves_explicit_evidence_endpoint_override(self):
        self.assertEqual(
            curator_agent.CuratorConfig(evidence_ollama_url="http://localhost:11434/api/generate").evidence_ollama_url,
            "http://localhost:11434/api/generate",
        )

    def test_model_listing_does_not_imply_inference_readiness(self):
        with patch.object(package_runtime, "request_json", return_value={"models": [{"name": "qwen2.5:1.5b-instruct"}]}):
            self.assertEqual(package_runtime.model_status()["inference"], "unchecked")
            with self.assertRaisesRegex(RuntimeError, "inference did not complete"):
                package_runtime.check_model()
        with patch.object(package_runtime, "request_json", side_effect=[
            {"models": [{"name": "qwen2.5:1.5b-instruct"}]}, {"done": True, "response": "READY"}
        ]) as request:
            self.assertEqual(package_runtime.check_model(7)["inference"], "ready")
            self.assertEqual(request.call_args.kwargs["timeout"], 7)

    def test_gemini_disabled_saves_exact_feedback_without_worker(self):
        self.initialize()
        path = self.root / "data/paper_agent.db"
        connection = sqlite3.connect(path)
        connection.execute("INSERT INTO papers(id,canonical_key,title) VALUES(1,'test:1','Paper')")
        connection.commit()
        connection.close()
        blob = 'Score: 4.5/5\nUseful operational evidence.'
        with patch.dict(os.environ, {"PAPER_AGENT_PACKAGED": "1", "PAPER_AGENT_GEMINI_ENABLED": "0"}), \
             patch.object(web, "start_profile_apply_worker") as worker, \
             patch.object(web, "apply_feedback_to_profile") as apply:
            result = web.save_feedback(path, paper_id=1, status="reviewed", notes=blob,
                feedback_content=blob, profile_apply_mode="background")
            self.assertTrue(result["feedback_saved"])
            worker.assert_not_called()
            apply.assert_not_called()
        connection = sqlite3.connect(path)
        self.assertEqual(connection.execute("SELECT content FROM raw_feedback").fetchone()[0], blob)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM structured_feedback").fetchone()[0], 1)
        connection.close()


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "scripts").mkdir()
        (self.root / "bin").mkdir()
        shutil.copy(ROOT / "scripts/install_project_paper.sh", self.root / "scripts")
        shutil.copy(ROOT / ".env.example", self.root)
        self.executable("uname", '#!/bin/sh\ncase "$1" in -s) echo Darwin;; -m) echo arm64;; esac\n')
        self.executable("docker", f'''#!{sys.executable}
import os, sys
a=sys.argv[1:]
if os.environ.get('INSTALL_TEST_FAIL_IMAGES') == '1':
 if a[:2] == ['image','inspect'] or (a[:1] == ['compose'] and 'pull' in a): sys.exit(1)
if a[:1]==['info']:
 print('linux/aarch64' if 'Architecture' in a[-1] else '8589934592')
elif a[:2]==['image','inspect'] and '--format' in a: print('linux/arm64')
elif a[:1]==['inspect']: print('false' if 'Running' in a[2] else '0')
elif a[:1]==['compose'] and 'ps' in a: print('helper-container')
''')
        self.env = {**os.environ, "PATH": str(self.root / "bin") + ":" + os.environ["PATH"]}

    def executable(self, name, content):
        path = self.root / "bin" / name
        path.write_text(content)
        path.chmod(0o755)

    def run_installer(self, *args):
        return subprocess.run(["bash", str(self.root / "scripts/install_project_paper.sh"), *args],
            cwd="/tmp", env=self.env, capture_output=True, text=True, timeout=10)

    def test_rerun_keeps_identity_images_and_customer_configuration(self):
        first = self.run_installer("--build", "--ollama-image", "ollama/ollama:test")
        self.assertEqual(first.returncode, 0, first.stderr)
        record = (self.root / ".paper-install").read_bytes()
        with (self.root / ".env").open("a") as output:
            output.write("\nPAPER_PORT=8123\nCUSTOMER_NOTE=preserve-me\n")
        config = (self.root / ".env").read_bytes()
        second = self.run_installer()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("127.0.0.1:8123", second.stdout)
        self.assertEqual((self.root / ".paper-install").read_bytes(), record)
        self.assertEqual((self.root / ".env").read_bytes(), config)
        self.assertEqual((self.root / ".env").stat().st_mode & 0o777, 0o600)

    def test_config_is_never_sourced_and_changed_image_is_rejected(self):
        self.assertEqual(self.run_installer("--ollama-image", "ollama/ollama:test").returncode, 0)
        sentinel = self.root / "executed"
        with (self.root / ".env").open("a") as output:
            output.write(f'\nPAPER_APP_IMAGE=$(touch {sentinel})\n')
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(sentinel.exists())
        with (self.root / ".env").open("a") as output:
            output.write('\nPAPER_APP_IMAGE=project-paper:changed\n')
        self.assertIn("not an upgrade", self.run_installer().stderr)

    def test_failed_first_image_acquisition_allows_corrected_selection(self):
        self.env['INSTALL_TEST_FAIL_IMAGES'] = '1'
        failed = self.run_installer('--app-image', 'project-paper:typo', '--ollama-image', 'ollama/ollama:test')
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn('Selected app image unavailable', failed.stderr)
        self.assertFalse((self.root / '.paper-install').exists())
        with (self.root / '.env').open('a') as output:
            output.write('\nPAPER_APP_IMAGE=project-paper:corrected\n')
        del self.env['INSTALL_TEST_FAIL_IMAGES']
        corrected = self.run_installer()
        self.assertEqual(corrected.returncode, 0, corrected.stderr)
        record = (self.root / '.paper-install').read_bytes()
        self.assertIn(b'app_image=project-paper:corrected', record)
        self.assertEqual(self.run_installer().returncode, 0)
        self.assertEqual((self.root / '.paper-install').read_bytes(), record)

    def test_unsupported_host_does_not_create_configuration(self):
        self.executable("uname", '#!/bin/sh\necho unsupported\n')
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / ".env").exists())


class ModelPreparationTests(unittest.TestCase):
    def test_cached_model_is_not_pulled_again_and_hung_api_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = root / "ollama"
            command.write_text(f'''#!{sys.executable}
import os, pathlib, sys, time
root=pathlib.Path(os.environ['MODEL_TEST_DIR'])
if os.environ.get('MODEL_TEST_HANG') == '1': time.sleep(30)
if sys.argv[1] == 'show': sys.exit(0 if (root/'model').exists() else 1)
if sys.argv[1] == 'pull':
 (root/'model').touch()
 with (root/'pulls').open('a') as f: f.write('pull\\n')
''')
            command.chmod(0o755)
            env = {**os.environ, "PATH": directory + ":" + os.environ["PATH"],
                "MODEL_TEST_DIR": directory, "PAPER_MODEL_PREPARE_TIMEOUT": "2", "PAPER_MODEL_PULL_ATTEMPTS": "1"}
            def prepare():
                return subprocess.run(["sh", str(ROOT / "deploy/prepare-model.sh")],
                    env=env, capture_output=True, text=True, timeout=6)
            first = prepare()
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIn("pulling:", first.stdout)
            second = prepare()
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertNotIn("pulling:", second.stdout)
            self.assertEqual((root / "pulls").read_text(), "pull\n")
            env["MODEL_TEST_HANG"] = "1"
            failed = prepare()
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("timed out or interrupted", failed.stderr)


if __name__ == "__main__":
    unittest.main()

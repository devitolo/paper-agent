from __future__ import annotations

import json
import os
import re
import signal
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.client import HTTPResponse
from pathlib import Path
from unittest.mock import patch

from paper_agents import curator_agent, package_runtime, prepare_model, runtime_config, topics, web

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

    def test_compose_prepare_model_uses_packaged_app_image_without_bind_mount(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        development = (ROOT / "docker-compose.dev.yml").read_text(encoding="utf-8")
        self.assertIn("prepare-model:", compose)
        self.assertIn("image: ${PAPER_APP_IMAGE", compose)
        self.assertIn('entrypoint: ["python", "-m", "paper_agents.prepare_model"]', compose)
        self.assertNotIn("./deploy/prepare-model.sh:/prepare-model.sh", compose)
        self.assertNotIn("build:", compose)
        self.assertIn("build:", development)


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
 platform=os.environ.get('INSTALL_TEST_PLATFORM', 'linux/arm64')
 print(('linux/x86_64' if platform == 'linux/amd64' else 'linux/aarch64') if 'Architecture' in a[-1] else '8589934592')
elif a[:2]==['image','inspect'] and '--format' in a:
 print('https://github.com/devitolo/paper-agent|v0.1.0' if 'org.opencontainers.image.source' in a[a.index('--format') + 1] else os.environ.get('INSTALL_TEST_PLATFORM', 'linux/arm64'))
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
        failed = self.run_installer('--app-image', 'project-paper:v0.1.0', '--ollama-image', 'ollama/ollama:test')
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn('Selected release app image unavailable', failed.stderr)
        self.assertFalse((self.root / '.paper-install').exists())
        with (self.root / '.env').open('a') as output:
            output.write('\nPAPER_APP_IMAGE=project-paper:v0.1.1\n')
        del self.env['INSTALL_TEST_FAIL_IMAGES']
        corrected = self.run_installer()
        self.assertEqual(corrected.returncode, 0, corrected.stderr)
        record = (self.root / '.paper-install').read_bytes()
        self.assertIn(b'app_image=project-paper:v0.1.1', record)
        self.assertEqual(self.run_installer().returncode, 0)
        self.assertEqual((self.root / '.paper-install').read_bytes(), record)

    def test_unsupported_host_does_not_create_configuration(self):
        self.executable("uname", '#!/bin/sh\necho unsupported\n')
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / ".env").exists())

    def test_linux_x86_64_selects_amd64_without_emulation(self):
        self.executable("uname", '#!/bin/sh\ncase "$1" in -s) echo Linux;; -m) echo x86_64;; esac\n')
        self.env["INSTALL_TEST_PLATFORM"] = "linux/amd64"
        result = self.run_installer("--build", "--ollama-image", "ollama/ollama:test")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("platform=linux/amd64", (self.root / ".paper-install").read_text())

    def test_release_rejects_latest_and_mode_change_on_rerun(self):
        self.assertNotEqual(
            self.run_installer("--app-image", "ghcr.io/devitolo/paper-agent:latest", "--ollama-image", "ollama/ollama:test").returncode,
            0,
        )
        (self.root / ".env").unlink()
        installed = self.run_installer("--app-image", "ghcr.io/devitolo/paper-agent:v0.1.0", "--ollama-image", "ollama/ollama:test")
        self.assertEqual(installed.returncode, 0, installed.stderr)
        result = self.run_installer("--build")
        self.assertIn("mode differs", result.stderr)

    def test_release_requires_oci_project_metadata(self):
        self.executable("docker", self.root.joinpath("bin/docker").read_text().replace(
            "https://github.com/devitolo/paper-agent|v0.1.0", "not-project-paper|unknown"
        ))
        result = self.run_installer("--app-image", "ghcr.io/devitolo/paper-agent:v0.1.0", "--ollama-image", "ollama/ollama:test")
        self.assertIn("OCI source/version metadata", result.stderr)

    def test_release_digest_references_are_exact_and_ollama_must_be_pinned(self):
        digest = "a" * 64
        app = f"ghcr.io/devitolo/paper-agent@sha256:{digest}"
        ollama = f"ollama/ollama@sha256:{digest}"
        valid = self.run_installer("--app-image", app, "--ollama-image", ollama)
        self.assertEqual(valid.returncode, 0, valid.stderr)
        for bad in (f"ghcr.io/devitolo/paper-agent@sha256:{'a' * 63}", f"ghcr.io/devitolo/paper-agent@sha256:{'g' * 64}"):
            (self.root / ".env").unlink(missing_ok=True)
            (self.root / ".paper-install").unlink(missing_ok=True)
            result = self.run_installer("--app-image", bad, "--ollama-image", ollama)
            self.assertIn("Release app image", result.stderr)
        for bad in ("ollama/ollama", "ollama/ollama:latest", "ollama/ollama:latest-cpu"):
            (self.root / ".env").unlink(missing_ok=True)
            (self.root / ".paper-install").unlink(missing_ok=True)
            result = self.run_installer("--app-image", app, "--ollama-image", bad)
            self.assertIn("Release Ollama image", result.stderr)


class ModelPreparationTests(unittest.TestCase):
    def test_packaged_prepare_model_skips_cached_model_and_pulls_when_absent(self):
        events = []

        def fake_request(path, payload=None, *, timeout=5, deadline=None):
            events.append((path, payload))
            if path == "/api/tags":
                present = any(event[0] == "/api/pull" for event in events)
                return {"models": [{"name": "qwen2.5:1.5b-instruct"}]} if present else {"models": []}
            return {}

        with patch.object(prepare_model, "request_json", side_effect=fake_request), \
             patch.object(prepare_model, "pull_model", side_effect=lambda model, deadline: events.append(("/api/pull", {"name": model}))):
            prepare_model.prepare_model(budget=10, attempts=1)

        self.assertIn(("/api/pull", {"name": "qwen2.5:1.5b-instruct"}), events)

        with patch.object(prepare_model, "request_json", return_value={"models": [{"name": "qwen2.5:1.5b-instruct"}]}), \
             patch.object(prepare_model, "pull_model") as pull:
            prepare_model.prepare_model(budget=10, attempts=1)
            pull.assert_not_called()

    def test_packaged_prepare_model_retries_pull_and_rejects_malformed_responses(self):
        calls = []
        def tags(_path, _payload=None, *, timeout=5, deadline=None):
            return {"models": [{"name": "qwen2.5:1.5b-instruct"}]} if len(calls) == 2 else {"models": []}

        def pull(_model, _deadline):
            calls.append("pull")
            if len(calls) == 1:
                raise OSError("temporary network failure")

        with patch.object(prepare_model, "request_json", side_effect=tags), \
             patch.object(prepare_model, "pull_model", side_effect=pull), \
             patch.object(prepare_model.time, "sleep"):
            prepare_model.prepare_model(budget=10, attempts=2)
        self.assertEqual(calls, ["pull", "pull"])

        with patch.object(prepare_model, "request_json", return_value={"models": "not-a-list"}), \
             patch.object(prepare_model.time, "monotonic", side_effect=[0, 0]):
            with self.assertRaisesRegex(prepare_model.ModelPreparationError, "malformed model listing"):
                prepare_model.model_present("qwen2.5:1.5b-instruct", deadline=1)

    def test_packaged_prepare_model_enforces_timeout_budget(self):
        with patch.object(prepare_model, "request_json", side_effect=OSError("unavailable")), \
             patch.object(prepare_model.time, "monotonic", side_effect=[0, 0, 2, 2]), \
             patch.object(prepare_model.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "did not become ready"):
                prepare_model.wait_for_api(deadline=1)

    def test_cached_result_after_deadline_and_malformed_models_are_rejected(self):
        with patch.object(prepare_model, "request_json", return_value={"models": [{"name": "qwen2.5:1.5b-instruct"}]}), \
             patch.object(prepare_model.time, "monotonic", side_effect=[0, 2]):
            with self.assertRaisesRegex(prepare_model.ModelPreparationError, "timed out"):
                prepare_model.model_present("qwen2.5:1.5b-instruct", deadline=1)
        for payload in (None, [], "bad", {"models": [None]}, {"models": [{"name": None}]}, {"models": [{"name": ""}]}):
            with self.assertRaises(prepare_model.ModelPreparationError):
                prepare_model.validated_models(payload)  # type: ignore[arg-type]

    def test_real_pull_stream_rejects_malformed_and_deadline_trickle(self):
        class Response:
            def __init__(self, payload): self.payload = iter(payload)
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self, _size): return next(self.payload)

        with patch.object(prepare_model.urllib.request, "urlopen", return_value=Response([b'{"status":"pulling"}\n', b''])), \
             patch.object(prepare_model.time, "monotonic", return_value=0):
            prepare_model.pull_model("qwen2.5:1.5b-instruct", deadline=10)
        for payload in ([b'[]\n', b''], [b'{"status":null}\n', b''], [b'{"done":"yes"}\n', b'']):
            with patch.object(prepare_model.urllib.request, "urlopen", return_value=Response(payload)), \
                 patch.object(prepare_model.time, "monotonic", return_value=0):
                with self.assertRaises(prepare_model.ModelPreparationError):
                    prepare_model.pull_model("qwen2.5:1.5b-instruct", deadline=10)
        ticks = iter([0, 0, 0, 0, 0, 2])
        with patch.object(prepare_model.urllib.request, "urlopen", return_value=Response([b'x', b'y', b'z'])), \
             patch.object(prepare_model.time, "monotonic", side_effect=lambda: next(ticks)):
            with self.assertRaisesRegex(prepare_model.ModelPreparationError, "timed out"):
                prepare_model.pull_model("qwen2.5:1.5b-instruct", deadline=1)

    def test_real_http_bodies_obey_wall_clock_budget(self):
        def response(parts):
            client, server = socket.socketpair()
            total = sum(len(part) for part, _delay in parts)
            server.sendall(f"HTTP/1.1 200 OK\r\nContent-Length: {total}\r\n\r\n".encode())
            def write():
                try:
                    for part, delay in parts:
                        server.sendall(part)
                        time.sleep(delay)
                except BrokenPipeError:
                    pass
                finally:
                    server.close()
            writer = threading.Thread(target=write)
            writer.start()
            result = HTTPResponse(client)
            result.begin()
            self.addCleanup(client.close)
            self.addCleanup(writer.join, 1)
            return result

        started = time.monotonic()
        with self.assertRaises(prepare_model.ModelPreparationError):
            prepare_model.read_body(response([(bytes([byte]), 0.03) for byte in b'{"models":[]}']), started + 0.12)
        self.assertLess(time.monotonic() - started, 0.35)

    def test_chunked_protocol_framing_obeys_process_wall_clock_deadline(self):
        def chunked_response(parts):
            client, server = socket.socketpair()
            server.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
            def write():
                try:
                    for part, delay in parts:
                        server.sendall(part)
                        time.sleep(delay)
                except BrokenPipeError:
                    pass
                finally:
                    server.close()
            writer = threading.Thread(target=write)
            writer.start()
            result = HTTPResponse(client)
            result.begin()
            self.addCleanup(client.close)
            self.addCleanup(writer.join, 1)
            return result, writer

        class ContextResponse:
            def __init__(self, value): self.value = value
            def __enter__(self): return self.value
            def __exit__(self, *_args): self.value.close(); return False

        tags, tags_writer = chunked_response([(b"F", 0.03)] * 20)
        started = time.monotonic()
        with patch.object(prepare_model.urllib.request, "urlopen", return_value=ContextResponse(tags)):
            with self.assertRaises(prepare_model.ModelPreparationError):
                with prepare_model.wall_clock_deadline(started + 0.12):
                    prepare_model.request_json("/api/tags", deadline=started + 0.12)
        self.assertLess(time.monotonic() - started, 0.35)
        tags_writer.join(1)
        self.assertFalse(tags_writer.is_alive())

        event = b'{"status":"done"}\n'
        first_chunk = f"{len(event):X}\r\n".encode() + event + b"\r\n0\r\nX"
        pull, pull_writer = chunked_response([(first_chunk, 0.03)] + [(b"X", 0.03)] * 20)
        started = time.monotonic()
        with patch.object(prepare_model.urllib.request, "urlopen", return_value=ContextResponse(pull)):
            with self.assertRaises(prepare_model.ModelPreparationError):
                with prepare_model.wall_clock_deadline(started + 0.12):
                    prepare_model.pull_model("qwen2.5:1.5b-instruct", deadline=started + 0.12)
        self.assertLess(time.monotonic() - started, 0.35)
        pull_writer.join(1)
        self.assertFalse(pull_writer.is_alive())

    def test_wall_clock_deadline_rejects_off_main_thread(self):
        errors = []
        def run():
            try:
                with prepare_model.wall_clock_deadline(time.monotonic() + 1):
                    pass
            except Exception as error:
                errors.append(error)
        worker = threading.Thread(target=run)
        worker.start()
        worker.join(1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], prepare_model.ModelPreparationError)
        self.assertIn("main thread", str(errors[0]))

    def test_wall_clock_deadline_rejects_and_preserves_active_timers(self):
        original_handler = signal.getsignal(signal.SIGALRM)
        original_timer = signal.getitimer(signal.ITIMER_REAL)
        fired = []
        def handler(_signum, _frame): fired.append("alarm")
        try:
            signal.signal(signal.SIGALRM, handler)
            for delay, interval in ((0.05, 0.0), (0.05, 0.05)):
                signal.setitimer(signal.ITIMER_REAL, delay, interval)
                with self.assertRaisesRegex(prepare_model.ModelPreparationError, "another ITIMER_REAL"):
                    with prepare_model.wall_clock_deadline(time.monotonic() + 1):
                        pass
                remaining, preserved_interval = signal.getitimer(signal.ITIMER_REAL)
                self.assertGreater(remaining, 0)
                self.assertEqual(preserved_interval, interval)
                time.sleep(0.12)
                self.assertTrue(fired)
                fired.clear()
                signal.setitimer(signal.ITIMER_REAL, 0)
            with self.assertRaises(ValueError):
                with prepare_model.wall_clock_deadline(time.monotonic() + 1):
                    raise ValueError("expected")
            self.assertIs(signal.getsignal(signal.SIGALRM), handler)
            self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, original_handler)
            if original_timer[0] > 0 or original_timer[1] > 0:
                signal.setitimer(signal.ITIMER_REAL, *original_timer)

    def test_unicode_errors_retry_as_model_preparation_errors_and_main_fails_cleanly(self):
        with self.assertRaises(prepare_model.ModelPreparationError):
            prepare_model.decode_json(b"\xff", "tags")
        with patch.object(prepare_model, "request_json", side_effect=[prepare_model.ModelPreparationError("bad utf-8"), {"models": []}]), \
             patch.object(prepare_model.time, "monotonic", return_value=0), \
             patch.object(prepare_model.time, "sleep"):
            prepare_model.wait_for_api(deadline=1)
        with patch.object(prepare_model, "prepare_model", side_effect=prepare_model.ModelPreparationError("bad utf-8")):
            self.assertEqual(prepare_model.main(), 1)


class WorkflowContractTests(unittest.TestCase):
    def test_container_packages_license_and_notice(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        self.assertIn("COPY LICENSE NOTICE ./", dockerfile)
        self.assertIn("!LICENSE", dockerignore)
        self.assertIn("!NOTICE", dockerignore)

    def test_only_strict_semver_pushes_can_publish(self):
        workflow = (ROOT / ".github/workflows/docker.yml").read_text(encoding="utf-8")
        match = re.search(r"=~ (\^v\([^\n]+\$)", workflow)
        self.assertIsNotNone(match)
        pattern = match.group(1)
        self.assertTrue(re.fullmatch(pattern, "v0.1.0"))
        for value in ("v01.2.3", "v1.02.3", "v1.2.03", "v1.2.3-rc.1", "v1.2", "version1.2.3"):
            self.assertIsNone(re.fullmatch(pattern, value), value)
        self.assertIn('"$RELEASE_REF_NAME"', workflow)
        self.assertNotIn('"${{ github.ref_name }}"', workflow)
        self.assertIn("workflow_dispatch", workflow)
        self.assertIn("publish:", workflow)
        self.assertIn("if: needs.validate.outputs.release_tag == 'true'", workflow)
        self.assertIn("packages: write", workflow)
        for action in ("docker/setup-qemu-action", "docker/setup-buildx-action", "actions/upload-artifact"):
            references = re.findall(rf"uses: {re.escape(action)}@([^\s]+)", workflow)
            self.assertTrue(references, action)
            self.assertTrue(all(re.fullmatch(r"[0-9a-f]{40}", reference) for reference in references), action)
        self.assertIn("platforms: linux/amd64,linux/arm64", workflow)

    def test_release_acceptance_is_manual_exact_image_linux_gate(self):
        workflow = (ROOT / ".github/workflows/release-acceptance.yml").read_text(encoding="utf-8")
        script = ROOT / "scripts/release_acceptance.sh"
        result = subprocess.run(["bash", "-n", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn("pull_request:", workflow)
        self.assertNotIn("push:", workflow)
        self.assertIn("runs-on: ubuntu-latest", workflow)
        self.assertIn("timeout-minutes: 50", workflow)
        self.assertRegex(workflow, r"app_image:[\s\S]+default: ghcr\.io/devitolo/paper-agent@sha256:[a-f0-9]{64}")
        self.assertRegex(workflow, r"ollama_image:[\s\S]+default: docker\.io/ollama/ollama@sha256:[a-f0-9]{64}")
        for action in ("actions/checkout", "actions/upload-artifact"):
            references = re.findall(rf"uses: {re.escape(action)}@([^\s]+)", workflow)
            self.assertTrue(references, action)
            self.assertTrue(all(re.fullmatch(r"[0-9a-f]{40}", reference) for reference in references), action)
        content = script.read_text(encoding="utf-8")
        for behavior in ("install_project_paper.sh", "/topics", "/scout/run", "/feedback", "force-recreate app", "scout_recovery=interrupted", "down --volumes"):
            self.assertIn(behavior, content)


if __name__ == "__main__":
    unittest.main()

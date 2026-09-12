"""One durable, bounded manual pipeline per app data directory."""
from __future__ import annotations

import contextvars
import fcntl
import functools
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from paper_agents import db
from paper_agents.local_extract import DEFAULT_MODEL
from paper_agents.runtime_config import ollama_url, packaged
from paper_agents.topics import DEFAULT_TOPIC_CONFIG_PATH, load_topic_config

RUN_TIMEOUT = 1800
ACTIVE = {"queued", "running"}
STATUSES = {"idle", "queued", "running", "completed", "empty", "failed", "interrupted"}
READINESS_TTL = 15 * 60
READINESS_RETRY_TTL = 60
READINESS_TIMEOUT = 20
_held = contextvars.ContextVar("paper_pipeline_lock", default=None)
_readiness_lock = threading.Lock()
_readiness_checks: set[Path] = set()


class ScoutBusy(RuntimeError):
    pass


def acquire(db_path: Path):
    path = db_path.resolve().parent / "scout.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise ScoutBusy("Scout is already running. Wait for it to finish before retrying.") from None
    return handle


def write_status(db_path: Path, state: dict) -> dict:
    directory = db_path.resolve().parent
    payload = {**state, "updated_at": int(time.time())}
    with tempfile.NamedTemporaryFile("w", dir=directory, delete=False, encoding="utf-8") as output:
        json.dump(payload, output)
        output.flush()
        os.fsync(output.fileno())
        temporary = Path(output.name)
    temporary.replace(directory / "scout-status.json")
    return payload


def _readiness_path(db_path: Path) -> Path:
    return db_path.resolve().parent / "model-readiness.json"


def _write_readiness(db_path: Path, state: dict) -> dict:
    directory = db_path.resolve().parent
    payload = {**state, "checked_at": int(time.time())}
    with tempfile.NamedTemporaryFile("w", dir=directory, delete=False, encoding="utf-8") as output:
        json.dump(payload, output)
        output.flush()
        os.fsync(output.fileno())
        temporary = Path(output.name)
    temporary.replace(_readiness_path(db_path))
    return payload


def _read_readiness(db_path: Path) -> dict | None:
    try:
        state = json.loads(_readiness_path(db_path).read_text(encoding="utf-8"))
        readiness_status = state.get("status") if isinstance(state, dict) else None
        if not isinstance(readiness_status, str) or readiness_status not in {"ready", "unavailable"}:
            return None
        if not isinstance(state.get("message"), str) or not isinstance(state.get("checked_at"), int):
            return None
        return state
    except (ValueError, OSError):
        return None


def _check_readiness(db_path: Path) -> None:
    try:
        from paper_agents.package_runtime import check_model
        check_model(timeout=READINESS_TIMEOUT)
        _write_readiness(db_path, {"status": "ready", "message": "Local Qwen inference is ready."})
    except Exception:
        _write_readiness(db_path, {"status": "unavailable",
            "message": "Local Qwen inference is unavailable. Saved papers remain available; inspect Runtime and retry after recovery."})
    finally:
        with _readiness_lock:
            _readiness_checks.discard(db_path.resolve())


def inference_readiness(db_path: Path, *, schedule: bool = True) -> dict:
    """Return a recent bounded inference result without probing on every UI poll."""
    path = db_path.resolve()
    cached = _read_readiness(path)
    now = int(time.time())
    if cached is not None:
        ttl = READINESS_TTL if cached["status"] == "ready" else READINESS_RETRY_TTL
        age = now - cached["checked_at"]
        if 0 <= age < ttl:
            return cached
    if not schedule:
        return {"status": "preparing", "message": "Checking local Qwen inference readiness."}
    with _readiness_lock:
        if path not in _readiness_checks:
            _readiness_checks.add(path)
            threading.Thread(target=_check_readiness, args=(path,), daemon=True,
                             name="paper-model-readiness").start()
    return {"status": "preparing", "message": "Checking local Qwen inference readiness."}


def _read(db_path: Path) -> dict:
    path = db_path.resolve().parent / "scout-status.json"
    if not path.exists():
        return {"status": "idle", "message": "Choose research topics, then run Scout."}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        scout_status = state.get("status") if isinstance(state, dict) else None
        if not isinstance(scout_status, str) or scout_status not in STATUSES:
            raise ValueError("invalid status")
        if not isinstance(state.get("message"), str) or not state["message"].strip():
            raise ValueError("invalid status message")
        if state["status"] in ACTIVE and (
                not isinstance(state.get("run_id"), str) or not state["run_id"] or
                not isinstance(state.get("topics"), list) or
                not all(isinstance(topic, str) and topic for topic in state["topics"])):
            raise ValueError("incomplete active status")
        return state
    except ValueError:
        return {"status": "failed", "message": "Scout status is incomplete or invalid. Saved papers are retained; inspect app logs and retry."}
    except OSError:
        return {"status": "failed", "message": "Scout status could not be read. Inspect app logs and retry."}


def status(db_path: Path) -> dict:
    """Reconcile abandoned state only while owning the same cross-process lock."""
    try:
        handle = acquire(db_path)
    except ScoutBusy:
        return {**_read(db_path), "busy": True}
    try:
        state = _read(db_path)
        if state["status"] in ACTIVE:
            state = write_status(db_path, {**state, "status": "interrupted",
                "message": "Scout was interrupted. Saved papers are retained; retry when ready."})
        return {**state, "busy": False}
    finally:
        handle.close()


def selected_topics(path: Path = DEFAULT_TOPIC_CONFIG_PATH) -> list[str]:
    return list(dict.fromkeys(topic.query for topic in load_topic_config(path)
        if topic.enabled and "arxiv" in topic.sources))


def terminal_state(result: dict) -> dict:
    recommendations = (result.get("curator") or {}).get("recommendations") or []
    common = {"workflow_cycle_id": result.get("workflow_cycle_id"), "recommendations": len(recommendations)}
    if recommendations:
        return {**common, "status": "completed", "message": f"Scout completed: {len(recommendations)} recommendations available. Review papers below; Health has any source/extraction warnings."}
    if (result.get("cycle") or {}).get("state") == "failed" or any(
            item.get("errors") for item in result.get("scout_results", [])):
        return {**common, "status": "failed", "message": "arXiv discovery failed. Saved papers are retained. Check Health/source logs, then retry after the source recovers."}
    return {**common, "status": "empty", "message": "Scout finished without recommendations. Check your topics or broaden their queries, then retry later; source rate limits still apply."}


def exclusive_pipeline(function):
    """Guard the existing full pipeline, including direct CLI/container callers."""
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        path = kwargs.get("db_path", db.DEFAULT_DB_PATH)
        if path is None or (not packaged() and _held.get() is None):
            return function(*args, **kwargs)
        path = Path(path).resolve()
        if _held.get() == path:
            return function(*args, **kwargs)
        handle = acquire(path)
        token = _held.set(path)
        state = {"run_id": uuid.uuid4().hex, "status": "running", "origin": "cli", "topics": [],
                 "message": "Scout pipeline is running.", "started_at": int(time.time())}
        try:
            write_status(path, state)
            result = function(*args, **kwargs)
            write_status(path, {**state, **terminal_state(result)})
            return result
        except Exception:
            write_status(path, {**state, "status": "failed", "message": "Scout pipeline failed. Check its logs and retry; saved papers are retained."})
            raise
        finally:
            _held.reset(token)
            handle.close()
    return wrapped


def _supervise(process, handle, db_path: Path, state: dict, log) -> None:
    try:
        try:
            code = process.wait(timeout=RUN_TIMEOUT)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            write_status(db_path, {**state, "status": "failed", "message": "Scout exceeded its 30-minute limit and stopped. Saved results remain available; check logs and retry."})
            return
        current = _read(db_path)
        if current.get("status") in ACTIVE:
            write_status(db_path, {**state, "status": "failed" if code else "interrupted",
                "message": "Scout worker stopped before completing. Saved papers remain; inspect data/scout-last.log and retry."})
    finally:
        log.close()
        handle.close()


def start(db_path: Path, config_path: Path = DEFAULT_TOPIC_CONFIG_PATH) -> dict:
    queries = selected_topics(config_path)
    if not queries:
        raise ValueError("Add at least one enabled arXiv topic in Topics before running Scout.")
    readiness = inference_readiness(db_path)
    if readiness["status"] != "ready":
        raise RuntimeError(readiness["message"])
    path = db_path.resolve()
    handle = acquire(path)
    state = {"run_id": uuid.uuid4().hex, "status": "queued", "origin": "web", "topics": queries,
             "source": "arxiv", "started_at": int(time.time()), "message": "Queued: checking the local model before arXiv discovery."}
    log = None
    process = None
    try:
        write_status(path, state)
        log = (path.parent / "scout-last.log").open("w", encoding="utf-8")
        process = subprocess.Popen([sys.executable, "-m", "paper_agents.package_runtime", "manual-scout",
                                    str(path), str(handle.fileno())],
            pass_fds=(handle.fileno(),), stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        threading.Thread(target=_supervise, args=(process, handle, path, state, log), daemon=True,
                         name="paper-manual-scout").start()
        return state
    except Exception:
        if process is not None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        write_status(path, {**state, "status": "failed", "message": "Could not start Scout. Check app logs and retry."})
        if log is not None:
            log.close()
        handle.close()
        raise


def worker(db_path: Path, descriptor: int) -> int:
    # The parent retains the same lock until this process and status finalization end.
    if os.fstat(descriptor).st_ino != (db_path.parent / "scout.lock").stat().st_ino:
        raise RuntimeError("Invalid inherited Scout lock")
    token = _held.set(db_path.resolve())
    state = _read(db_path)
    try:
        write_status(db_path, {**state, "status": "running", "message": "Checking local Qwen inference readiness."})
        from paper_agents.package_runtime import check_model
        check_model(timeout=120)
        _write_readiness(db_path, {"status": "ready", "message": "Local Qwen inference is ready."})
        write_status(db_path, {**state, "status": "running", "message": "Running arXiv Scout, Curator and paper extraction. You can keep reviewing papers."})
        from paper_agents.pipeline import run_daily_pipeline
        result = run_daily_pipeline(topics=state["topics"], source_name="arxiv", db_path=db_path,
            profile_path=db_path.parent / "profile.json", scout_dir=db_path.parent / "scout",
            pdf_dir=db_path.parent / "papers", model=DEFAULT_MODEL, ollama_url=ollama_url(),
            fetch_limit=20, keep_limit=3, max_scout_attempts=1, request_delay=5,
            scout_retries=2, scout_timeout=60, timeout=300, workers=1, limit_chunks=2, mode="quick")
        write_status(db_path, {**state, **terminal_state(result)})
        return 0
    except Exception:
        import traceback
        traceback.print_exc()
        _write_readiness(db_path, {"status": "unavailable",
            "message": "Local Qwen inference is unavailable. Saved papers remain available; inspect Runtime and retry after recovery."})
        write_status(db_path, {**state, "status": "failed", "message": "Scout or local model failed. Existing papers are safe. Check Runtime, Health and data/scout-last.log; retry after recovery."})
        return 1
    finally:
        _held.reset(token)


def worker_main(db_path: Path, descriptor: int) -> int:
    def deadline(signum, frame):
        raise TimeoutError("Manual Scout exceeded its 30-minute deadline")
    signal.signal(signal.SIGALRM, deadline)
    signal.alarm(RUN_TIMEOUT)
    return worker(db_path, descriptor)


if __name__ == "__main__":
    raise SystemExit(worker_main(Path(sys.argv[1]), int(sys.argv[2])))

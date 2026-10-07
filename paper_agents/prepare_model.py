"""Prepare the packaged default Ollama model through its bounded HTTP API."""
from __future__ import annotations

import json
import os
import re
import signal
import sys
import time
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from typing import Any

from paper_agents.local_extract import DEFAULT_MODEL
from paper_agents.runtime_config import ollama_url


class ModelPreparationError(RuntimeError):
    """A recoverable model-preparation failure suitable for logs and retries."""

@contextmanager
def wall_clock_deadline(deadline: float):
    """Interrupt blocking HTTP parsing; refuse to coexist with an active real timer."""
    if not hasattr(signal, "setitimer") or not hasattr(signal, "SIGALRM"):
        raise ModelPreparationError("model preparation requires POSIX SIGALRM deadline support")
    if threading.current_thread() is not threading.main_thread():
        raise ModelPreparationError("model preparation must run on the main thread to enforce its wall-clock deadline")
    delay = remaining_timeout(deadline)
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_delay, previous_interval = signal.getitimer(signal.ITIMER_REAL)
    if previous_delay > 0 or previous_interval > 0:
        raise ModelPreparationError("model preparation cannot run while another ITIMER_REAL timer is active")
    def expired(_signum: int, _frame: Any) -> None:
        raise ModelPreparationError("model preparation timed out")

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, delay)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def model_api_base() -> str:
    return ollama_url().removesuffix("/api/generate")


def remaining_timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ModelPreparationError("model preparation timed out")
    return remaining


def _response_socket(response: Any) -> Any | None:
    """Find urllib's underlying socket without relying on a specific response wrapper."""
    current = response
    for attribute in ("fp", "raw", "_sock", "sock"):
        current = getattr(current, attribute, None)
        if current is None:
            return None
        if hasattr(current, "settimeout"):
            return current
    return None


def read_byte(response: Any, deadline: float) -> bytes:
    timeout = remaining_timeout(deadline)
    sock = _response_socket(response)
    if sock is not None:
        sock.settimeout(timeout)
    try:
        chunk = response.read(1)
    except OSError as error:
        raise ModelPreparationError(f"Ollama response body timed out or failed: {error}") from error
    remaining_timeout(deadline)
    if not isinstance(chunk, bytes):
        raise ModelPreparationError("Ollama returned malformed response body")
    return chunk


def read_body(response: Any, deadline: float) -> bytes:
    """Read one byte at a time so a trickling body cannot bypass the overall deadline."""
    body = bytearray()
    while (chunk := read_byte(response, deadline)):
        body.extend(chunk)
    return bytes(body)


def decode_json(raw: bytes, description: str) -> Any:
    try:
        text = raw.decode("utf-8")
        return json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ModelPreparationError(f"Ollama returned malformed {description} JSON") from error


def request_json(path: str, payload: dict[str, Any] | None = None, *, timeout: float = 5, deadline: float | None = None) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(model_api_base() + path, data=data, headers={"Content-Type": "application/json"})
    deadline = time.monotonic() + timeout if deadline is None else deadline
    try:
        with urllib.request.urlopen(request, timeout=remaining_timeout(deadline)) as response:
            result = decode_json(read_body(response, deadline), path)
    except (ModelPreparationError, OSError, urllib.error.URLError) as error:
        raise ModelPreparationError(f"Ollama request failed for {path}: {error}") from error
    if not isinstance(result, dict):
        raise ModelPreparationError(f"Ollama returned malformed JSON for {path}")
    return result


def validated_models(payload: dict[str, Any]) -> list[str]:
    if not isinstance(payload, dict):
        raise ModelPreparationError("Ollama returned malformed model listing")
    models = payload.get("models")
    if not isinstance(models, list):
        raise ModelPreparationError("Ollama returned malformed model listing")
    names: list[str] = []
    for item in models:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"].strip():
            raise ModelPreparationError("Ollama returned malformed model entry")
        names.append(item["name"])
    return names


def wait_for_api(deadline: float) -> None:
    print("waiting: Ollama API", flush=True)
    last_error: Exception | None = None
    while True:
        try:
            payload = request_json("/api/tags", timeout=remaining_timeout(deadline), deadline=deadline)
            validated_models(payload)
            remaining_timeout(deadline)
            return
        except (ModelPreparationError, OSError, urllib.error.URLError) as error:
            last_error = error
            try:
                time.sleep(min(2, remaining_timeout(deadline)))
            except ModelPreparationError:
                break
    raise ModelPreparationError(f"Ollama API did not become ready before model preparation timed out: {last_error}")


def model_present(model: str, deadline: float) -> bool:
    payload = request_json("/api/tags", timeout=remaining_timeout(deadline), deadline=deadline)
    names = validated_models(payload)
    remaining_timeout(deadline)
    return model in names


def validate_pull_event(event: Any) -> dict[str, Any]:
    if not isinstance(event, dict):
        raise ModelPreparationError("Ollama returned malformed pull event")
    if not any(key in event for key in ("status", "error", "done")):
        raise ModelPreparationError("Ollama pull event has no status")
    if "status" in event and (not isinstance(event["status"], str) or not event["status"].strip()):
        raise ModelPreparationError("Ollama pull event has malformed status")
    if "error" in event and (not isinstance(event["error"], str) or not event["error"].strip()):
        raise ModelPreparationError("Ollama pull event has malformed error")
    if "done" in event and not isinstance(event["done"], bool):
        raise ModelPreparationError("Ollama pull event has malformed completion flag")
    return event


def _consume_pull_event(raw: bytes) -> None:
    if not raw.strip():
        return
    event = decode_json(raw, "pull event")
    event = validate_pull_event(event)
    if "status" in event:
        print(f"pull status: {event['status']}", flush=True)
    if "error" in event:
        raise ModelPreparationError(f"Ollama model pull failed: {event['error']}")


def pull_model(model: str, deadline: float) -> None:
    request = urllib.request.Request(model_api_base() + "/api/pull", data=json.dumps({"name": model, "stream": True}).encode("utf-8"), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=remaining_timeout(deadline)) as response:
            buffered = bytearray()
            while True:
                # A newline is only a framing convenience; each byte is deadline-bound.
                raw = read_byte(response, deadline)
                if not raw:
                    break
                buffered.extend(raw)
                if raw == b"\n":
                    _consume_pull_event(bytes(buffered))
                    buffered.clear()
            if buffered:
                _consume_pull_event(bytes(buffered))
    except ModelPreparationError:
        raise
    except (OSError, urllib.error.URLError) as error:
        raise ModelPreparationError(f"Ollama model pull failed: {error}") from error


def prepare_model(model: str = DEFAULT_MODEL, *, budget: int = 1800, attempts: int = 2) -> None:
    if budget <= 0 or attempts <= 0:
        raise ModelPreparationError("invalid model preparation limits")
    deadline = time.monotonic() + budget
    with wall_clock_deadline(deadline):
        wait_for_api(deadline)
        if model_present(model, deadline):
            remaining_timeout(deadline)
            print(f"ready: {model} already present", flush=True)
            return
        for attempt in range(1, attempts + 1):
            try:
                remaining_timeout(deadline)
                print(f"pulling: {model} (attempt {attempt}/{attempts})", flush=True)
                pull_model(model, deadline)
                if model_present(model, deadline):
                    remaining_timeout(deadline)
                    print("ready: model present; installer performs a bounded inference check before success", flush=True)
                    return
                raise ModelPreparationError("model pull finished without making the model available")
            except (ModelPreparationError, OSError, urllib.error.URLError) as error:
                if attempt >= attempts:
                    raise ModelPreparationError(f"model pull failed: {error}") from error
                print(f"retrying after model pull failure: {error}", flush=True)
                time.sleep(min(2, remaining_timeout(deadline)))
    raise ModelPreparationError("model pull finished without making the model available")


def verify_model_listing(payload: dict, model: str, expected_digest: str) -> dict:
    """GET /api/tags digest is the full local manifest identity, not short CLI ID."""
    if not isinstance(expected_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
        raise ModelPreparationError("Expected full lowercase 64-character model manifest digest")
    validated_models(payload)
    matches = [item for item in payload["models"] if item["name"] == model]
    if len(matches) != 1 or matches[0].get("digest") != expected_digest:
        raise ModelPreparationError("Required local model absent, ambiguous, or digest mismatch; no pull attempted")
    if matches[0].get("model", model) != model:
        raise ModelPreparationError("Model listing name/model mismatch")
    return {"model":model, "digest":expected_digest, "identity":"verified", "inference":"unchecked"}


def verify_model(model: str, expected_digest: str, *, budget: int = 30) -> dict:
    # Validate before making even the single read-only API request.
    if not isinstance(expected_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
        raise ModelPreparationError("Expected full lowercase 64-character model manifest digest")
    if not 0 < budget <= 120:
        raise ModelPreparationError("Verify-only budget must be within 1..120 seconds")
    deadline = time.monotonic() + budget
    with wall_clock_deadline(deadline):
        payload = request_json("/api/tags", timeout=remaining_timeout(deadline), deadline=deadline)
        remaining_timeout(deadline)
        return verify_model_listing(payload, model, expected_digest)


def positive_int_env(name: str, default: str) -> int:
    value = os.environ.get(name, default)
    if not value.isdigit() or int(value) <= 0:
        raise ModelPreparationError(f"{name} must be a positive integer")
    return int(value)


def main() -> int:
    def stop(_signum: int, _frame: object) -> None:
        raise ModelPreparationError("model preparation timed out or interrupted; inspect logs and retry")
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        if os.environ.get("PAPER_MIGRATION_CONTRACT") == "1":
            from paper_agents.migration_lifecycle import validate_container_contract
            validate_container_contract()
        mode = os.environ.get("PAPER_MODEL_MODE", "prepare")
        if mode == "verify":
            print(json.dumps(verify_model(os.environ.get("PAPER_AGENT_MODEL", DEFAULT_MODEL),
                os.environ.get("PAPER_AGENT_EXPECTED_MODEL_DIGEST", ""),
                budget=positive_int_env("PAPER_MODEL_VERIFY_TIMEOUT", "30"))))
            return 0
        if mode != "prepare":
            raise ModelPreparationError("Invalid PAPER_MODEL_MODE")
        if os.environ.get("PAPER_AGENT_STARTUP_MODE") == "imported":
            raise ModelPreparationError("Imported state requires verify-only model mode")
        prepare_model(os.environ.get("PAPER_AGENT_MODEL", DEFAULT_MODEL), budget=positive_int_env("PAPER_MODEL_PREPARE_TIMEOUT", "1800"), attempts=positive_int_env("PAPER_MODEL_PULL_ATTEMPTS", "2"))
        return 0
    except ModelPreparationError as error:
        print(f"failed: {error}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


DEFAULT_MODEL = "gpt-5.1"
RESPONSES_URL = "https://api.openai.com/v1/responses"


def call_openai_json(system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set. Export it before running the agents.")

    request_body = {
        "model": os.getenv("OPENAI_MODEL", DEFAULT_MODEL),
        "instructions": system_prompt,
        "input": [
            {
                "role": "user",
                "content": "Return JSON for this payload:\n" + json.dumps(user_payload, indent=2),
            }
        ],
        "text": {"format": {"type": "json_object"}},
    }
    request = urllib.request.Request(
        RESPONSES_URL,
        data=json.dumps(request_body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            response_body = json.loads(response.read())
    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        if error.code == 429 and "insufficient_quota" in details:
            raise RuntimeError(
                "OpenAI API quota is not available for this key/project. "
                "Check billing, usage limits, or select a project with available credits."
            ) from error
        raise RuntimeError(f"OpenAI API request failed: {error.code} {details}") from error

    return json.loads(_extract_output_text(response_body))


def call_openai_text(system_prompt: str, user_content: str, *, timeout: int = 180) -> str:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set. Export it before running the agents.")

    request_body = {
        "model": os.getenv("OPENAI_MODEL", DEFAULT_MODEL),
        "instructions": system_prompt,
        "input": [
            {
                "role": "user",
                "content": user_content,
            }
        ],
    }
    request = urllib.request.Request(
        RESPONSES_URL,
        data=json.dumps(request_body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_body = json.loads(response.read())
    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        if error.code == 429 and "insufficient_quota" in details:
            raise RuntimeError(
                "OpenAI API quota is not available for this key/project. "
                "Check billing, usage limits, or select a project with available credits."
            ) from error
        raise RuntimeError(f"OpenAI API request failed: {error.code} {details}") from error

    return _extract_output_text(response_body)


def _extract_output_text(response_body: dict[str, Any]) -> str:
    chunks: list[str] = []
    for item in response_body.get("output", []):
        for content in item.get("content", []):
            text = content.get("text")
            if text:
                chunks.append(text)

    if not chunks:
        raise RuntimeError(f"OpenAI API response did not include output text: {response_body}")

    return "".join(chunks)

"""Explicit opt-in MiniLM transport. No retry, proxy, redirect, or embedded fallback."""
from __future__ import annotations
import json
import math
import urllib.error
import urllib.parse
import urllib.request
from .minilm_eval import MODEL_ID, MODEL_REVISION, MODEL_SHA256, QUERY_VERSION

API_VERSION = 1
MAX_BYTES = 262144
MAX_PAIRS = 32
IDENTITY = dict(model_id=MODEL_ID, model_revision=MODEL_REVISION,
                model_sha256=MODEL_SHA256, query_version=QUERY_VERSION)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class HTTPMiniLMScorer:
    def __init__(self, url: str, timeout: float = 10):
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != 'http' or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path not in ('', '/')
                or not 0 < timeout <= 30):
            raise ValueError('MiniLM requires an internal HTTP origin and timeout in (0,30]')
        self.url = url.rstrip('/') + '/v1/score'
        self.timeout = timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def score_pairs(self, pairs: list[dict]) -> list[float]:
        if not 1 <= len(pairs) <= MAX_PAIRS:
            raise ValueError('MiniLM batch must contain 1..32 pairs')
        body = json.dumps({'api_version': API_VERSION, 'pairs': pairs}, allow_nan=False).encode()
        if len(body) > MAX_BYTES:
            raise ValueError('MiniLM request exceeds byte limit')
        request = urllib.request.Request(self.url, data=body, headers={'Content-Type':'application/json'})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_BYTES+1)
            if len(raw) > MAX_BYTES:
                raise ValueError('oversized response')
            payload = json.loads(raw)
            if not isinstance(payload,dict):
                raise ValueError('invalid response object')
            if payload.get('api_version') != API_VERSION or payload.get('identity') != IDENTITY:
                raise ValueError('model/API identity mismatch')
            scores = payload['scores']
            if not isinstance(scores,list) or len(scores) != len(pairs):
                raise ValueError('score count mismatch')
            if any(type(x) not in (int,float) or not math.isfinite(x) for x in scores):
                raise ValueError('invalid score')
            return [float(x) for x in scores]
        except urllib.error.HTTPError as exc:
            code=exc.code
            exc.close()
            raise RuntimeError('MiniLM HTTP request failed: status ' + str(code)) from None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RuntimeError('MiniLM HTTP inference unavailable: ' + type(exc).__name__) from None

    def score_pair(self, title: str, abstract: str, interest: str) -> float:
        return self.score_pairs([dict(title=title, abstract=abstract, interest=interest)])[0]

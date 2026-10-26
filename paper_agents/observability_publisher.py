"""Bounded publisher, run separately from the network-isolated collector."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from paper_agents.observability_transport import (
    ENDPOINT, MAX_BYTES, TransportError, atomic, decode, encoded, identifier,
    locked, project, read_key, signature, validate,
)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def publish_once(status_file, state_file, url, key_file, events_file=None):
    # Disable gate precedes every file/network access, including credentials.
    if os.environ.get('PAPER_OBSERVABILITY_PUBLISH_ENABLED') != '1':
        return {'state': 'disabled'}
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.path != ENDPOINT or parsed.query or parsed.fragment:
        raise TransportError(400, 'approved HTTPS endpoint required')
    state_file = Path(state_file)
    with locked(state_file):
        try:
            with state_file.open('rb') as stream:
                pending_raw = stream.read(MAX_BYTES + 4097)
            if len(pending_raw) > MAX_BYTES + 4096:
                raise ValueError('pending state too large')
            pending = json.loads(pending_raw)
            if set(pending) != {'id', 'body'} or not isinstance(pending['body'], str):
                raise ValueError('invalid pending state')
            identifier(pending['id'])
            body = pending['body'].encode()
            validate(decode(body))
        except FileNotFoundError:
            with Path(status_file).open('rb') as stream:
                raw = stream.read(MAX_BYTES + 1)
            source = decode(raw)
            events = []
            if events_file:
                with Path(events_file).open('rb') as stream:
                    events = decode(stream.read(MAX_BYTES + 1))
            body = encoded(project(source, events))
            if len(body) > MAX_BYTES:
                raise TransportError(413, 'payload too large')
            pending = {'id': str(uuid.uuid4()), 'body': body.decode()}
            atomic(state_file, pending)
        key = read_key(key_file)
        opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()), urllib.request.ProxyHandler({}))
        for attempt in range(3):
            timestamp = str(int(time.time()))
            request = urllib.request.Request(url, data=body, method='POST', headers={
                'Content-Type': 'application/json',
                'X-Paper-Observability-Timestamp': timestamp,
                'X-Paper-Observability-Id': pending['id'],
                'X-Paper-Observability-Signature': signature(key, body, timestamp, pending['id']),
            })
            try:
                with opener.open(request, timeout=5) as response:
                    result = decode(response.read(MAX_BYTES + 1))
                    if response.status != 200 or set(result) != {'schema', 'id', 'observed_at', 'duplicate'} or result['id'] != pending['id'] or result['observed_at'] != decode(body)['status']['observed_at'] or type(result['duplicate']) is not bool or result['schema'] != 'project-paper.observability-delivery.v1':
                        raise TransportError(502, 'invalid receipt')
                state_file.unlink()
                return {'state': 'delivered', 'id': pending['id'], 'duplicate': result['duplicate']}
            except urllib.error.HTTPError as error:
                error.close()
                if error.code < 500:
                    raise TransportError(error.code, 'delivery rejected; pending evidence retained') from None
            except (OSError, urllib.error.URLError):
                pass
            if attempt < 2:
                time.sleep(attempt + 1)
        raise TransportError(503, 'delivery failed; pending evidence retained')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--status-file', type=Path, required=True)
    parser.add_argument('--state-file', type=Path, required=True)
    parser.add_argument('--url', required=True)
    parser.add_argument('--key-file', type=Path, required=True)
    parser.add_argument('--events-file', type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(publish_once(**vars(args))))
    except (OSError, ValueError, TypeError):
        # No path, key, body, response or raw exception text in diagnostics.
        print('{"state":"delivery_failed"}')
        raise SystemExit(1)


if __name__ == '__main__':
    main()

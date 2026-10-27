"""Dedicated, default-off resource telemetry transport; no paper sync or DB access."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
import uuid

ENDPOINT = '/observability/v1/mini-status'
MAX_BYTES = 32768
SCHEMA = 'project-paper.observability-delivery.v1'
STATUS_SCHEMA = 'project-paper.resource-status.v1'
STORE = Path('/var/lib/project-paper-observability-ingest/mini-state.json')
CPU = ('average_percent', 'max_observed_percent', 'peak_sustained_5m_percent', 'max_one_minute_percent')
MEMORY = ('available_average_bytes', 'available_max_bytes', 'available_min_bytes')
AVAILABILITY = {'observed', 'unconfigured', 'unavailable', 'unknown'}
SOURCES = {'arxiv', 'openalex', 'semantic', 'core', 'ssrn', 'zenml', 'collector', 'publisher'}
CODES = {'none', 'timeout', 'unavailable', 'rate_limited', 'validation_failed', 'delivery_failed', 'overlap'}


class TransportError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def enabled():
    return os.environ.get('PAPER_OBSERVABILITY_INGEST_ENABLED') == '1'


def exact(value, fields):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise TransportError(400, 'invalid fields')


def number(value, low, high, *, integer=False, nullable=False):
    if value is None and nullable:
        return
    if isinstance(value, bool) or not isinstance(value, int if integer else (int, float)):
        raise TransportError(400, 'invalid number')
    if not low <= value <= high or not math.isfinite(float(value)):
        raise TransportError(400, 'number outside limits')


def stamp(value):
    if not isinstance(value, str) or len(value) > 40:
        raise TransportError(400, 'invalid timestamp')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError()
        return parsed.timestamp()
    except (ValueError, TypeError, OverflowError):
        raise TransportError(400, 'invalid UTC timestamp') from None


def identifier(value):
    if not isinstance(value, str):
        raise TransportError(400, 'invalid delivery ID')
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError()
    except ValueError:
        raise TransportError(400, 'invalid delivery ID') from None


def encoded(value):
    return json.dumps(value, allow_nan=False, sort_keys=True, separators=(',', ':')).encode()


def decode(body):
    if len(body) > MAX_BYTES:
        raise TransportError(413, 'body too large')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise TransportError(400, 'duplicate field')
            result[key] = value
        return result
    try:
        return json.loads(body, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise TransportError(400, 'invalid JSON') from None


def validate(payload, *, clock=None, persisted=False):
    clock = time.time() if clock is None else clock
    exact(payload, ('schema', 'producer', 'status', 'events'))
    if payload['schema'] != SCHEMA or payload['producer'] != 'mini-production':
        raise TransportError(400, 'invalid producer/schema')
    s = payload['status']
    exact(s, ('schema', 'host', 'environment', 'scope', 'state', 'collector_version', 'observed_at', 'window', 'sample_count', 'coverage_seconds', 'cpu', 'memory', 'latest'))
    if (s['schema'], s['host'], s['environment'], s['scope']) != (STATUS_SCHEMA, 'mini-production', 'mini-production', 'host'):
        raise TransportError(400, 'invalid status identity')
    if s['state'] not in ('observed', 'warming') or not isinstance(s['collector_version'], str) or not re.fullmatch(r'[0-9]{1,8}', s['collector_version']):
        raise TransportError(400, 'invalid collector state')
    observed = stamp(s['observed_at'])
    if not persisted and not clock - 2700 <= observed <= clock + 300:
        raise TransportError(400, 'stale or future status')
    w = s['window']
    exact(w, ('start_at', 'end_at', 'interval_seconds', 'complete', 'sample_gaps'))
    start, end = stamp(w['start_at']), stamp(w['end_at'])
    if end - start != 900 or end > observed + 300 or end < observed - 2700 or type(w['complete']) is not bool:
        raise TransportError(400, 'invalid window')
    number(w['interval_seconds'], 900, 900, integer=True)
    number(w['sample_gaps'], 0, 100, integer=True)
    number(s['sample_count'], 0, 100, integer=True)
    number(s['coverage_seconds'], 0, 900, integer=True)
    exact(s['cpu'], CPU)
    for value in s['cpu'].values():
        number(value, 0, 100, nullable=True)
    if any(s['cpu'][key] is not None for key in CPU[2:]):
        raise TransportError(400, 'unmeasured peak')
    if w['complete'] and (s['coverage_seconds'] != 900 or not s['sample_count'] or w['sample_gaps'] or s['cpu']['average_percent'] is None):
        raise TransportError(400, 'contradictory complete window')
    if (s['state'] == 'observed') != w['complete']:
        raise TransportError(400, 'contradictory state')
    if s['cpu']['average_percent'] is not None and (s['cpu']['max_observed_percent'] is None or s['cpu']['average_percent'] > s['cpu']['max_observed_percent']):
        raise TransportError(400, 'invalid CPU maximum')
    exact(s['memory'], MEMORY)
    for value in s['memory'].values():
        number(value, 0, 2**63 - 1, integer=True, nullable=True)
    vals = [s['memory'][key] for key in MEMORY]
    if all(v is not None for v in vals) and not vals[2] <= vals[0] <= vals[1]:
        raise TransportError(400, 'invalid memory maximum')
    latest = s['latest']
    exact(latest, ('observed_at', 'disk', 'containers', 'temperature', 'cgroup'))
    if latest['observed_at'] is not None and stamp(latest['observed_at']) > observed + 300:
        raise TransportError(400, 'future observation')
    disk = latest['disk']
    if disk is not None:
        exact(disk, ('capacity_bytes', 'available_bytes', 'used_percent', 'inode_used_percent'))
        for key in ('capacity_bytes', 'available_bytes'):
            number(disk[key], 0, 2**63 - 1, integer=True, nullable=True)
        for key in ('used_percent', 'inode_used_percent'):
            number(disk[key], 0, 100, nullable=True)
        if disk['capacity_bytes'] is not None and disk['available_bytes'] is not None and disk['available_bytes'] > disk['capacity_bytes']:
            raise TransportError(400, 'invalid disk capacity')
    validate_container_health(latest['containers'], clock=clock, persisted=persisted)
    for key in ('temperature', 'cgroup'):
        exact(latest[key], ('state',))
        if not isinstance(latest[key]['state'], str) or latest[key]['state'] not in AVAILABILITY:
            raise TransportError(400, 'invalid availability')
    events = payload['events']
    if not isinstance(events, list) or len(events) > 20:
        raise TransportError(400, 'invalid events')
    seen = set()
    for event in events:
        exact(event, ('id', 'observed_at', 'source', 'state', 'code'))
        identifier(event['id'])
        event_time = stamp(event['observed_at'])
        if event['id'] in seen or event_time > observed + 300 or (not persisted and not clock - 604800 <= event_time <= clock + 300):
            raise TransportError(400, 'invalid event time/ID')
        seen.add(event['id'])
        if any(not isinstance(event[key], str) for key in ('source', 'state', 'code')) or event['source'] not in SOURCES or event['state'] not in ('started', 'completed', 'failed', 'skipped') or event['code'] not in CODES:
            raise TransportError(400, 'invalid event enum')
    return payload


CONTAINER_SERVICES = ('app', 'minilm', 'ollama', 'phoenix', 'observability-collector', 'observability-publisher')


def validate_container_health(value, *, clock=None, persisted=False):
    if isinstance(value, dict) and set(value) == {'state'}:
        if not isinstance(value['state'], str) or value['state'] not in AVAILABILITY:
            raise TransportError(400, 'invalid container availability')
        return value
    exact(value, ('state', 'observed_at', 'items'))
    if value['state'] != 'observed':
        raise TransportError(400, 'invalid container state')
    observed = stamp(value['observed_at'])
    clock = time.time() if clock is None else clock
    if not persisted and not clock - 2700 <= observed <= clock + 300:
        raise TransportError(400, 'invalid container observation time')
    items = value['items']
    if not isinstance(items, list) or len(items) != len(CONTAINER_SERVICES):
        raise TransportError(400, 'invalid container inventory')
    for service, item in zip(CONTAINER_SERVICES, items):
        exact(item, ('service', 'status', 'health', 'image_digest', 'image_id', 'cpu_percent', 'memory_bytes', 'memory_limit_bytes'))
        if item['service'] != service:
            raise TransportError(400, 'invalid container service')
        if not isinstance(item['status'], str) or item['status'] not in ('created', 'running', 'paused', 'restarting', 'removing', 'exited', 'dead', 'unavailable'):
            raise TransportError(400, 'invalid container status')
        if not isinstance(item['health'], str) or item['health'] not in ('healthy', 'unhealthy', 'starting', 'no-healthcheck', 'unavailable'):
            raise TransportError(400, 'invalid container health')
        for field in ('image_digest', 'image_id'):
            if item[field] is not None and (not isinstance(item[field], str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', item[field])):
                raise TransportError(400, 'invalid container digest')
        number(item['cpu_percent'], 0, 6400, nullable=True)
        for field in ('memory_bytes', 'memory_limit_bytes'):
            number(item[field], 0, 2**63-1, integer=True, nullable=True)
        if item['status'] != 'running' and any(item[field] is not None for field in ('cpu_percent', 'memory_bytes', 'memory_limit_bytes')):
            raise TransportError(400, 'invalid stopped container measurement')
        if item['memory_bytes'] is not None and item['memory_limit_bytes'] is not None and item['memory_bytes'] > item['memory_limit_bytes']:
            raise TransportError(400, 'invalid container memory')
    return value


def project(status, events=(), *, clock=None):
    """Build an allowlisted projection; never send source paths or free text."""
    if not isinstance(status, dict) or any(not isinstance(status.get(key), dict) for key in ('cpu', 'memory', 'latest')):
        raise TransportError(400, 'invalid source status')
    fields = ('schema', 'host', 'environment', 'scope', 'state', 'collector_version', 'observed_at', 'window', 'sample_count', 'coverage_seconds')
    s = {key: status.get(key) for key in fields}
    s['cpu'] = {key: status.get('cpu', {}).get(key) for key in CPU}
    s['memory'] = {key: status.get('memory', {}).get(key) for key in MEMORY}
    latest = status.get('latest') or {}
    disk = latest.get('disk')
    if disk is not None and not isinstance(disk, dict):
        raise TransportError(400, 'invalid source disk')
    s['latest'] = {'observed_at': latest.get('observed_at'), 'disk': None if disk is None else {key: disk.get(key) for key in ('capacity_bytes', 'available_bytes', 'used_percent', 'inode_used_percent')}}
    for key in ('containers', 'temperature', 'cgroup'):
        if latest.get(key) is not None and not isinstance(latest[key], dict):
            raise TransportError(400, 'invalid source availability')
        state = (latest.get(key) or {}).get('state', 'unknown')
        s['latest'][key] = {'state': state if isinstance(state, str) and state in AVAILABILITY else 'unknown'}
    if isinstance(latest.get('containers'), dict) and 'items' in latest['containers']:
        s['latest']['containers'] = validate_container_health(latest['containers'], clock=clock)
    return validate({'schema': SCHEMA, 'producer': 'mini-production', 'status': s, 'events': list(events)}, clock=clock)


def read_key(path):
    with Path(path).open('rb') as stream:
        key = stream.read(129).strip()
    if not re.fullmatch(b'[0-9a-f]{64}', key):
        raise TransportError(503, 'transport key unavailable')
    return bytes.fromhex(key.decode())


def signature(key, body, timestamp, delivery_id):
    message = '\n'.join(('POST', ENDPOINT, timestamp, delivery_id, hashlib.sha256(body).hexdigest())).encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def authenticate(headers, body, key, *, clock=None):
    clock = time.time() if clock is None else clock
    timestamp = headers.get('X-Paper-Observability-Timestamp', '')
    delivery_id = headers.get('X-Paper-Observability-Id', '')
    identifier(delivery_id)
    if not re.fullmatch(r'[0-9]{1,12}', timestamp) or abs(clock - int(timestamp)) > 300:
        raise TransportError(401, 'authentication failed')
    provided = headers.get('X-Paper-Observability-Signature', '')
    if not re.fullmatch(r'[0-9a-f]{64}', provided) or not hmac.compare_digest(provided, signature(key, body, timestamp, delivery_id)):
        raise TransportError(401, 'authentication failed')
    return delivery_id


@contextmanager
def locked(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    fd = os.open(str(path) + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def atomic(path, value):
    data = encoded(value)
    fd, name = tempfile.mkstemp(prefix='.telemetry-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o640)
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_state(path):
    # State has one payload and up to 256 small receipt entries.
    with Path(path).open('rb') as stream:
        raw = stream.read(131073)
    if len(raw) > 131072:
        raise TransportError(503, 'stored state invalid')
    try:
        state = json.loads(raw)
        exact(state, ('payload', 'received_at', 'hash', 'receipts'))
        validate(state['payload'], persisted=True)
        stamp(state['received_at'])
        if not isinstance(state['receipts'], list) or not 1 <= len(state['receipts']) <= 256:
            raise ValueError()
        for receipt in state['receipts']:
            exact(receipt, ('id', 'hash'))
            identifier(receipt['id'])
            if not re.fullmatch(r'[0-9a-f]{64}', receipt['hash']):
                raise ValueError()
        if not re.fullmatch(r'[0-9a-f]{64}', state['hash']):
            raise ValueError()
        return state
    except (ValueError, TypeError, KeyError, RecursionError):
        raise TransportError(503, 'stored state invalid') from None


def ingest(path, body, delivery_id, *, clock=None):
    clock = time.time() if clock is None else clock
    identifier(delivery_id)
    payload = validate(decode(body), clock=clock)
    digest = hashlib.sha256(body).hexdigest()
    path = Path(path)
    with locked(path):
        try:
            previous = read_state(path)
        except FileNotFoundError:
            previous = None
        duplicate = False
        receipts = [] if previous is None else previous['receipts']
        if previous:
            matching = next((r for r in receipts if r['id'] == delivery_id), None)
            if matching:
                if matching['hash'] != digest:
                    raise TransportError(409, 'delivery ID conflict')
                duplicate = True
            elif stamp(payload['status']['observed_at']) < stamp(previous['payload']['status']['observed_at']):
                raise TransportError(409, 'older status')
            elif stamp(payload['status']['observed_at']) == stamp(previous['payload']['status']['observed_at']):
                if previous['hash'] != digest:
                    raise TransportError(409, 'timestamp conflict')
                duplicate = True
        if duplicate and not matching:
            # Remember new IDs used for an otherwise identical latest body,
            # without changing the telemetry or its receipt time.
            atomic(path, {**previous, 'receipts': (receipts + [{'id': delivery_id, 'hash': digest}])[-256:]})
        if not duplicate:
            state = {'payload': payload, 'received_at': datetime.fromtimestamp(clock, timezone.utc).isoformat(), 'hash': digest, 'receipts': (receipts + [{'id': delivery_id, 'hash': digest}])[-256:]}
            atomic(path, state)
    return {'schema': SCHEMA, 'id': delivery_id, 'observed_at': payload['status']['observed_at'], 'duplicate': duplicate}


def health_status(path=None, *, clock=None):
    """Read the received Mini snapshot without locks, writes or secret access.

    A receipt proves the last accepted delivery only; it cannot report the
    outcome of a later publisher attempt that never reached AWS.
    """
    clock = time.time() if clock is None else clock
    unavailable = {'state': 'unavailable', 'freshness': 'unavailable',
                   'source': 'Mini', 'observed_at': None, 'received_at': None,
                   'age_seconds': None}
    try:
        saved = read_state(path or os.environ.get('PAPER_OBSERVABILITY_STATE_FILE', STORE))
        status = dict(saved['payload']['status'])
        observed, received = stamp(status['observed_at']), stamp(saved['received_at'])
        if max(observed, received) > clock + 300 or observed > received + 300:
            return {**unavailable, 'error': 'Telemetry timestamps are inconsistent or in the future.'}
        age = max(0, int(clock - observed))
        stale = age > 2700 or clock - received > 2700
        return {**status, 'source': 'Mini', 'received_at': saved['received_at'],
                'age_seconds': age, 'freshness': 'stale' if stale else 'fresh',
                'state': 'stale' if stale else status['state'],
                'error': 'No fresh Mini telemetry received within 45 minutes.' if stale else None}
    except FileNotFoundError:
        return {**unavailable, 'error': 'No Mini telemetry has been received.'}
    except OSError:
        return {**unavailable, 'error': 'Stored Mini telemetry cannot be read.'}
    except (ValueError, TypeError, KeyError, OverflowError):
        return {**unavailable, 'error': 'Stored Mini telemetry is invalid.'}


def handle_post(handler):
    """Bounded HTTP adapter shared by the serving endpoint and local tests."""
    if not enabled():
        handler.send_error(404)
        return
    try:
        if handler.path != ENDPOINT:
            raise TransportError(400, 'unexpected URL')
        for key in ('Content-Length', 'Content-Type', 'X-Paper-Observability-Timestamp', 'X-Paper-Observability-Id', 'X-Paper-Observability-Signature'):
            if len(handler.headers.get_all(key, [])) != 1:
                raise TransportError(400, 'missing/duplicate header')
        raw_length = handler.headers['Content-Length']
        if not re.fullmatch(r'[0-9]{1,6}', raw_length) or handler.headers.get('Transfer-Encoding') or handler.headers.get('Content-Encoding'):
            raise TransportError(400, 'invalid framing')
        length = int(raw_length)
        if length > MAX_BYTES:
            raise TransportError(413, 'body too large')
        if handler.headers['Content-Type'].lower() != 'application/json':
            raise TransportError(415, 'invalid content type')
        old_timeout = handler.connection.gettimeout()
        try:
            deadline = time.monotonic() + 5
            chunks = []
            remaining = length
            while remaining:
                budget = deadline - time.monotonic()
                if budget <= 0:
                    raise TimeoutError()
                handler.connection.settimeout(budget)
                chunk = handler.rfile.read1(min(remaining, 4096))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            body = b''.join(chunks)
        finally:
            handler.connection.settimeout(old_timeout)
        if len(body) != length:
            raise TransportError(400, 'truncated body')
        key = read_key(os.environ['PAPER_OBSERVABILITY_INGEST_KEY_FILE'])
        delivery_id = authenticate(handler.headers, body, key)
        result = ingest(os.environ.get('PAPER_OBSERVABILITY_STATE_FILE', STORE), body, delivery_id)
        response = encoded(result)
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Content-Length', str(len(response)))
        handler.end_headers()
        handler.wfile.write(response)
    except TransportError as error:
        handler.close_connection = True
        handler.send_error(error.code, str(error))
    except TimeoutError:
        handler.close_connection = True
        handler.send_error(408)
    except (OSError, KeyError, ValueError, TypeError, RecursionError):
        handler.close_connection = True
        handler.send_error(503, 'telemetry unavailable')

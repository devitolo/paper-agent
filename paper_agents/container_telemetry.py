"""Sanitize the fixed Mini Docker facts received over publisher stdin."""
from datetime import datetime, timezone
import json
import math
import re

from paper_agents.observability_transport import CONTAINER_SERVICES, validate_container_health

UNITS = {'B': 1, 'kB': 1000, 'KB': 1000, 'MB': 1000**2, 'GB': 1000**3,
         'KiB': 1024, 'MiB': 1024**2, 'GiB': 1024**3, 'TiB': 1024**4}


def memory_bytes(value):
    match = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)', value.strip())
    if not match or match[2] not in UNITS:
        raise ValueError('invalid memory measurement')
    result = float(match[1]) * UNITS[match[2]]
    if not math.isfinite(result) or result > 2**63-1:
        raise ValueError('invalid memory measurement')
    return int(result)


def read_container_health(stream):
    raw = stream.read(16385)
    if len(raw) > 16384:
        raise ValueError('oversize container input')
    lines = raw.splitlines()
    if len(lines) != len(CONTAINER_SERVICES) * 3:
        raise ValueError('invalid container input count')
    items = []
    for index, service in enumerate(CONTAINER_SERVICES):
        inspected, image, stats = [json.loads(line) for line in lines[index*3:index*3+3]]
        if not isinstance(inspected, dict) or not isinstance(image, dict) or not isinstance(stats, dict):
            raise ValueError('invalid container input')
        status = inspected.get('status', 'unavailable')
        digests = image.get('digests') or []
        if not isinstance(digests, list):
            raise ValueError('invalid image digests')
        if image.get('image_id') != inspected.get('image_id'):
            digests = []
        digest = next((item.split('@', 1)[1] for item in digests
                       if isinstance(item, str) and re.fullmatch(r'[^\s@]+@sha256:[0-9a-f]{64}', item)), None)
        cpu = memory = limit = None
        if status == 'running' and stats:
            cpu = float(stats['cpu'].removesuffix('%'))
            used, capacity = stats['memory'].split('/')
            memory, limit = memory_bytes(used), memory_bytes(capacity)
        items.append({'service': service, 'status': status, 'health': inspected.get('health', 'unavailable'),
                      'image_digest': digest, 'image_id': inspected.get('image_id'),
                      'cpu_percent': cpu, 'memory_bytes': memory, 'memory_limit_bytes': limit})
    return validate_container_health({'state': 'observed', 'observed_at': datetime.now(timezone.utc).isoformat(), 'items': items})

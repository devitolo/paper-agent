"""Read-only, sanitized resource status model shared by CLI and Health UI."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any


SCHEMA = "project-paper.resource-status.v1"


def load_resource_status(path: Path | None = None, *, now: datetime | None = None) -> dict[str, Any]:
    target = path or Path(os.environ.get("PAPER_AGENT_RESOURCE_STATUS_FILE", "data/resource-status.json"))
    unknown: dict[str, Any] = {"schema": SCHEMA, "state": "unknown", "observed_at": None, "age_seconds": None, "sample_count": 0, "coverage_seconds": 0}
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {**unknown, "state": "unavailable"}
    if not isinstance(value, dict) or value.get("schema") != SCHEMA or not isinstance(value.get("observed_at"), str):
        return {**unknown, "state": "malformed"}
    current = now or datetime.now(timezone.utc)
    try:
        observed = datetime.fromisoformat(value["observed_at"].replace("Z", "+00:00"))
        age = max(0, int((current - observed).total_seconds()))
    except (TypeError, ValueError):
        return {**unknown, "state": "malformed"}
    state = str(value.get("state", "observed"))
    if age > 45 * 60:
        state = "stale"
    return {**value, "state": state, "age_seconds": age}


def load_aws_resource_status(path: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Bounded read-only projection of the existing local AWS collector format."""
    import math
    current = now or datetime.now(timezone.utc)
    unavailable = {'state': 'unavailable', 'freshness': 'unavailable', 'source': 'AWS',
                   'observed_at': None, 'age_seconds': None}
    def number(value, maximum):
        if value is None:return None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= maximum or not math.isfinite(value):
            raise ValueError('invalid metric')
        return value
    def instant(value):
        if not isinstance(value, str) or len(value) > 40:raise ValueError('invalid time')
        observed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if observed.utcoffset() is None or observed.utcoffset().total_seconds() != 0:raise ValueError('UTC required')
        age = (current-observed).total_seconds()
        if age < -300:raise ValueError('future observation')
        return max(0, int(age))
    try:
        with path.open('rb') as stream:raw=stream.read(32769)
        if len(raw)>32768:raise ValueError('oversize status')
        value=json.loads(raw)
        if (value.get('schema'),value.get('environment'),value.get('scope')) != (SCHEMA,'aws-production','host'):
            raise ValueError('invalid identity')
        age=instant(value['observed_at'])
        if age>2700:
            return {**unavailable,'state':'stale','freshness':'stale','observed_at':value['observed_at'],'age_seconds':age,'error':'No fresh AWS telemetry within 45 minutes.'}
        cpu={key:number(value.get('cpu',{}).get(key),100) for key in ('average_percent','max_observed_percent')}
        if all(cpu[k] is not None for k in cpu) and cpu['average_percent']>cpu['max_observed_percent']:raise ValueError('invalid CPU maximum')
        memory={key:number(value.get('memory',{}).get(key),2**63-1) for key in ('available_average_bytes','available_min_bytes')}
        maximum=number(value.get('memory',{}).get('available_max_bytes'),2**63-1)
        if all(memory[k] is not None for k in memory) and memory['available_min_bytes']>memory['available_average_bytes']:
            raise ValueError('invalid memory minimum')
        if maximum is not None and any(v is not None and v>maximum for v in memory.values()):
            raise ValueError('invalid memory maximum')
        latest=value.get('latest') or {};disk=latest.get('disk');disk_state='unavailable'
        if disk is not None:
            observed=(latest.get('observation_times') or {}).get('disk') or latest.get('observed_at')
            disk_age=instant(observed)
            disk_state='stale' if disk_age>2700 else 'observed'
            if disk_state=='observed':
                disk={key:number(disk.get(key),100 if key=='used_percent' else 2**63-1) for key in ('capacity_bytes','available_bytes','used_percent')}
                if disk['capacity_bytes'] is not None and disk['available_bytes'] is not None and disk['available_bytes']>disk['capacity_bytes']:raise ValueError('invalid capacity')
            else:disk=None
        window=value.get('window') or {}
        if type(window.get('complete')) is not bool:raise ValueError('invalid window')
        def integer(value,maximum):
            if type(value) is not int or not 0<=value<=maximum:raise ValueError('invalid coverage/count')
            return value
        samples=integer(value.get('sample_count'),10000)
        coverage=integer(value.get('coverage_seconds'),900)
        gaps=integer(window.get('sample_gaps'),10000)
        if type(window.get('interval_seconds')) is not int or window['interval_seconds']!=900:
            raise ValueError('invalid window interval')
        instant(window.get('start_at'));instant(window.get('end_at'))
        start=datetime.fromisoformat(window['start_at'].replace('Z','+00:00'))
        end=datetime.fromisoformat(window['end_at'].replace('Z','+00:00'))
        observed=datetime.fromisoformat(value['observed_at'].replace('Z','+00:00'))
        if (end-start).total_seconds()!=900 or not -2700 <= (end-observed).total_seconds() <= 300:
            raise ValueError('invalid window boundaries')
        if window['complete'] and (coverage!=900 or not samples or gaps or cpu['average_percent'] is None):
            raise ValueError('contradictory complete window')
        return {'state':'observed','freshness':'fresh','source':'AWS','observed_at':value['observed_at'],'age_seconds':age,
                'cpu':cpu,'memory':memory,'disk':disk,'disk_state':disk_state,'complete':window['complete']}
    except OSError:
        return {**unavailable,'error':'AWS telemetry export is not readable.'}
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        return {**unavailable,'error':'AWS telemetry export is invalid.'}

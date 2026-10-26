"""Generate a narrow JSON Compose override; never rewrite the base configuration."""
import json
import os
from pathlib import Path
import stat
import tempfile
import sys

EXPORT = '/var/lib/project-paper-observability/aws-production-export'
STATUS = EXPORT + '/status.json'


def configure(base):
    app = base['services']['app']
    mount = {'type': 'bind', 'source': EXPORT, 'target': EXPORT, 'read_only': True}
    for existing in app.get('volumes', []):
        target = existing.get('target') if isinstance(existing, dict) else existing.split(':')[1] if isinstance(existing, str) and ':' in existing else None
        if target == EXPORT and existing != mount:
            raise ValueError('AWS telemetry mount conflict; refusing overwrite')
    environment = app.get('environment') or {}
    if not isinstance(environment, dict):raise ValueError('normalized environment required')
    if environment.get('PAPER_AGENT_AWS_RESOURCE_STATUS_FILE') not in (None, STATUS):
        raise ValueError('AWS telemetry path conflict; refusing overwrite')
    return {'services': {'app': {'volumes': [mount], 'environment': {'PAPER_AGENT_AWS_RESOURCE_STATUS_FILE': STATUS}}}}


def update(path, base):
    path = Path(path)
    result = configure(base)
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):raise ValueError('regular override required')
        if json.loads(path.read_bytes()) != result:raise ValueError('unknown override; refusing overwrite')
        return
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.aws-resource-compose-')
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(result, stream, sort_keys=True)
            stream.flush();os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


if __name__ == '__main__':
    export = Path(EXPORT)
    if export.is_symlink() or not export.is_dir() or (export/'status.json').is_symlink() or not (export/'status.json').is_file():
        raise SystemExit('existing AWS resource export missing')
    update(sys.argv[1], json.load(sys.stdin))

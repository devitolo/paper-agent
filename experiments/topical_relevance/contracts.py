from __future__ import annotations

import hashlib
import json
from pathlib import Path

INTERESTS = (
    'enterprise AI platform/architecture/governance/organizational tradeoffs',
    'AIOps/observability/incident response/diagnosis/RCA/logs-metrics-traces/system relationships',
    'agent reliability/tool-agent coordination/context/failure handling/recovery/human oversight',
)
LABELS = {'relevant', 'partial', 'unrelated', 'insufficient_metadata'}


class ContractError(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise ContractError(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def read_json(path):
    require(Path(path).stat().st_size <= 5 * 1024 * 1024, 'input_size_limit')
    return json.loads(Path(path).read_text())


def external_path(path):
    """Resolve aliases and reject output anywhere inside a Git checkout/worktree."""
    path = Path(path).expanduser().resolve()
    for parent in (path, *path.parents):
        require(not (parent / '.git').exists(), 'private_artifact_inside_git')
    return path


def new_directory(path):
    path = external_path(path)
    require(path.parent.is_dir(), 'output_parent_missing')
    path.mkdir(mode=0o700, exist_ok=False)
    return path


def publish(path, value):
    """Publish a complete immutable record without overwriting even on races."""
    import os
    import tempfile
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.pending-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(canonical(value) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        # Same-directory hard link is an atomic no-clobber publication.
        os.link(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)

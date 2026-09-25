"""Host-cron entry: one owned runtime lease through wrapper and descendant cleanup."""
import os
from pathlib import Path
import subprocess
import sys

from .gemini_guardian import supervise
from .gemini_process import _termination_as_exception, _cleanup_signals
from .gemini_runtime import check_timeout
from .migration_lifecycle import lease, check_descriptor

SCRIPTS = {'arxiv':'nightly_pipeline.sh', 'openalex':'openalex_pipeline.sh',
           'semantic':'semantic_scholar_pipeline.sh',
           'profile':'biweekly_profile_rebuild_compare.sh', 'backup':'backup_db.sh'}


def command(job, root):
    if job not in SCRIPTS:
        raise RuntimeError('Unknown migration cron job')
    result = ['bash', str(root/'scripts'/SCRIPTS[job])]
    if job == 'backup':
        result += [str(root/'data/paper_agent.db'), '/backups']
    return result


def watch(job, reader, descriptor, budget):
    check_timeout(budget)
    check_descriptor(Path(os.environ['PAPER_AGENT_LIFECYCLE_DIR'])/'.runtime.lock', descriptor)
    if job == 'profile':
        from .gemini_runtime import enabled, check_ready
        if not enabled():
            raise RuntimeError('Gemini migration parity requires explicit enablement')
        check_ready()
    root = Path(os.environ.get('PAPER_AGENT_REPO', '/app'))
    # Lease remains owned here across wrapper launch, all writes, and cleanup.
    return supervise(command(job, root), reader, budget, env=os.environ.copy(), cwd=root)


def run(job):
    command(job, Path('/app'))  # Validate before acquiring/launching anything.
    if os.environ.get('PAPER_AGENT_STARTUP_MODE') != 'imported':
        raise RuntimeError('Container cron requires imported runtime configuration')
    budget = float(os.environ.get('PAPER_AGENT_JOB_TIMEOUT', '1800'))
    check_timeout(budget)
    with _termination_as_exception(), lease(Path(os.environ['PAPER_AGENT_LIFECYCLE_DIR'])) as descriptor:
        reader, writer = os.pipe()
        process = None
        try:
            process = subprocess.Popen([sys.executable, '-m', 'paper_agents.migration_job',
                '--watch', job, str(reader), str(descriptor), str(budget)],
                pass_fds=(reader, descriptor), start_new_session=True)
            try:
                return process.wait(timeout=budget+3)
            except subprocess.TimeoutExpired:
                raise RuntimeError('Migration job cleanup pending; runtime lease retained.') from None
        finally:
            # Closing liveness pipe also works when this caller is SIGKILLed.
            os.close(reader); os.close(writer)
            if process is not None:
                with _cleanup_signals():
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        # Never kill the watcher which owns descendants and the lease.
                        raise RuntimeError('Migration job cleanup pending; runtime lease retained.') from None


def main():
    try:
        if len(sys.argv) == 6 and sys.argv[1] == '--watch':
            return watch(sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), float(sys.argv[5]))
        if len(sys.argv) != 2:
            raise RuntimeError('Expected one migration cron job name')
        return run(sys.argv[1])
    except (RuntimeError, ValueError, OSError):
        print('Migration cron job unavailable or failed; inspect local configuration and runtime lease.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

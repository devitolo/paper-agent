"""Independent bounded watchdog, retaining the inherited lifecycle lease."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from .migration_lifecycle import check_descriptor, signal_group


def run(args, descriptor, locks, budget, singleton_descriptor=None):
    check_descriptor(Path(locks)/'.runtime.lock',descriptor)
    if singleton_descriptor is not None:
        check_descriptor(Path(locks)/'.scheduler.lock',singleton_descriptor)
    child_env = os.environ.copy()
    if singleton_descriptor is not None:
        child_env["PAPER_AGENT_SCHEDULER_LEASE_FD"] = str(singleton_descriptor)
    process=None
    old={}
    def stop(signum, frame):raise KeyboardInterrupt
    for sig in (signal.SIGTERM,signal.SIGINT):old[sig]=signal.signal(sig,stop)
    try:
        process=subprocess.Popen(args,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,start_new_session=True,env=child_env,pass_fds=(descriptor,)+((singleton_descriptor,) if singleton_descriptor is not None else ()))
        try:return process.wait(timeout=budget)
        except subprocess.TimeoutExpired:return 124
        except KeyboardInterrupt:return 130
    finally:
        # A scheduler SIGKILL does not kill this watchdog: its own deadline still applies.
        for sig in old:signal.signal(sig,signal.SIG_IGN)
        if process is not None:
            for sig in (signal.SIGTERM,signal.SIGKILL):
                signal_group(process,sig)
                if sig==signal.SIGTERM:time.sleep(5)
            process.wait(timeout=2)
        os.close(descriptor)
        if singleton_descriptor is not None:os.close(singleton_descriptor)
        for sig,handler in old.items():signal.signal(sig,handler)


if __name__=='__main__':
    raise SystemExit(run(sys.argv[5:],int(sys.argv[1]),sys.argv[3],float(sys.argv[4]),int(sys.argv[2]) if int(sys.argv[2]) >= 0 else None))

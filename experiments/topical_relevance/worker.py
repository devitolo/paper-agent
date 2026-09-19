"""Internal offline adapter process; synthetic inputs only."""
import json
import signal
import subprocess
import sys
import time
from .adapters import validate_spec, score
from .contracts import digest


def emit(value):
    print(json.dumps(value, allow_nan=False), flush=True)


def main():
    start = time.perf_counter()
    spec = json.loads(sys.stdin.readline())
    validate_spec(spec)
    learned = None
    if spec['kind'] == 'minilm_onnx':
        from .adapters.minilm import Adapter
        learned = Adapter(spec)
    emit({'learned_runtime': learned.runtime if learned else None, 'status': 'ready', 'adapter_sha256': digest(spec), 'load_seconds': time.perf_counter()-start})
    for line in sys.stdin:
        request = json.loads(line)
        behavior = spec.get('behavior', 'normal')
        if behavior in {'hang', 'resistant_child'}:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            if behavior == 'resistant_child':
                subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(300)'])
            time.sleep(300)
        elif behavior == 'error':
            print('SECRET_FIXTURE_TEXT should never enter reports', file=sys.stderr, flush=True)
            raise RuntimeError('SECRET_FIXTURE_TEXT')
        elif behavior == 'malformed':
            print('not-json SECRET_FIXTURE_TEXT', flush=True)
            continue
        elif behavior == 'flood':
            print('X'*300000, flush=True)
            continue
        before = time.process_time()
        tokens = None
        if learned:
            value, tokens = learned.score(request['payload'], request['interest_index'])
        else:
            value = score(spec, request['payload'], request['interest_index'])
        emit({'seq': request['seq'], 'tokens': tokens, 'score': value, 'payload_sha256': digest(request['payload']),
              'cpu_seconds': time.process_time()-before})


if __name__ == '__main__':
    main()

"""Build-time record of resolved runtime versions; not a compatibility attestation."""
import importlib.metadata
import json
import platform
from pathlib import Path
import subprocess
import sys
from zoneinfo import ZoneInfo


def capture(base_reference, source_bundle_sha256="", source_revision="unknown"):
    tools = {'curl':['curl','--version'], 'bash':['bash','--version'],
             'flock':['flock','--version'], 'date':['date','--version']}
    versions = {name:subprocess.check_output(command,text=True).splitlines()[0]
                for name,command in tools.items()}
    ZoneInfo('America/Los_Angeles')
    from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
    from opentelemetry.sdk.trace import TracerProvider
    return {'format':1, 'base_reference':base_reference, 'python':sys.version,
        'source_bundle_sha256':source_bundle_sha256, 'source_revision':source_revision,
        'machine':platform.machine(), 'tools':versions, 'timezone':'America/Los_Angeles',
        'debian_packages':subprocess.check_output(['dpkg-query','-W','-f=${Package}=${Version}\n'],text=True).splitlines(),
        'python_packages':sorted(f'{d.metadata["Name"]}=={d.version}' for d in importlib.metadata.distributions()),
        'compatibility_qualified':False}


if __name__ == '__main__':
    Path(sys.argv[2]).write_text(json.dumps(capture(sys.argv[1], *sys.argv[3:5]),indent=2)+'\n')

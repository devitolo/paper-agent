"""Build-only inventory; reads installed files, never starts Gemini/auth."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

root = Path('/opt/paper-gemini')
package_root = root/'node_modules/@google/gemini-cli'
package = json.loads((package_root/'package.json').read_text())
entry = package['bin']
entry = entry['gemini'] if isinstance(entry, dict) else entry
entry = (package_root/entry).resolve(strict=True)
if not entry.is_relative_to(package_root) or package['version'] != '0.52.0':
    raise SystemExit('Unexpected pinned Gemini package layout/version')
(root/'cli').symlink_to(entry)
node = Path('/usr/local/bin/node')
version = subprocess.check_output([str(node), '--version'], text=True).strip()
if version != 'v22.23.2':
    raise SystemExit('Unexpected Node version')
inventory = {'node_reference': sys.argv[1], 'node_version': version,
             'cli_version': package['version'], 'cli_entry': str(entry),
             'node_sha256': hashlib.sha256(node.read_bytes()).hexdigest(),
             'cli_sha256': hashlib.sha256(entry.read_bytes()).hexdigest(),
             'lock_sha256': hashlib.sha256((root/'package-lock.json').read_bytes()).hexdigest(),
             'build_qualified': False}
(root/'runtime.json').write_text(json.dumps(inventory, indent=2)+'\n')

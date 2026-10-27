"""Policy checks are local fixtures; never contact Docker or a production host."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from scripts import observability_host_package_install as installer

class HostPolicyTests(unittest.TestCase):
    def test_image_source_architecture_and_size_are_verified_after_pull(self):
        image='ghcr.io/devitolo/paper-agent/observability:observability-mini@sha256:'+'c'*64
        source='a'*40
        cases=[('arm64','linux',source,123),('amd64','windows',source,123),('amd64','linux','b'*40,123),('amd64','linux',source,513*1024**2),('amd64','linux',source,True),('amd64','linux',source,123)]
        for architecture,system,revision,size in cases:
            metadata=json.dumps(dict(architecture=architecture,os=system,revision=revision,size=size)).encode()
            with patch.object(installer,'docker_command',side_effect=[b'/var/lib/docker\n',b'{"architecture":"amd64","os":"linux"}',b'',metadata]) as command,patch.object(installer,'resources') as resources:
                if (architecture,system,revision,size)==('amd64','linux',source,123):
                    installer.prepare_image(Path('/'),image,source,image)
                    self.assertEqual(resources.call_count,2)
                else:
                    with self.assertRaises(ValueError):installer.prepare_image(Path('/'),image,source,image)
                self.assertEqual(command.call_args_list[2].args[0],['pull','--quiet',image])
    def test_headroom_policy_blocks_before_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'proc').mkdir()
            path=root/'proc/meminfo';path.write_text('MemAvailable: 262144 kB\n')
            with patch.object(installer.os,'statvfs',return_value=SimpleNamespace(f_bavail=2048,f_frsize=1024**2)):
                installer.resources(root,'/unused')
                path.write_text('MemAvailable: 262143 kB\n')
                with self.assertRaises(ValueError):installer.resources(root,'/unused')
            with patch.object(installer.os,'statvfs',return_value=SimpleNamespace(f_bavail=2047,f_frsize=1024**2)):
                with self.assertRaises(ValueError):installer.resources(root,'/unused')
    def test_config_parser_never_accepts_shell_or_other_target(self):
        old='ghcr.io/devitolo/paper-agent/observability:observability-mini@sha256:'+'a'*64
        new='ghcr.io/devitolo/paper-agent/observability:observability-mini@sha256:'+'b'*64
        content=('PROJECT_PAPER_OBSERVABILITY_IMAGE='+old+'\nPROJECT_PAPER_RESOURCE_ROOT=/var/lib/project-paper-observability/mini-production\nPROJECT_PAPER_RESOURCE_EXPORT_DIR=/var/lib/project-paper-observability/mini-production-export\nPROJECT_PAPER_RESOURCE_ENVIRONMENT=mini-production\nPROJECT_PAPER_RESOURCE_HOST=mini-production\nPROJECT_PAPER_RESOURCE_ENV_FILE='+installer.ENV_FILE+'\n').encode()
        self.assertEqual(installer.image_config(content,new),content.replace(old.encode(),new.encode()))
        for bad in (content+b'PATH=/tmp\n',content+content.splitlines(keepends=True)[0],content.replace(b'HOST=mini-production',b'HOST=$(touch /tmp/pwned)'),content.replace(b'ENVIRONMENT=mini-production',b'ENVIRONMENT=aws-production')):
            with self.assertRaises(ValueError):installer.image_config(bad,new)

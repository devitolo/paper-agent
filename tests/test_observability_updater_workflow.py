"""Static release-boundary regressions; runtime/crypto evidence is separate."""
from pathlib import Path
import re
import unittest

ROOT=Path(__file__).resolve().parents[1]

class UpdaterWorkflowTests(unittest.TestCase):
    def setUp(self):self.source=(ROOT/'.github/workflows/observability-release.yml').read_text()
    def test_attestation_permissions_subjects_and_source_label(self):
        package=self.source.split('  package:',1)[1].split('  deploy:',1)[0]
        for text in ('id-token: write','attestations: write','--build-arg VCS_REF="$GITHUB_SHA"','host-package/artifact.tar','host-package/manifest.json'):
            self.assertIn(text,package)
        self.assertIn('environment:',self.source.split('  deploy:',1)[1])
    def test_installer_inbox_matches_fixed_identity_and_failure_evidence_survives(self):
        deploy=self.source.split('  deploy:',1)[1]
        self.assertIn('staged="$inbox/$bundle-$GITHUB_SHA"',deploy)
        self.assertIn('mv -- "$upload" "$staged"',deploy)
        self.assertIn('--target mini-production --bundle "$bundle" --source-sha "$GITHUB_SHA"',deploy)
        self.assertIn('previous="$staged.previous-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT"',deploy)
        self.assertNotIn("trap 'rm -rf",deploy)
        self.assertNotIn('rm -rf -- "$staged"',deploy)
    def test_native_validation_modules_exist_and_include_new_boundaries(self):
        names=re.findall(r'\btests\.[a-zA-Z0-9_]+',self.source)
        for name in names:self.assertTrue((ROOT/(name.replace('.','/')+'.py')).is_file(),name)
        for name in ('tests.test_observability_host_package_build','tests.test_observability_bootstrap_build','tests.test_observability_host_package_policy'):
            self.assertIn(name,names)
    def test_aws_stays_on_its_existing_helper_path(self):
        self.assertIn('else\n            sudo -n /usr/local/sbin/project-paper-observability-deploy --image "$image" --target "$target"',self.source)
    def test_no_added_reviewer_or_authorizer_requirement(self):
        for text in ('require_approval_policy', 'APPROVED_REVIEWERS', 'authorize-production', 'required_reviewers'):
            self.assertNotIn(text,self.source)
        self.assertFalse((ROOT/'scripts/observability_authorization_policy.py').exists())

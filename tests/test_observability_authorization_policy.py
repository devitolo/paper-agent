import copy
import unittest
from scripts.observability_authorization_policy import (
    ENVIRONMENT, REPOSITORY, REF, require_approval_policy, require_release_context)


class AuthorizationPolicyTests(unittest.TestCase):
    def require_policy(self, environment, policies):
        require_approval_policy(environment, policies,
                                approved_reviewers=frozenset({('User', 123)}))

    def setUp(self):
        self.environment = {
            'name': ENVIRONMENT, 'can_admins_bypass': False,
            'deployment_branch_policy': {'protected_branches': False,
                                         'custom_branch_policies': True},
            'protection_rules': [{'type': 'required_reviewers',
                                  'prevent_self_review': True,
                                  'reviewers': [{'type': 'User', 'reviewer': {'id': 123}}]}]}
        self.policies = {'total_count': 1, 'branch_policies': [
            {'name': 'observability-mini', 'type': 'tag'}]}

    def test_exact_policy_and_context_pass(self):
        self.require_policy(self.environment, self.policies)
        require_release_context(REPOSITORY, 'push', REF, 'a' * 40)

    def test_current_unprotected_environment_rejected(self):
        self.environment['protection_rules'] = []
        with self.assertRaises(ValueError):
            self.require_policy(self.environment, self.policies)

    def test_missing_bypass_self_review_or_reviewer_protection_rejected(self):
        variants = []
        for value in (True, None):
            env = copy.deepcopy(self.environment); env['can_admins_bypass'] = value
            variants.append(env)
        for field, value in [('prevent_self_review', False), ('reviewers', [])]:
            env = copy.deepcopy(self.environment); env['protection_rules'][0][field] = value
            variants.append(env)
        for env in variants:
            with self.subTest(env=env), self.assertRaises(ValueError):
                self.require_policy(env, self.policies)

    def test_wrong_environment_or_broad_tag_policy_rejected(self):
        for name in ('mini-production', 'observability-aws-production', None):
            env = copy.deepcopy(self.environment); env['name'] = name
            with self.assertRaises(ValueError): self.require_policy(env, self.policies)
        for policies in (None, {}, {'total_count': 2, 'branch_policies': self.policies['branch_policies']},
                         {'total_count': 1, 'branch_policies': [{'name': '*', 'type': 'tag'}]},
                         {'total_count': 1, 'branch_policies': [{'name': 'observability-mini', 'type': 'branch'}]}):
            with self.assertRaises(ValueError): self.require_policy(self.environment, policies)

    def test_unconfigured_identity_and_reviewer_substitution_rejected(self):
        with self.assertRaises(ValueError):
            require_approval_policy(self.environment, self.policies)
        self.require_policy(self.environment, self.policies)
        # A different reviewer configured after preflight cannot authorize signing.
        self.environment['protection_rules'][0]['reviewers'][0]['reviewer']['id'] = 456
        with self.assertRaises(ValueError):
            self.require_policy(self.environment, self.policies)

    def test_policy_weakened_after_preflight_rejected_before_signing(self):
        self.require_policy(self.environment, self.policies)
        self.environment['can_admins_bypass'] = True
        with self.assertRaises(ValueError):
            self.require_policy(self.environment, self.policies)

    def test_wrong_caller_event_ref_or_commit_rejected(self):
        for values in [('other/repo', 'push', REF, 'a'*40),
                       (REPOSITORY, 'workflow_dispatch', REF, 'a'*40),
                       (REPOSITORY, 'push', 'refs/heads/observability-mini', 'a'*40),
                       (REPOSITORY, 'push', REF, 'main')]:
            with self.assertRaises(ValueError): require_release_context(*values)

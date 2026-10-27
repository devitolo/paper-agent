"""Fail-closed checks for the future pinned Mini release authorizer.

This module neither fetches GitHub configuration nor grants authority. The pinned
authorizer must fetch fresh API responses itself before and after approval.
"""
ENVIRONMENT = 'observability-mini-production'
REPOSITORY = 'devitolo/paper-agent'
REF = 'refs/tags/observability-mini'
# Deliberately unconfigured until a human selects the intended identities.
# The immutable authorizer must carry the reviewed set; never caller inputs.
APPROVED_REVIEWERS = frozenset()


def require_approval_policy(environment, branch_policies, *, approved_reviewers=APPROVED_REVIEWERS):
    if not approved_reviewers:
        raise ValueError('reviewed approver identities are not configured')
    if not isinstance(environment, dict) or environment.get('name') != ENVIRONMENT:
        raise ValueError('exact observability production environment required')
    if environment.get('can_admins_bypass') is not False:
        raise ValueError('approval bypass must be disabled and observable')
    rules = environment.get('protection_rules')
    if not isinstance(rules, list):
        raise ValueError('protection rules unavailable')
    reviewers = [rule for rule in rules if isinstance(rule, dict)
                 and rule.get('type') == 'required_reviewers']
    if len(reviewers) != 1 or reviewers[0].get('prevent_self_review') is not True:
        raise ValueError('required reviewers with self-review disabled required')
    entries = reviewers[0].get('reviewers')
    if not isinstance(entries, list) or not entries:
        raise ValueError('at least one configured reviewer required')
    actual_reviewers = []
    for entry in entries:
        if (not isinstance(entry, dict) or entry.get('type') not in ('User', 'Team')
                or not isinstance(entry.get('reviewer'), dict)
                or type(entry['reviewer'].get('id')) is not int
                or entry['reviewer']['id'] <= 0):
            raise ValueError('invalid reviewer identity')
        actual_reviewers.append((entry['type'], entry['reviewer']['id']))
    if (len(set(actual_reviewers)) != len(actual_reviewers)
            or frozenset(actual_reviewers) != frozenset(approved_reviewers)):
        raise ValueError('reviewer substitution is not authorized')
    if environment.get('deployment_branch_policy') != {
            'protected_branches': False, 'custom_branch_policies': True}:
        raise ValueError('explicit release tag policy required')
    if not isinstance(branch_policies, dict):
        raise ValueError('deployment policies unavailable')
    policies = branch_policies.get('branch_policies')
    if (type(branch_policies.get('total_count')) is not int
            or branch_policies['total_count'] != 1
            or not isinstance(policies, list) or len(policies) != 1
            or not isinstance(policies[0], dict)
            or policies[0].get('name') != 'observability-mini'
            or policies[0].get('type') != 'tag'):
        raise ValueError('only the exact observability-mini tag may authorize')


def require_release_context(repository, event, ref, source_sha):
    import re
    if (repository, event, ref) != (REPOSITORY, 'push', REF):
        raise ValueError('exact repository and release tag push required')
    if not isinstance(source_sha, str) or not re.fullmatch('[0-9a-f]{40}', source_sha):
        raise ValueError('immutable source commit required')

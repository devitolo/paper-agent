"""One shared text selection, performed before any adapter is chosen."""
import re
from .contracts import require, digest


def normalize(text):
    return re.sub(r'\s+', ' ', text).strip()


def clip(text, cap):
    if len(text) <= cap:
        return text
    cut = text[:cap]
    return cut.rsplit(' ', 1)[0] if ' ' in cut else cut


def usable_abstract(text, minimum):
    return (len(text) >= minimum
            and len(re.findall(r'[A-Za-z]', text)) >= 20
            and text.lower() not in {'abstract unavailable', 'no abstract available'})


def select(record, policy):
    require(set(policy) == {'title_chars', 'abstract_chars', 'min_abstract_chars'}, 'invalid_text_policy')
    for key, value in policy.items():
        require(type(value) is int and 1 <= value <= 16000, 'invalid_text_limit')
    require(policy['abstract_chars'] >= policy['min_abstract_chars'], 'inconsistent_text_limits')
    title = record.get('title')
    abstract = record.get('abstract')
    kind = record.get('abstract_kind')
    if not isinstance(title, str) or not title.strip() or not isinstance(abstract, str):
        return {'status': 'insufficient_metadata', 'reason': 'missing_title_or_abstract'}
    title, abstract = normalize(title), normalize(abstract)
    if kind != 'synthetic_source_abstract' or not usable_abstract(abstract, policy['min_abstract_chars']):
        return {'status': 'insufficient_metadata', 'reason': 'unusable_or_unverified_abstract'}
    selected_title = clip(title, policy['title_chars'])
    selected_abstract = clip(abstract, policy['abstract_chars'])
    if not usable_abstract(selected_abstract, policy['min_abstract_chars']):
        return {'status': 'insufficient_metadata', 'reason': 'unusable_after_truncation'}
    payload = {'title': selected_title, 'abstract': selected_abstract}
    return {'status': 'ready', 'payload': payload, 'payload_sha256': digest(payload),
            'normalization': 'whitespace-collapse-v1',
            'title_span': [0, len(selected_title)], 'abstract_span': [0, len(selected_abstract)],
            'offset_basis': 'normalized_title_and_abstract',
            'original_normalized_chars': {'title': len(title), 'abstract': len(abstract)},
            'truncated': selected_title != title or selected_abstract != abstract}

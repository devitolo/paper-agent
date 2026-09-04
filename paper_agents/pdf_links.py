"""Helpers for distinguishing paper links from direct PDF links."""

from __future__ import annotations

from urllib.parse import urlparse


def looks_like_direct_pdf_url(source: str | None, url: str | None) -> bool:
    """Return true when a URL is plausible as a directly fetchable PDF."""
    if not url:
        return False
    normalized_source = (source or "").strip().lower()
    normalized_url = url.strip()
    if not normalized_url:
        return False
    lower_url = normalized_url.lower()
    if normalized_source == "arxiv" and "arxiv.org/" in lower_url:
        return True

    parsed = urlparse(normalized_url)
    path = parsed.path.lower()
    query = parsed.query.lower()
    if path.endswith(".pdf") or ".pdf/" in path:
        return True
    if path.endswith("/pdf") or "/pdf/" in path:
        return True
    if lower_url.endswith(".pdf") or ".pdf?" in lower_url or ".pdf#" in lower_url:
        return True
    if "format=pdf" in query or "download=pdf" in query:
        return True
    return False

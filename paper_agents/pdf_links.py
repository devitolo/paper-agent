"""Helpers for distinguishing paper links from direct PDF links."""

from __future__ import annotations

import html
import re
from urllib.parse import urlparse
from urllib.parse import urljoin


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


def semantic_reader_url(source: str | None, source_id: str | None) -> str | None:
    if (source or "").strip().lower() != "semantic_scholar":
        return None
    paper_id = (source_id or "").strip()
    if not paper_id:
        return None
    return f"https://www.semanticscholar.org/reader/{paper_id}"


def candidate_pdf_urls(source: str | None, source_id: str | None, pdf_url: str | None) -> list[str]:
    urls: list[str] = []
    if looks_like_direct_pdf_url(source, pdf_url):
        urls.append(str(pdf_url).strip())
    reader_url = semantic_reader_url(source, source_id)
    if reader_url and reader_url not in urls:
        urls.append(reader_url)
    return urls


def extract_pdf_links_from_html(base_url: str, html_text: str) -> list[str]:
    links: list[str] = []
    for match in re.finditer(r"""href=["']([^"']+)["']""", html_text, re.IGNORECASE):
        url = urljoin(base_url, html.unescape(match.group(1)))
        if looks_like_direct_pdf_url(None, url) and url not in links:
            links.append(url)
    return links

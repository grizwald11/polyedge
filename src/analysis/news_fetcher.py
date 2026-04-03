"""HTTP fetching, HTML parsing, and URL normalization for news research.

Extracted from news_researcher.py — provides article text extraction,
full-article fetching, and URL dedup/normalization helpers.
"""

from __future__ import annotations

import json
import logging
import re
from html.parser import HTMLParser

import httpx

logger = logging.getLogger(__name__)

# Constants used by fetching logic
MAX_ARTICLE_FETCH = 3  # Fetch full text for top N results
MAX_ARTICLE_CHARS = 3000  # Max chars to extract per article
ARTICLE_FETCH_TIMEOUT = 5.0  # Seconds per article fetch

# Tracking params stripped during URL normalization
_TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "gclsrc", "dclid", "msclkid",
    "mc_cid", "mc_eid", "ref", "source",
})


class _ArticleTextExtractor(HTMLParser):
    """Extract visible text from HTML, skipping script/style/nav/header/footer."""

    _SKIP_TAGS = frozenset({"script", "style", "nav", "header", "footer", "noscript", "svg"})

    def __init__(self):
        super().__init__()
        self._pieces: list[str] = []
        self._skip_depth: int = 0
        self._in_paragraph: bool = False

    def handle_starttag(self, tag: str, attrs):
        tag_lower = tag.lower()
        if tag_lower in self._SKIP_TAGS:
            self._skip_depth += 1
        if tag_lower in ("p", "div", "article", "section", "h1", "h2", "h3", "li", "blockquote"):
            self._pieces.append("\n")

    def handle_endtag(self, tag: str):
        if tag.lower() in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag.lower() in ("p", "div", "article", "section", "li", "blockquote"):
            self._pieces.append("\n")

    def handle_data(self, data: str):
        if self._skip_depth == 0:
            self._pieces.append(data)

    def get_text(self) -> str:
        raw = "".join(self._pieces)
        # Collapse runs of whitespace but preserve paragraph breaks
        lines = raw.split("\n")
        cleaned = []
        for line in lines:
            line = re.sub(r"[ \t]+", " ", line).strip()
            if line:
                cleaned.append(line)
        return " ".join(cleaned)


def _extract_text_from_html(html: str) -> str:
    """Extract visible text from HTML using stdlib parser."""
    parser = _ArticleTextExtractor()
    try:
        parser.feed(html)
    except Exception:
        # Fallback: strip tags with regex if parser fails on malformed HTML
        text = re.sub(r"<[^>]+>", " ", html)
        return re.sub(r"\s+", " ", text).strip()
    return parser.get_text()


def _truncate_at_sentence(text: str, max_chars: int) -> str:
    """Truncate text at the last sentence boundary before max_chars."""
    if len(text) <= max_chars:
        return text
    # Find last sentence-ending punctuation before limit
    truncated = text[:max_chars]
    for end_char in (".!?"):
        last_pos = truncated.rfind(end_char)
        if last_pos > max_chars * 0.5:  # Don't truncate too aggressively
            return truncated[: last_pos + 1]
    # No good sentence boundary found — cut at last space
    last_space = truncated.rfind(" ")
    if last_space > max_chars * 0.5:
        return truncated[:last_space] + "..."
    return truncated + "..."


def _normalize_url(url: str) -> str:
    """Normalize a URL for deduplication — strip tracking params, fragments, www prefix.

    Keeps non-tracking query params so articles distinguished only by query
    (e.g., ?article=123 vs ?article=456) are not falsely deduplicated.
    """
    try:
        from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if host.startswith("www."):
            host = host[4:]
        # Strip only known tracking params; keep the rest
        if parsed.query:
            params = parse_qs(parsed.query, keep_blank_values=True)
            filtered = {k: v for k, v in params.items() if k.lower() not in _TRACKING_PARAMS}
            clean_query = urlencode(filtered, doseq=True) if filtered else ""
        else:
            clean_query = ""
        return urlunparse((parsed.scheme, host, parsed.path.rstrip("/"), "", clean_query, ""))
    except Exception as e:
        logger.debug(f"URL normalization failed for {url[:80]}: {e}")
        return url


def _extract_source(url: str) -> str:
    """Extract a readable source name from a URL."""
    try:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
        # Strip www. prefix
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception as e:
        logger.debug(f"Source extraction failed for {url[:80]}: {e}")
        return url


async def fetch_article_text(url: str) -> str:
    """Fetch and extract main text content from an article URL.

    Uses a lightweight approach: fetch HTML, strip tags, extract the
    largest text block. Returns empty string on failure.
    """
    if not url:
        return ""
    try:
        async with httpx.AsyncClient(
            timeout=ARTICLE_FETCH_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; PolyEdge/1.0)"},
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            if not any(ct in content_type for ct in ("text/html", "application/xhtml", "application/json")):
                return ""
            raw_text = resp.text
            content_type_lower = content_type.lower()
    except (httpx.HTTPError, httpx.TimeoutException, OSError) as e:
        logger.debug(f"Article fetch failed for {url}: {e}", exc_info=True)
        return ""
    except Exception as e:
        logger.warning(f"Unexpected error fetching article {url}: {e}", exc_info=True)
        return ""

    # Try JSON-LD articleBody extraction first (AMP / structured data)
    if "application/json" in content_type_lower:
        try:
            data = json.loads(raw_text)
            article_body = data.get("articleBody", "")
            if article_body:
                return _truncate_at_sentence(article_body, MAX_ARTICLE_CHARS)
        except (json.JSONDecodeError, AttributeError):
            pass
        return ""

    html = raw_text

    # Extract text using stdlib HTML parser (robust, not regex)
    text = _extract_text_from_html(html)
    if not text:
        return ""

    # M-12: Reject articles with fewer than 50 words (likely nav/ad fragments)
    if len(text.split()) < 50:
        logger.debug(f"Article too short ({len(text.split())} words): {url[:80]}")
        return ""

    # Extract sentences (>40 chars) for quality content
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.strip()) > 40]
    if not sentences:
        return ""

    # Include first 2 sentences (headline/lede) + body
    first_part = " ".join(sentences[:2])
    start = min(3, len(sentences) // 4)
    middle_part = " ".join(sentences[start:])
    combined = first_part + " " + middle_part
    # Truncate at sentence boundary rather than mid-sentence
    return _truncate_at_sentence(combined, MAX_ARTICLE_CHARS)


async def enrich_with_article_text(results: list) -> list:
    """Fetch full article text for top results and append to snippets.

    Args:
        results: List of NewsResult objects to enrich.

    Returns:
        The same list with top results' snippets replaced by full article text
        when the full text is richer than the original snippet.
    """
    import asyncio

    to_fetch = results[:MAX_ARTICLE_FETCH]
    tasks = [fetch_article_text(r.url) for r in to_fetch]
    texts = await asyncio.gather(*tasks, return_exceptions=True)

    for i, text in enumerate(texts):
        if isinstance(text, str) and text and len(text) > len(to_fetch[i].snippet):
            to_fetch[i].snippet = text

    return results

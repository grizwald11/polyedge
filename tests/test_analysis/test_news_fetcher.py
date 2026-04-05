"""Tests for news_fetcher.py — HTML parsing, sentence truncation, URL normalization, quality filtering."""

from __future__ import annotations

import pytest

from src.analysis.news_fetcher import (
    _extract_text_from_html,
    _normalize_url,
    _truncate_at_sentence,
    _extract_source,
)


class TestExtractTextFromHtml:
    def test_basic_paragraph(self):
        html = "<html><body><p>Hello world.</p></body></html>"
        text = _extract_text_from_html(html)
        assert "Hello world." in text

    def test_strips_script_tags(self):
        html = "<html><body><script>var x = 1;</script><p>Visible text.</p></body></html>"
        text = _extract_text_from_html(html)
        assert "var x" not in text
        assert "Visible text." in text

    def test_strips_style_tags(self):
        html = "<html><body><style>.cls { color: red; }</style><p>Content here.</p></body></html>"
        text = _extract_text_from_html(html)
        assert "color" not in text
        assert "Content here." in text

    def test_strips_nav_header_footer(self):
        html = (
            "<html><body>"
            "<nav>Menu items</nav>"
            "<header>Site header</header>"
            "<p>Main content.</p>"
            "<footer>Footer links</footer>"
            "</body></html>"
        )
        text = _extract_text_from_html(html)
        assert "Menu items" not in text
        assert "Site header" not in text
        assert "Footer links" not in text
        assert "Main content." in text

    def test_empty_html(self):
        assert _extract_text_from_html("") == ""

    def test_nested_skip_tags(self):
        html = "<script><div>Nested content</div></script><p>Real text.</p>"
        text = _extract_text_from_html(html)
        assert "Nested content" not in text
        assert "Real text." in text

    def test_collapses_whitespace(self):
        html = "<p>Too   many     spaces   here.</p>"
        text = _extract_text_from_html(html)
        assert "  " not in text

    def test_malformed_html_fallback(self):
        # Malformed HTML should not crash
        html = "<p>Unclosed paragraph<div>Mixed <b>tags"
        text = _extract_text_from_html(html)
        assert len(text) > 0


class TestTruncateAtSentence:
    def test_short_text_unchanged(self):
        text = "Short text."
        assert _truncate_at_sentence(text, 100) == text

    def test_truncates_at_period(self):
        text = "First sentence. Second sentence. Third sentence here is longer."
        result = _truncate_at_sentence(text, 35)
        assert result.endswith(".")
        assert len(result) <= 35

    def test_truncates_at_exclamation(self):
        text = "Breaking news! The market moved dramatically today."
        result = _truncate_at_sentence(text, 20)
        assert result.endswith("!")

    def test_truncates_at_question_mark(self):
        text = "Will rates rise? Analysts are divided on the matter."
        result = _truncate_at_sentence(text, 20)
        assert result.endswith("?")

    def test_falls_back_to_space_with_ellipsis(self):
        # No sentence boundary in the first half
        text = "a " * 100
        result = _truncate_at_sentence(text, 50)
        assert result.endswith("...")

    def test_exact_length_unchanged(self):
        text = "Exact."
        assert _truncate_at_sentence(text, 6) == text


class TestNormalizeUrl:
    def test_strips_utm_params(self):
        url = "https://example.com/article?utm_source=twitter&utm_medium=social"
        result = _normalize_url(url)
        assert "utm_source" not in result
        assert "utm_medium" not in result

    def test_strips_www_prefix(self):
        url = "https://www.example.com/path"
        result = _normalize_url(url)
        assert "www." not in result
        assert "example.com" in result

    def test_preserves_non_tracking_params(self):
        url = "https://example.com/article?id=123&page=2"
        result = _normalize_url(url)
        assert "id=123" in result
        assert "page=2" in result

    def test_strips_fragment(self):
        url = "https://example.com/article#section1"
        result = _normalize_url(url)
        assert "#section1" not in result

    def test_strips_trailing_slash(self):
        url = "https://example.com/path/"
        result = _normalize_url(url)
        assert result.endswith("/path")

    def test_handles_invalid_url_gracefully(self):
        # Should return the original URL on failure
        result = _normalize_url("")
        assert isinstance(result, str)

    def test_deduplicates_same_article(self):
        url1 = "https://www.example.com/article?utm_source=twitter"
        url2 = "https://example.com/article"
        assert _normalize_url(url1) == _normalize_url(url2)


class TestExtractSource:
    def test_extracts_domain(self):
        assert _extract_source("https://reuters.com/article/123") == "reuters.com"

    def test_strips_www(self):
        assert _extract_source("https://www.bbc.com/news") == "bbc.com"

    def test_handles_empty_url(self):
        result = _extract_source("")
        assert isinstance(result, str)

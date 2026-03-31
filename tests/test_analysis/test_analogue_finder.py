"""Tests for the market analogue finder."""

import pytest

from src.analysis.analogue_finder import AnalogueFinder, MarketAnalogue


@pytest.fixture
def finder_with_corpus(tmp_db):
    """AnalogueFinder with seeded resolved markets."""
    conn = tmp_db._get_conn()
    markets = [
        ("FED-CUT-MAR", "Will the Fed cut rates in March 2026?", "Fed", 0.35, 0.28, 0, 0.12),
        ("FED-CUT-JUN", "Will the Fed cut rates in June 2026?", "Fed", 0.50, 0.42, 1, 0.25),
        ("FED-HIKE-DEC", "Will the Fed raise rates in December 2025?", "Fed", 0.20, 0.15, 0, 0.04),
        ("TRUMP-WIN", "Will Trump win the 2028 presidential election?", "Politics", 0.45, 0.40, 0, 0.20),
        ("HARRIS-NOM", "Will Harris win the Democratic nomination?", "Politics", 0.60, 0.55, 1, 0.16),
        ("AI-BENCH", "Will GPT-5 pass the bar exam?", "Tech/AI", 0.70, 0.65, 1, 0.09),
        ("UKRAINE-CEASE", "Will there be a Ukraine ceasefire by June 2026?", "Geopolitics", 0.30, 0.25, 0, 0.09),
        ("CPI-ABOVE", "Will CPI come in above 3% in April 2026?", "Fed", 0.40, 0.35, 0, 0.16),
    ]
    for ticker, question, cat, pred, mkt, actual, brier in markets:
        conn.execute(
            """INSERT OR IGNORE INTO markets (ticker, platform, question, category, first_seen, last_updated)
               VALUES (?, 'kalshi', ?, ?, '2026-03-01', '2026-03-01')""",
            (ticker, question, cat),
        )
        conn.execute(
            """INSERT INTO calibration_records
               (market_id, market_question, predicted_probability, market_price_at_prediction,
                actual_outcome, predicted_at, resolved_at, brier_score)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (ticker, question, pred, mkt, actual, "2026-03-01", "2026-03-20", brier),
        )
    conn.commit()
    finder = AnalogueFinder(tmp_db)
    return finder


@pytest.fixture
def empty_finder(tmp_db):
    """AnalogueFinder with no resolved markets."""
    return AnalogueFinder(tmp_db)


class TestAnalogueFinder:
    def test_finds_similar_fed_markets(self, finder_with_corpus):
        results = finder_with_corpus.find_analogues(
            "Will the Fed cut rates in September 2026?", category="Fed"
        )
        assert len(results) > 0
        # Fed rate cut questions should be top matches
        assert any("Fed cut rates" in a.question for a in results)

    def test_returns_empty_for_no_corpus(self, empty_finder):
        results = empty_finder.find_analogues("Will X happen?")
        assert results == []

    def test_respects_top_k(self, finder_with_corpus):
        results = finder_with_corpus.find_analogues(
            "Will the Fed change rates?", top_k=2
        )
        assert len(results) <= 2

    def test_respects_min_similarity(self, finder_with_corpus):
        results = finder_with_corpus.find_analogues(
            "Completely unrelated gibberish xyzzy foobar",
            min_similarity=0.90,
        )
        assert len(results) == 0

    def test_same_category_boost(self, finder_with_corpus):
        """Same-category matches should be boosted."""
        results = finder_with_corpus.find_analogues(
            "Will the Fed cut rates?", category="Fed"
        )
        if len(results) >= 2:
            # Fed results should dominate top positions
            fed_results = [a for a in results if a.category == "Fed"]
            assert len(fed_results) >= 1

    def test_analogue_fields_populated(self, finder_with_corpus):
        results = finder_with_corpus.find_analogues(
            "Will the Fed cut interest rates?"
        )
        if results:
            a = results[0]
            assert isinstance(a, MarketAnalogue)
            assert a.market_id != ""
            assert a.question != ""
            assert 0.0 <= a.similarity_score <= 2.0  # Can exceed 1.0 with boost
            assert 0.0 <= a.predicted_probability <= 1.0
            assert isinstance(a.actual_outcome, bool)

    def test_corpus_refresh(self, finder_with_corpus):
        """Corpus should be built on first query."""
        # Force corpus build
        finder_with_corpus.find_analogues("test")
        assert len(finder_with_corpus._corpus) > 0

    def test_deduplicates_by_market_id(self, tmp_db):
        """Multiple calibration records for same market should not duplicate."""
        conn = tmp_db._get_conn()
        for i in range(3):
            conn.execute(
                """INSERT INTO calibration_records
                   (market_id, market_question, predicted_probability, market_price_at_prediction,
                    actual_outcome, predicted_at, resolved_at, brier_score)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                ("SAME-MKT", "Will the same event happen?", 0.50 + i * 0.05, 0.45,
                 1, f"2026-03-0{i+1}", "2026-03-20", 0.10),
            )
        conn.execute(
            """INSERT OR IGNORE INTO markets (ticker, platform, question, category, first_seen, last_updated)
               VALUES ('SAME-MKT', 'kalshi', 'Will the same event happen?', 'Other', '2026-03-01', '2026-03-01')""",
        )
        conn.commit()
        finder = AnalogueFinder(tmp_db)
        finder._build_corpus()
        assert len(finder._corpus) == 1


class TestFormatForPrompt:
    def test_formats_analogues(self, finder_with_corpus):
        analogues = [
            MarketAnalogue(
                market_id="FED-CUT-MAR",
                question="Will the Fed cut rates in March 2026?",
                category="Fed",
                similarity_score=0.82,
                predicted_probability=0.35,
                actual_outcome=False,
                brier_score=0.12,
                market_price=0.28,
            ),
        ]
        text = finder_with_corpus.format_for_prompt(analogues)
        assert "SIMILAR PAST MARKETS" in text
        assert "Fed cut rates" in text
        assert "82%" in text
        assert "NO" in text
        assert "35%" in text

    def test_empty_analogues(self, finder_with_corpus):
        text = finder_with_corpus.format_for_prompt([])
        assert text == ""

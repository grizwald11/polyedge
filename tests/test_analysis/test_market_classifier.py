"""Tests for market classifier."""

from src.analysis.market_classifier import classify_market
from src.core.models import Market, MarketCategory, MarketToken


class TestClassifyMarket:
    def test_uses_existing_category(self):
        m = Market(ticker="T1", question="Test", category=MarketCategory.POLITICS)
        assert classify_market(m) == MarketCategory.POLITICS

    def test_classifies_other_by_keywords(self):
        m = Market(
            ticker="T1",
            question="Will the Fed cut interest rates?",
            category=MarketCategory.OTHER,
            tags=["Economics"],
        )
        assert classify_market(m) == MarketCategory.FED_MACRO

    def test_uses_subtitle(self):
        m = Market(
            ticker="T1",
            question="Will this happen?",
            subtitle="FOMC rate decision federal reserve",
            category=MarketCategory.OTHER,
        )
        assert classify_market(m) == MarketCategory.FED_MACRO

    def test_politics_keywords(self):
        m = Market(
            ticker="T1",
            question="Will the president win the election?",
            category=MarketCategory.OTHER,
        )
        assert classify_market(m) == MarketCategory.POLITICS

    def test_truly_unknown(self):
        m = Market(
            ticker="T1",
            question="Will a random thing happen?",
            category=MarketCategory.OTHER,
        )
        assert classify_market(m) == MarketCategory.OTHER

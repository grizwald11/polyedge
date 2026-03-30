"""Tests for the DataEnricher aggregator."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import Settings
from src.core.models import Market, MarketCategory
from src.data.data_enricher import DataEnricher


def _make_market(
    category: MarketCategory = MarketCategory.FED_MACRO,
    question: str = "Will the Fed cut rates in May 2026?",
    yes_price: float = 0.34,
) -> Market:
    return Market(
        ticker="FED-CUT-MAY",
        question=question,
        description="Resolves YES if the Fed cuts rates.",
        category=category,
        yes_price=yes_price,
        no_price=1.0 - yes_price,
        volume_24h=100000,
        liquidity=50000,
        end_date=datetime(2026, 5, 15, tzinfo=timezone.utc),
    )


class TestCategoryRouting:
    @pytest.mark.asyncio
    async def test_fed_macro_gets_all_sources(self):
        settings = Settings()
        enricher = DataEnricher(settings)

        market = _make_market(category=MarketCategory.FED_MACRO)

        with (
            patch.object(enricher.news_researcher, "get_context", new_callable=AsyncMock, return_value="NEWS") as news_mock,
            patch.object(enricher.fred, "get_macro_summary", new_callable=AsyncMock, return_value="FRED") as fred_mock,
            patch.object(enricher.cleveland_fed, "get_context", new_callable=AsyncMock, return_value="CLEV") as clev_mock,
            patch.object(enricher.fedwatch, "get_context", new_callable=AsyncMock, return_value="FEDW") as fedw_mock,
            patch.object(enricher.metaculus, "get_context", new_callable=AsyncMock, return_value="META") as meta_mock,
            patch.object(enricher.manifold, "get_context", new_callable=AsyncMock, return_value="MANI"),
            patch.object(enricher.polymarket, "get_context", new_callable=AsyncMock, return_value="POLY") as poly_mock,
        ):
            context = await enricher.get_context(market)

        assert "NEWS" in context
        assert "FRED" in context
        assert "CLEV" in context
        assert "FEDW" in context
        assert "META" in context
        assert "POLY" in context

    @pytest.mark.asyncio
    async def test_politics_skips_economic_sources(self):
        settings = Settings()
        enricher = DataEnricher(settings)

        market = _make_market(
            category=MarketCategory.POLITICS,
            question="Will Biden win 2028?",
        )

        with (
            patch.object(enricher.news_researcher, "get_context", new_callable=AsyncMock, return_value="NEWS"),
            patch.object(enricher.fred, "get_macro_summary", new_callable=AsyncMock, return_value="FRED") as fred_mock,
            patch.object(enricher.cleveland_fed, "get_context", new_callable=AsyncMock, return_value="CLEV") as clev_mock,
            patch.object(enricher.fedwatch, "get_context", new_callable=AsyncMock, return_value="FEDW") as fedw_mock,
            patch.object(enricher.metaculus, "get_context", new_callable=AsyncMock, return_value="META"),
            patch.object(enricher.manifold, "get_context", new_callable=AsyncMock, return_value=""),
            patch.object(enricher.polymarket, "get_context", new_callable=AsyncMock, return_value="POLY"),
        ):
            context = await enricher.get_context(market)

        # Should NOT have called economic sources
        fred_mock.assert_not_called()
        clev_mock.assert_not_called()
        fedw_mock.assert_not_called()

        assert "NEWS" in context
        assert "META" in context
        assert "POLY" in context
        assert "FRED" not in context

    @pytest.mark.asyncio
    async def test_earnings_gets_fred_but_not_fed(self):
        settings = Settings()
        enricher = DataEnricher(settings)

        market = _make_market(
            category=MarketCategory.EARNINGS,
            question="Will Apple beat earnings?",
        )

        with (
            patch.object(enricher.news_researcher, "get_context", new_callable=AsyncMock, return_value="NEWS"),
            patch.object(enricher.fred, "get_macro_summary", new_callable=AsyncMock, return_value="FRED") as fred_mock,
            patch.object(enricher.cleveland_fed, "get_context", new_callable=AsyncMock, return_value="CLEV") as clev_mock,
            patch.object(enricher.fedwatch, "get_context", new_callable=AsyncMock, return_value="FEDW") as fedw_mock,
            patch.object(enricher.metaculus, "get_context", new_callable=AsyncMock, return_value="META"),
            patch.object(enricher.manifold, "get_context", new_callable=AsyncMock, return_value=""),
            patch.object(enricher.polymarket, "get_context", new_callable=AsyncMock, return_value="POLY"),
        ):
            context = await enricher.get_context(market)

        fred_mock.assert_called_once()
        clev_mock.assert_not_called()
        fedw_mock.assert_not_called()


class TestGracefulDegradation:
    @pytest.mark.asyncio
    async def test_all_sources_fail(self):
        settings = Settings()
        enricher = DataEnricher(settings)

        market = _make_market()

        with (
            patch.object(enricher.news_researcher, "get_context", new_callable=AsyncMock, return_value=""),
            patch.object(enricher.fred, "get_macro_summary", new_callable=AsyncMock, return_value=""),
            patch.object(enricher.cleveland_fed, "get_context", new_callable=AsyncMock, return_value=""),
            patch.object(enricher.fedwatch, "get_context", new_callable=AsyncMock, return_value=""),
            patch.object(enricher.metaculus, "get_context", new_callable=AsyncMock, return_value=""),
            patch.object(enricher.manifold, "get_context", new_callable=AsyncMock, return_value=""),
            patch.object(enricher.polymarket, "get_context", new_callable=AsyncMock, return_value=""),
        ):
            context = await enricher.get_context(market)

        assert context == "No additional context available."

    @pytest.mark.asyncio
    async def test_exception_in_source_doesnt_block_others(self):
        settings = Settings()
        enricher = DataEnricher(settings)

        market = _make_market()

        with (
            patch.object(enricher.news_researcher, "get_context", new_callable=AsyncMock, side_effect=Exception("boom")),
            patch.object(enricher.fred, "get_macro_summary", new_callable=AsyncMock, return_value="FRED DATA"),
            patch.object(enricher.cleveland_fed, "get_context", new_callable=AsyncMock, return_value=""),
            patch.object(enricher.fedwatch, "get_context", new_callable=AsyncMock, return_value=""),
            patch.object(enricher.metaculus, "get_context", new_callable=AsyncMock, return_value=""),
            patch.object(enricher.manifold, "get_context", new_callable=AsyncMock, return_value=""),
            patch.object(enricher.polymarket, "get_context", new_callable=AsyncMock, return_value=""),
        ):
            context = await enricher.get_context(market)

        assert "FRED DATA" in context


class TestTruncation:
    @pytest.mark.asyncio
    async def test_truncates_when_over_limit(self):
        settings = Settings()
        enricher = DataEnricher(settings)

        market = _make_market()

        # Create sections that exceed 5000 chars total
        long_news = "NEWS " * 1500  # 7500 chars
        long_fred = "FRED " * 500

        with (
            patch.object(enricher.news_researcher, "get_context", new_callable=AsyncMock, return_value=long_news),
            patch.object(enricher.fred, "get_macro_summary", new_callable=AsyncMock, return_value=long_fred),
            patch.object(enricher.cleveland_fed, "get_context", new_callable=AsyncMock, return_value=""),
            patch.object(enricher.fedwatch, "get_context", new_callable=AsyncMock, return_value=""),
            patch.object(enricher.metaculus, "get_context", new_callable=AsyncMock, return_value="META"),
            patch.object(enricher.manifold, "get_context", new_callable=AsyncMock, return_value=""),
            patch.object(enricher.polymarket, "get_context", new_callable=AsyncMock, return_value="POLY"),
        ):
            context = await enricher.get_context(market)

        assert len(context) <= 5000


class TestSectionOrdering:
    @pytest.mark.asyncio
    async def test_sections_in_priority_order(self):
        settings = Settings()
        enricher = DataEnricher(settings)

        market = _make_market(category=MarketCategory.FED_MACRO)

        with (
            patch.object(enricher.news_researcher, "get_context", new_callable=AsyncMock, return_value="NEWS_BLOCK"),
            patch.object(enricher.fred, "get_macro_summary", new_callable=AsyncMock, return_value="FRED_BLOCK"),
            patch.object(enricher.cleveland_fed, "get_context", new_callable=AsyncMock, return_value="CLEV_BLOCK"),
            patch.object(enricher.fedwatch, "get_context", new_callable=AsyncMock, return_value="FEDW_BLOCK"),
            patch.object(enricher.metaculus, "get_context", new_callable=AsyncMock, return_value="META_BLOCK"),
            patch.object(enricher.manifold, "get_context", new_callable=AsyncMock, return_value=""),
            patch.object(enricher.polymarket, "get_context", new_callable=AsyncMock, return_value="POLY_BLOCK"),
        ):
            context = await enricher.get_context(market)

        # News should come first
        news_pos = context.index("NEWS_BLOCK")
        fred_pos = context.index("FRED_BLOCK")
        meta_pos = context.index("META_BLOCK")
        poly_pos = context.index("POLY_BLOCK")

        assert news_pos < fred_pos < meta_pos < poly_pos

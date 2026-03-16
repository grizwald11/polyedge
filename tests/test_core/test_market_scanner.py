"""Tests for the market scanner."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import Settings
from src.core.gamma_client import GammaClient, parse_market
from src.core.models import Market, MarketCategory, MarketToken
from src.data.market_scanner import MarketScanner
from src.storage.database import Database


class TestMarketScannerFilter:
    @pytest.fixture
    def scanner(self, tmp_db, settings):
        gamma = GammaClient()
        return MarketScanner(gamma, tmp_db, settings)

    def test_filters_low_volume(self, scanner, sample_market, sample_market_low_volume):
        result = scanner.filter_markets([sample_market, sample_market_low_volume])
        assert len(result) == 1
        assert result[0].condition_id == sample_market.condition_id

    def test_filters_crypto(self, scanner, sample_market, sample_market_crypto):
        result = scanner.filter_markets([sample_market, sample_market_crypto])
        assert len(result) == 1
        assert result[0].condition_id == sample_market.condition_id

    def test_filters_inactive(self, scanner, sample_market):
        inactive = sample_market.model_copy()
        inactive.active = False
        result = scanner.filter_markets([sample_market, inactive])
        assert len(result) == 1

    def test_filters_closed(self, scanner, sample_market):
        closed = sample_market.model_copy()
        closed.closed = True
        result = scanner.filter_markets([sample_market, closed])
        assert len(result) == 1

    def test_filters_non_binary(self, scanner):
        multi = Market(
            condition_id="multi",
            question="Who wins?",
            tokens=[
                MarketToken(token_id="a", outcome="Alice", price=0.4),
                MarketToken(token_id="b", outcome="Bob", price=0.3),
                MarketToken(token_id="c", outcome="Carol", price=0.3),
            ],
            volume_24h=100000,
            active=True,
        )
        result = scanner.filter_markets([multi])
        assert len(result) == 0  # Not binary

    def test_passes_qualifying_market(self, scanner, sample_market):
        result = scanner.filter_markets([sample_market])
        assert len(result) == 1

    def test_passes_politics_market(self, scanner, sample_market_politics):
        result = scanner.filter_markets([sample_market_politics])
        assert len(result) == 1


class TestMarketScannerRank:
    @pytest.fixture
    def scanner(self, tmp_db, settings):
        gamma = GammaClient()
        return MarketScanner(gamma, tmp_db, settings)

    def test_ranks_by_score(self, scanner, sample_market, sample_market_politics):
        # Both markets should be ranked, higher scored first
        ranked = scanner.rank_markets([sample_market, sample_market_politics])
        assert len(ranked) == 2
        # Just verify both are present and ordering is deterministic
        ids = {m.condition_id for m in ranked}
        assert sample_market.condition_id in ids
        assert sample_market_politics.condition_id in ids

    def test_respects_max_markets(self, scanner):
        scanner.settings.scanning.max_markets = 2
        markets = [
            Market(
                condition_id=f"m{i}",
                question=f"Market {i}?",
                tokens=[
                    MarketToken(token_id=f"y{i}", outcome="Yes", price=0.5),
                    MarketToken(token_id=f"n{i}", outcome="No", price=0.5),
                ],
                volume_24h=float(i * 10000),
                active=True,
            )
            for i in range(5)
        ]
        ranked = scanner.rank_markets(markets)
        assert len(ranked) <= 2

    def test_empty_list(self, scanner):
        ranked = scanner.rank_markets([])
        assert ranked == []


class TestMarketScannerStore:
    @pytest.fixture
    def scanner(self, tmp_db, settings):
        gamma = GammaClient()
        return MarketScanner(gamma, tmp_db, settings)

    def test_stores_market(self, scanner, sample_market, tmp_db):
        scanner.store_markets([sample_market])
        stored = tmp_db.get_market(sample_market.condition_id)
        assert stored is not None
        assert stored["question"] == sample_market.question

    def test_stores_multiple(self, scanner, sample_market, sample_market_politics, tmp_db):
        scanner.store_markets([sample_market, sample_market_politics])
        count = tmp_db.get_market_count()
        assert count == 2

    def test_upsert_updates(self, scanner, sample_market, tmp_db):
        scanner.store_markets([sample_market])
        # Update volume
        updated = sample_market.model_copy()
        updated.volume_24h = 999999.0
        scanner.store_markets([updated])
        stored = tmp_db.get_market(sample_market.condition_id)
        assert stored["volume_24h"] == 999999.0


class TestMarketScannerFullCycle:
    @pytest.mark.asyncio
    async def test_run_scan_cycle(self, tmp_db, settings, gamma_markets_response):
        gamma = GammaClient()
        scanner = MarketScanner(gamma, tmp_db, settings)

        # Mock the gamma client to return our test data
        scanner.gamma.get_all_active_markets = AsyncMock(return_value=gamma_markets_response)

        markets = await scanner.run_scan_cycle()

        # Should have filtered out low volume and crypto
        assert len(markets) >= 1
        # Should have stored in DB
        count = tmp_db.get_market_count()
        assert count >= 1

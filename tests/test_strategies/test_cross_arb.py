"""Tests for cross-market arbitrage strategy."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import Settings
from src.core.models import (
    Direction, Market, MarketCategory, MarketToken, StrategyName,
)
from src.data.market_graph import MarketGraph
from src.strategies.cross_arb import CrossArbStrategy


def _make_market(ticker, yes_price, no_price=None, event_ticker="", question=None):
    if no_price is None:
        no_price = 1.0 - yes_price
    # Default to a mutually-exclusive question when event_ticker is set,
    # so Type C mutual-exclusivity tests work correctly.
    if question is None:
        question = f"Who will win {event_ticker}?" if event_ticker else f"Market {ticker}"
    return Market(
        ticker=ticker,
        question=question,
        event_ticker=event_ticker,
        tokens=[
            MarketToken(token_id=f"{ticker}_yes", outcome="Yes", price=yes_price),
            MarketToken(token_id=f"{ticker}_no", outcome="No", price=no_price),
        ],
        end_date=datetime.now(timezone.utc) + timedelta(days=45),
        volume_24h=100000,
    )


@pytest.fixture
def mock_graph():
    graph = MagicMock(spec=MarketGraph)
    graph.find_subset_superset_pairs = MagicMock(return_value=[])
    return graph


@pytest.fixture
def mock_forecaster():
    forecaster = MagicMock()
    forecaster.assess_market_with_prompt = AsyncMock(return_value=None)
    return forecaster


class TestTypeAIntraMarket:
    @pytest.mark.asyncio
    async def test_detects_underpriced_market(self, mock_graph, mock_forecaster, tmp_db):
        settings = Settings()
        strategy = CrossArbStrategy(mock_graph, mock_forecaster, settings, tmp_db)

        # YES=0.45, NO=0.50 → sum=0.95, edge=0.05
        market = _make_market("MKT-A", 0.45, 0.50)
        signals = await strategy.scan_for_opportunities([market])

        assert len(signals) == 1
        assert signals[0].strategy == StrategyName.CROSS_ARB
        assert signals[0].edge == pytest.approx(0.05)

    @pytest.mark.asyncio
    async def test_no_arb_when_prices_sum_to_one(self, mock_graph, mock_forecaster, tmp_db):
        settings = Settings()
        strategy = CrossArbStrategy(mock_graph, mock_forecaster, settings, tmp_db)

        market = _make_market("MKT-A", 0.50, 0.50)
        signals = await strategy.scan_for_opportunities([market])

        type_a = [s for s in signals if "Intra-market" in s.reasoning]
        assert len(type_a) == 0


class TestTypeAKellyConsistency:
    """Regression: Type A signals must satisfy Kelly's market_price = probability - edge."""

    @pytest.mark.asyncio
    async def test_probability_estimate_consistent_with_kelly(self, mock_graph, mock_forecaster, tmp_db):
        settings = Settings()
        strategy = CrossArbStrategy(mock_graph, mock_forecaster, settings, tmp_db)

        # YES=0.45, NO=0.50 → sum=0.95, edge=0.05
        market = _make_market("MKT-K", 0.45, 0.50)
        signals = await strategy.scan_for_opportunities([market])

        type_a = [s for s in signals if "Intra-market" in s.reasoning]
        assert len(type_a) == 1
        sig = type_a[0]

        # Kelly derives: market_price = probability_estimate - edge
        kelly_market_price = sig.probability_estimate - sig.edge
        assert abs(kelly_market_price - sig.market_price) < 0.005, (
            f"Kelly mismatch: {sig.probability_estimate} - {sig.edge} = "
            f"{kelly_market_price} != {sig.market_price}"
        )


class TestTypeCMutualExclusivity:
    @pytest.mark.asyncio
    async def test_detects_underpriced_event(self, mock_graph, mock_forecaster, tmp_db):
        settings = Settings()
        strategy = CrossArbStrategy(mock_graph, mock_forecaster, settings, tmp_db)

        # 3 candidates, YES prices sum to 0.90 < 1.00
        markets = [
            _make_market("CAND-A", 0.30, 0.70, event_ticker="ELECTION"),
            _make_market("CAND-B", 0.25, 0.75, event_ticker="ELECTION"),
            _make_market("CAND-C", 0.35, 0.65, event_ticker="ELECTION"),
        ]

        signals = await strategy.scan_for_opportunities(markets)

        type_c = [s for s in signals if "Mutual exclusivity" in s.reasoning]
        assert len(type_c) == 1
        assert type_c[0].direction == Direction.BUY_YES

    @pytest.mark.asyncio
    async def test_detects_overpriced_event(self, mock_graph, mock_forecaster, tmp_db):
        settings = Settings()
        strategy = CrossArbStrategy(mock_graph, mock_forecaster, settings, tmp_db)

        # 3 candidates, YES prices sum to 1.10 > 1.00
        markets = [
            _make_market("CAND-A", 0.40, 0.60, event_ticker="ELECTION"),
            _make_market("CAND-B", 0.35, 0.65, event_ticker="ELECTION"),
            _make_market("CAND-C", 0.35, 0.65, event_ticker="ELECTION"),
        ]

        signals = await strategy.scan_for_opportunities(markets)

        type_c = [s for s in signals if "Mutual exclusivity" in s.reasoning]
        assert len(type_c) == 1
        assert type_c[0].direction == Direction.BUY_NO

    @pytest.mark.asyncio
    async def test_no_arb_balanced_event(self, mock_graph, mock_forecaster, tmp_db):
        settings = Settings()
        strategy = CrossArbStrategy(mock_graph, mock_forecaster, settings, tmp_db)

        markets = [
            _make_market("CAND-A", 0.50, 0.50, event_ticker="ELECTION"),
            _make_market("CAND-B", 0.50, 0.50, event_ticker="ELECTION"),
        ]

        signals = await strategy.scan_for_opportunities(markets)

        type_c = [s for s in signals if "Mutual exclusivity" in s.reasoning]
        assert len(type_c) == 0


class TestTypeCEdgeScaling:
    """Tests for Type C single-outcome edge scaling fix."""

    @pytest.mark.asyncio
    async def test_edge_scaled_proportionally_underpriced(self, mock_graph, mock_forecaster, tmp_db):
        """Type C underpriced: single_edge < basket_edge for the cheapest outcome."""
        settings = Settings()
        strategy = CrossArbStrategy(mock_graph, mock_forecaster, settings, tmp_db)

        # YES prices: 0.20 + 0.30 + 0.40 = 0.90 → basket_edge = 0.10
        markets = [
            _make_market("CAND-A", 0.20, 0.80, event_ticker="EV1"),
            _make_market("CAND-B", 0.30, 0.70, event_ticker="EV1"),
            _make_market("CAND-C", 0.40, 0.60, event_ticker="EV1"),
        ]

        signals = await strategy.scan_for_opportunities(markets)
        type_c = [s for s in signals if "Mutual exclusivity" in s.reasoning]
        assert len(type_c) == 1

        # Cheapest = CAND-A (0.20). single_edge = 0.10 * (0.20 / 0.90) ≈ 0.022
        basket_edge = 0.10
        expected_single = basket_edge * (0.20 / 0.90)
        assert type_c[0].edge == pytest.approx(expected_single, abs=0.001)
        assert type_c[0].edge < basket_edge  # Must be less than basket edge

    @pytest.mark.asyncio
    async def test_edge_scaled_proportionally_overpriced(self, mock_graph, mock_forecaster, tmp_db):
        """Type C overpriced: single_edge < basket_edge for the most expensive outcome."""
        settings = Settings()
        strategy = CrossArbStrategy(mock_graph, mock_forecaster, settings, tmp_db)

        # YES prices: 0.40 + 0.35 + 0.40 = 1.15 → basket_edge = 0.15
        markets = [
            _make_market("CAND-A", 0.40, 0.60, event_ticker="EV2"),
            _make_market("CAND-B", 0.35, 0.65, event_ticker="EV2"),
            _make_market("CAND-C", 0.40, 0.60, event_ticker="EV2"),
        ]

        signals = await strategy.scan_for_opportunities(markets)
        type_c = [s for s in signals if "Mutual exclusivity" in s.reasoning]
        assert len(type_c) == 1

        basket_edge = 0.15
        # Most expensive = CAND-A or CAND-C (0.40). single_edge = 0.15 * (0.40/1.15)
        expected_single = basket_edge * (0.40 / 1.15)
        assert type_c[0].edge == pytest.approx(expected_single, abs=0.001)
        assert type_c[0].edge < basket_edge

    @pytest.mark.asyncio
    async def test_single_market_in_event_no_signal(self, mock_graph, mock_forecaster, tmp_db):
        """An event with only 1 market should not generate Type C signals."""
        settings = Settings()
        strategy = CrossArbStrategy(mock_graph, mock_forecaster, settings, tmp_db)

        markets = [_make_market("CAND-A", 0.30, 0.70, event_ticker="SOLO")]
        signals = await strategy.scan_for_opportunities(markets)
        type_c = [s for s in signals if "Mutual exclusivity" in s.reasoning]
        assert len(type_c) == 0

    @pytest.mark.asyncio
    async def test_zero_yes_prices_no_crash(self, mock_graph, mock_forecaster, tmp_db):
        """Markets with yes_price=0 should not cause division by zero."""
        settings = Settings()
        strategy = CrossArbStrategy(mock_graph, mock_forecaster, settings, tmp_db)

        markets = [
            _make_market("CAND-A", 0.0, 1.0, event_ticker="ZERO"),
            _make_market("CAND-B", 0.0, 1.0, event_ticker="ZERO"),
        ]
        # Should not crash — may or may not produce signals
        signals = await strategy.scan_for_opportunities(markets)
        # No crash is the assertion

    @pytest.mark.asyncio
    async def test_empty_markets_list(self, mock_graph, mock_forecaster, tmp_db):
        """Empty markets list should produce no signals."""
        settings = Settings()
        strategy = CrossArbStrategy(mock_graph, mock_forecaster, settings, tmp_db)
        signals = await strategy.scan_for_opportunities([])
        assert signals == []


class TestTypeCProbabilityEstimate:
    """Regression: Type C BUY_NO probability_estimate must include edge."""

    @pytest.mark.asyncio
    async def test_buy_no_probability_includes_edge(self, mock_graph, mock_forecaster, tmp_db):
        """For overpriced events (BUY_NO), probability_estimate should be
        no_price + single_edge, not just no_price. Otherwise edge = prob - market = 0."""
        settings = Settings()
        strategy = CrossArbStrategy(mock_graph, mock_forecaster, settings, tmp_db)

        # 3 candidates, YES sum = 1.15 > 1.00, basket_edge = 0.15
        markets = [
            _make_market("CAND-A", 0.40, 0.60, event_ticker="OVR"),
            _make_market("CAND-B", 0.35, 0.65, event_ticker="OVR"),
            _make_market("CAND-C", 0.40, 0.60, event_ticker="OVR"),
        ]

        signals = await strategy.scan_for_opportunities(markets)
        type_c = [s for s in signals if "Mutual exclusivity" in s.reasoning]
        assert len(type_c) == 1
        sig = type_c[0]

        # probability_estimate - market_price should equal edge (within float tolerance)
        assert sig.probability_estimate - sig.market_price == pytest.approx(sig.edge, abs=0.001)
        # probability_estimate should be > market_price (not equal)
        assert sig.probability_estimate > sig.market_price

    @pytest.mark.asyncio
    async def test_buy_no_kelly_derives_correct_market_price(self, mock_graph, mock_forecaster, tmp_db):
        """Kelly sizer derives market_price = probability - edge. For BUY_NO
        Type C, this should equal the actual NO price."""
        settings = Settings()
        strategy = CrossArbStrategy(mock_graph, mock_forecaster, settings, tmp_db)

        markets = [
            _make_market("X1", 0.45, 0.55, event_ticker="KLY"),
            _make_market("X2", 0.35, 0.65, event_ticker="KLY"),
            _make_market("X3", 0.30, 0.70, event_ticker="KLY"),
        ]  # sum = 1.10, basket_edge = 0.10

        signals = await strategy.scan_for_opportunities(markets)
        type_c = [s for s in signals if "Mutual exclusivity" in s.reasoning]
        assert len(type_c) == 1
        sig = type_c[0]

        kelly_market_price = sig.probability_estimate - sig.edge
        assert abs(kelly_market_price - sig.market_price) < 0.005


class TestCacheExpiry:
    """Regression: cache expiry must return None on parse errors."""

    def test_invalid_timestamp_returns_none(self, mock_graph, mock_forecaster, tmp_db):
        """If validated_at is unparseable, return None (don't fall through to stale data)."""
        import json
        settings = Settings()
        strategy = CrossArbStrategy(mock_graph, mock_forecaster, settings, tmp_db)

        # Insert a cache entry with invalid timestamp
        conn = tmp_db._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO arb_relationships "
            "(market_a, market_b, relationship_data, validated_at) VALUES (?, ?, ?, ?)",
            ("MKT-A", "MKT-B", json.dumps({"arbitrage_exists": True}), "not-a-date"),
        )
        conn.commit()

        result = strategy._get_cached_relationship("MKT-A", "MKT-B")
        assert result is None  # Should return None, not stale data


class TestTypeBSubset:
    @pytest.mark.asyncio
    async def test_claude_validation_used(self, mock_forecaster, tmp_db):
        graph = MagicMock(spec=MarketGraph)
        graph.find_subset_superset_pairs = MagicMock(return_value=[
            ("MKT-A", "MKT-B", 0.85),
        ])

        # Mock Claude returning a subset relationship
        from src.core.models import ForecastResult
        mock_forecaster.assess_market_with_prompt = AsyncMock(return_value=ForecastResult(
            probability=0.5,
            raw_response='{"relationship": "subset_ab", "confidence": 0.8, "arbitrage_exists": true}',
        ))

        settings = Settings()
        strategy = CrossArbStrategy(graph, mock_forecaster, settings, tmp_db)

        # A subset of B, but A priced higher → arb
        markets = [
            _make_market("MKT-A", 0.60),
            _make_market("MKT-B", 0.50),
        ]

        signals = await strategy.scan_for_opportunities(markets)

        # Should call Claude for validation
        mock_forecaster.assess_market_with_prompt.assert_called()


class TestTypeCAdditionalEdgeCases:
    """Additional Type C edge cases beyond the basic suite."""

    def _make_strategy(self, mock_graph, mock_forecaster, tmp_db):
        return CrossArbStrategy(mock_graph, mock_forecaster, Settings(), tmp_db)

    def test_all_prices_zero(self, mock_graph, mock_forecaster, tmp_db):
        """All markets in event have 0 YES price — no crash."""
        strategy = self._make_strategy(mock_graph, mock_forecaster, tmp_db)
        markets = [
            _make_market("Z1", yes_price=0.00, event_ticker="ZERO-EVENT"),
            _make_market("Z2", yes_price=0.00, event_ticker="ZERO-EVENT"),
            _make_market("Z3", yes_price=0.00, event_ticker="ZERO-EVENT"),
        ]
        signals = strategy._check_mutual_exclusivity(markets, "ZERO-EVENT")
        # Sum = 0 < 1 − min_edge → underpriced, edge calc shouldn't crash
        for s in signals:
            assert s.edge >= 0

    def test_large_event_basket(self, mock_graph, mock_forecaster, tmp_db):
        """Event with many outcomes (10+) — should still work correctly."""
        strategy = self._make_strategy(mock_graph, mock_forecaster, tmp_db)
        markets = []
        for i in range(10):
            # Use 0.07 (not 0.08) so sum=0.70, basket_edge=0.30,
            # single_edge=0.03 — clearly above min_edge=0.02 even with float rounding.
            m = _make_market(f"BIG-{i}", yes_price=0.07, event_ticker="BIG-EVENT")
            markets.append(m)
        # Sum = 10 * 0.07 = 0.70 < 1.0 − 0.02 = 0.98 → underpriced
        signals = strategy._check_mutual_exclusivity(markets, "BIG-EVENT")
        assert len(signals) == 1
        assert signals[0].direction == Direction.BUY_YES
        # Edge should be scaled down for single outcome
        assert signals[0].edge < 0.30  # Not full basket edge of 0.30

    def test_duplicate_markets_no_double_count(self, mock_graph, mock_forecaster, tmp_db):
        """Duplicate tickers shouldn't cause issues."""
        strategy = self._make_strategy(mock_graph, mock_forecaster, tmp_db)
        m1 = _make_market("DUP-A", yes_price=0.40, event_ticker="DUP-EVENT")
        m2 = _make_market("DUP-B", yes_price=0.40, event_ticker="DUP-EVENT")
        signals = strategy._check_mutual_exclusivity([m1, m2], "DUP-EVENT")
        # Sum = 0.80 < 0.98 → underpriced
        assert len(signals) == 1

    def test_overpriced_event_selects_most_expensive(self, mock_graph, mock_forecaster, tmp_db):
        """Overpriced event should sell the most expensive outcome."""
        strategy = self._make_strategy(mock_graph, mock_forecaster, tmp_db)
        markets = [
            _make_market("OVER-A", yes_price=0.60, event_ticker="OVER-EVENT"),
            _make_market("OVER-B", yes_price=0.30, event_ticker="OVER-EVENT"),
            _make_market("OVER-C", yes_price=0.15, event_ticker="OVER-EVENT"),
        ]
        # Sum = 1.05 > 1.02
        signals = strategy._check_mutual_exclusivity(markets, "OVER-EVENT")
        assert len(signals) == 1
        assert signals[0].market_id == "OVER-A"
        assert signals[0].direction == Direction.BUY_NO

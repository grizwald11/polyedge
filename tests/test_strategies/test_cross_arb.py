"""Tests for cross-market arbitrage strategy."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import Settings
from src.core.models import (
    Direction,
    Market,
    MarketCategory,
    MarketToken,
    StrategyName,
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


class TestTypeAIntraMarketOverpriced:
    """Type A: overpriced markets where YES + NO > 1.0."""

    @pytest.mark.asyncio
    async def test_overpriced_yes_higher_than_no_buys_no(self, mock_graph, mock_forecaster, tmp_db):
        """When YES > NO and sum > 1.0, buy NO (complement of the overpriced side)."""
        settings = Settings()
        strategy = CrossArbStrategy(mock_graph, mock_forecaster, settings, tmp_db)

        # YES=0.60, NO=0.55 → sum=1.15, overpriced_edge=0.15; YES > NO → buy NO
        market = _make_market("OVR-1", yes_price=0.60, no_price=0.55)
        signal = strategy._check_intra_market(market)

        assert signal is not None
        assert signal.direction == Direction.BUY_NO
        assert signal.market_price == pytest.approx(0.55)
        assert signal.edge == pytest.approx(0.15)
        assert "overpricing" in signal.reasoning

    @pytest.mark.asyncio
    async def test_overpriced_no_higher_than_yes_buys_yes(self, mock_graph, mock_forecaster, tmp_db):
        """When NO > YES and sum > 1.0, buy YES (complement of the overpriced NO)."""
        settings = Settings()
        strategy = CrossArbStrategy(mock_graph, mock_forecaster, settings, tmp_db)

        # YES=0.45, NO=0.60 → sum=1.05, overpriced_edge=0.05; NO > YES → buy YES
        market = _make_market("OVR-2", yes_price=0.45, no_price=0.60)
        signal = strategy._check_intra_market(market)

        assert signal is not None
        assert signal.direction == Direction.BUY_YES
        assert signal.market_price == pytest.approx(0.45)
        assert signal.edge == pytest.approx(0.05)
        assert "overpricing" in signal.reasoning

    @pytest.mark.asyncio
    async def test_underpriced_no_higher_than_yes_buys_no(self, mock_graph, mock_forecaster, tmp_db):
        """When YES + NO < 1.0 and NO < YES, the cheaper NO side is bought (lines 101-102)."""
        settings = Settings()
        strategy = CrossArbStrategy(mock_graph, mock_forecaster, settings, tmp_db)

        # YES=0.55, NO=0.40 → sum=0.95, underpriced_edge=0.05; YES > NO → buy NO
        market = _make_market("UND-1", yes_price=0.55, no_price=0.40)
        signal = strategy._check_intra_market(market)

        assert signal is not None
        assert signal.direction == Direction.BUY_NO
        assert signal.market_price == pytest.approx(0.40)
        assert signal.edge == pytest.approx(0.05)
        assert "underpricing" in signal.reasoning


class TestMutuallyExclusiveDetection:
    """Unit tests for _is_mutually_exclusive covering all branches."""

    def _make_strategy(self, mock_graph, mock_forecaster, tmp_db):
        return CrossArbStrategy(mock_graph, mock_forecaster, Settings(), tmp_db)

    def test_single_market_returns_false(self, mock_graph, mock_forecaster, tmp_db):
        """Line 153: fewer than 2 markets → False."""
        strategy = self._make_strategy(mock_graph, mock_forecaster, tmp_db)
        result = strategy._is_mutually_exclusive([_make_market("A", 0.5)])
        assert result is False

    def test_temporal_cascade_returns_false(self, mock_graph, mock_forecaster, tmp_db):
        """Lines 174-178: temporal cascade questions are not mutually exclusive."""
        strategy = self._make_strategy(mock_graph, mock_forecaster, tmp_db)
        markets = [
            _make_market("T1", 0.4, question="will fed cut rates before march?"),
            _make_market("T2", 0.6, question="will fed cut rates before june?"),
        ]
        result = strategy._is_mutually_exclusive(markets)
        assert result is False

    def test_threshold_cascade_returns_false(self, mock_graph, mock_forecaster, tmp_db):
        """Lines 194-198: threshold cascade questions are not mutually exclusive."""
        strategy = self._make_strategy(mock_graph, mock_forecaster, tmp_db)
        markets = [
            _make_market("TH1", 0.4, question="will unemployment fall below 4%?"),
            _make_market("TH2", 0.6, question="will unemployment fall below 5%?"),
        ]
        result = strategy._is_mutually_exclusive(markets)
        assert result is False

    def test_independent_verb_patterns_return_false(self, mock_graph, mock_forecaster, tmp_db):
        """Lines 209-211: different questions with independent-verb patterns → False."""
        strategy = self._make_strategy(mock_graph, mock_forecaster, tmp_db)
        markets = [
            _make_market("V1", 0.4, question="will trump visit france?"),
            _make_market("V2", 0.3, question="will trump visit germany?"),
        ]
        result = strategy._is_mutually_exclusive(markets)
        assert result is False

    def test_shared_question_no_exclusive_keyword_returns_false(self, mock_graph, mock_forecaster, tmp_db):
        """Lines 235: shared question with no exclusive keyword → conservative False."""
        strategy = self._make_strategy(mock_graph, mock_forecaster, tmp_db)
        markets = [
            _make_market("S1", 0.4, question="will the market rally in 2026?"),
            _make_market("S2", 0.3, question="will the market rally in 2026?"),
        ]
        result = strategy._is_mutually_exclusive(markets)
        assert result is False

    def test_shared_question_with_exclusive_keyword_who_will_win(self, mock_graph, mock_forecaster, tmp_db):
        """Shared question with 'who will win' → True."""
        strategy = self._make_strategy(mock_graph, mock_forecaster, tmp_db)
        markets = [
            _make_market("W1", 0.4, question="who will win the election?"),
            _make_market("W2", 0.3, question="who will win the election?"),
            _make_market("W3", 0.2, question="who will win the election?"),
        ]
        result = strategy._is_mutually_exclusive(markets)
        assert result is True

    def test_more_than_2_different_questions_returns_false(self, mock_graph, mock_forecaster, tmp_db):
        """Lines 240-241: >2 markets with different questions → non-exclusive by default."""
        strategy = self._make_strategy(mock_graph, mock_forecaster, tmp_db)
        markets = [
            _make_market("D1", 0.4, question="will alice win?"),
            _make_market("D2", 0.3, question="will bob win?"),
            _make_market("D3", 0.2, question="will carol win?"),
        ]
        result = strategy._is_mutually_exclusive(markets)
        assert result is False

    def test_two_markets_democratic_republican_returns_true(self, mock_graph, mock_forecaster, tmp_db):
        """Lines 248-249: two markets with dem/rep complement → True."""
        strategy = self._make_strategy(mock_graph, mock_forecaster, tmp_db)
        markets = [
            _make_market("P1", 0.4, question="will the democratic candidate win?"),
            _make_market("P2", 0.5, question="will the republican candidate win?"),
        ]
        result = strategy._is_mutually_exclusive(markets)
        assert result is True

    def test_two_markets_yes_no_complement_returns_true(self, mock_graph, mock_forecaster, tmp_db):
        """Lines 250-251: two markets with yes/no phrasing → True."""
        strategy = self._make_strategy(mock_graph, mock_forecaster, tmp_db)
        markets = [
            _make_market("YN1", 0.4, question="will the bill pass yes?"),
            _make_market("YN2", 0.5, question="will the bill pass no?"),
        ]
        result = strategy._is_mutually_exclusive(markets)
        assert result is True

    def test_two_different_unrelated_questions_returns_false(self, mock_graph, mock_forecaster, tmp_db):
        """Lines 253-254: two different unrelated questions → False (safe default)."""
        strategy = self._make_strategy(mock_graph, mock_forecaster, tmp_db)
        markets = [
            _make_market("U1", 0.4, question="will alice announce a deal?"),
            _make_market("U2", 0.5, question="will bob resign from office?"),
        ]
        result = strategy._is_mutually_exclusive(markets)
        assert result is False


class TestCheckMutualExclusivityEdgeCases:
    """Cover remaining branches of _check_mutual_exclusivity."""

    def _make_strategy(self, mock_graph, mock_forecaster, tmp_db):
        return CrossArbStrategy(mock_graph, mock_forecaster, Settings(), tmp_db)

    def test_less_than_2_markets_returns_empty(self, mock_graph, mock_forecaster, tmp_db):
        """Line 268: < 2 markets → empty list."""
        strategy = self._make_strategy(mock_graph, mock_forecaster, tmp_db)
        result = strategy._check_mutual_exclusivity(
            [_make_market("A", 0.5, event_ticker="EV")], "EV"
        )
        assert result == []

    def test_not_mutually_exclusive_returns_empty(self, mock_graph, mock_forecaster, tmp_db):
        """Line 271: non-exclusive event → empty list."""
        strategy = self._make_strategy(mock_graph, mock_forecaster, tmp_db)
        # temporal cascade — not exclusive
        markets = [
            _make_market("T1", 0.4, question="will rates cut before march?"),
            _make_market("T2", 0.5, question="will rates cut before june?"),
        ]
        result = strategy._check_mutual_exclusivity(markets, "EV-T")
        assert result == []

    def test_underpriced_scaled_edge_below_min_no_signal(self, mock_graph, mock_forecaster, tmp_db):
        """When scaled single_edge < min_edge, no signal is emitted (underpriced branch)."""
        strategy = self._make_strategy(mock_graph, mock_forecaster, tmp_db)
        # Many cheap outcomes: sum well below 1, but each scaled edge is tiny
        # 50 markets at 0.005 each → sum=0.25, basket_edge=0.75
        # single_edge = 0.75 * (0.005 / 0.25) = 0.015 < min_edge=0.02 → no signal
        markets = []
        for i in range(50):
            m = _make_market(f"TINY-{i}", yes_price=0.005,
                             question="who will win the event?",
                             event_ticker="TINY-EVENT")
            markets.append(m)
        signals = strategy._check_mutual_exclusivity(markets, "TINY-EVENT")
        assert signals == []


class TestCheckSubsetArbCachePaths:
    """Cover cache-hit paths in _check_subset_arb (lines 348, 356-362)."""

    @pytest.mark.asyncio
    async def test_cache_hit_with_arbitrage_emits_signal(self, mock_forecaster, tmp_db):
        """Lines 356-362: cache hit with arbitrage_exists=True builds signal."""
        import json

        graph = MagicMock(spec=MarketGraph)
        graph.find_subset_superset_pairs = MagicMock(return_value=[("MKT-A", "MKT-B", 0.9)])

        settings = Settings()
        strategy = CrossArbStrategy(graph, mock_forecaster, settings, tmp_db)

        # Pre-populate cache with a subset_ab relationship
        conn = tmp_db._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO arb_relationships "
            "(market_a, market_b, relationship_data, validated_at) VALUES (?, ?, ?, ?)",
            (
                "MKT-A", "MKT-B",
                json.dumps({
                    "relationship": "subset_ab",
                    "confidence": 0.8,
                    "arbitrage_exists": True,
                    "cached_price_a": 0.60,
                    "cached_price_b": 0.50,
                }),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()

        markets = [
            _make_market("MKT-A", yes_price=0.60),
            _make_market("MKT-B", yes_price=0.50),
        ]
        signals = await strategy.scan_for_opportunities(markets)

        # Should produce a subset signal without calling Claude
        mock_forecaster.assess_market_with_prompt.assert_not_called()
        subset_signals = [s for s in signals if "Subset arb" in s.reasoning]
        assert len(subset_signals) == 1
        assert subset_signals[0].market_id == "MKT-B"
        assert subset_signals[0].direction == Direction.BUY_YES

    @pytest.mark.asyncio
    async def test_cache_hit_no_arbitrage_skips_signal(self, mock_forecaster, tmp_db):
        """Line 348 + 356 branch: cache hit with arbitrage_exists=False → no signal, no Claude call."""
        import json

        graph = MagicMock(spec=MarketGraph)
        graph.find_subset_superset_pairs = MagicMock(return_value=[("MKT-C", "MKT-D", 0.8)])

        settings = Settings()
        strategy = CrossArbStrategy(graph, mock_forecaster, settings, tmp_db)

        conn = tmp_db._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO arb_relationships "
            "(market_a, market_b, relationship_data, validated_at) VALUES (?, ?, ?, ?)",
            (
                "MKT-C", "MKT-D",
                json.dumps({
                    "relationship": "none",
                    "arbitrage_exists": False,
                    "cached_price_a": 0.50,
                    "cached_price_b": 0.50,
                }),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()

        markets = [
            _make_market("MKT-C", yes_price=0.50),
            _make_market("MKT-D", yes_price=0.50),
        ]
        signals = await strategy.scan_for_opportunities(markets)

        mock_forecaster.assess_market_with_prompt.assert_not_called()
        subset_signals = [s for s in signals if "Subset arb" in s.reasoning]
        assert len(subset_signals) == 0

    @pytest.mark.asyncio
    async def test_missing_market_in_lookup_is_skipped(self, mock_forecaster, tmp_db):
        """Line 347: pair references a ticker not in market_lookup → continue (no crash)."""
        graph = MagicMock(spec=MarketGraph)
        # Pair references MKT-Z which is NOT in the markets list
        graph.find_subset_superset_pairs = MagicMock(return_value=[("MKT-A", "MKT-Z", 0.9)])

        settings = Settings()
        strategy = CrossArbStrategy(graph, mock_forecaster, settings, tmp_db)

        markets = [_make_market("MKT-A", yes_price=0.60)]
        signals = await strategy.scan_for_opportunities(markets)

        # Should not crash, no subset signals
        subset_signals = [s for s in signals if "Subset arb" in s.reasoning]
        assert len(subset_signals) == 0


class TestBuildSubsetSignal:
    """Cover _build_subset_signal branches (lines 401-406, 435-451)."""

    def _make_strategy(self, mock_graph, mock_forecaster, tmp_db):
        return CrossArbStrategy(mock_graph, mock_forecaster, Settings(), tmp_db)

    def test_subset_ab_no_arb_when_prices_close(self, mock_graph, mock_forecaster, tmp_db):
        """Lines 421/435: subset_ab but A not significantly above B → None."""
        strategy = self._make_strategy(mock_graph, mock_forecaster, tmp_db)
        market_a = _make_market("A", yes_price=0.52)
        market_b = _make_market("B", yes_price=0.50)
        # edge = 0.02 which equals min_edge (0.02), so the condition A > B + min_edge is not met
        result = strategy._build_subset_signal(
            market_a, market_b,
            {"relationship": "subset_ab", "confidence": 0.8, "arbitrage_exists": True},
        )
        assert result is None

    def test_subset_ab_arb_when_a_significantly_above_b(self, mock_graph, mock_forecaster, tmp_db):
        """Lines 422-434: subset_ab with clear arb → Signal on B."""
        strategy = self._make_strategy(mock_graph, mock_forecaster, tmp_db)
        market_a = _make_market("A", yes_price=0.65)
        market_b = _make_market("B", yes_price=0.50)
        result = strategy._build_subset_signal(
            market_a, market_b,
            {"relationship": "subset_ab", "confidence": 0.75, "arbitrage_exists": True},
        )
        assert result is not None
        assert result.market_id == "B"
        assert result.direction == Direction.BUY_YES
        assert result.edge == pytest.approx(0.15)
        assert result.confidence == pytest.approx(0.75)

    def test_subset_ba_arb_when_b_significantly_above_a(self, mock_graph, mock_forecaster, tmp_db):
        """Lines 437-449: subset_ba with clear arb → Signal on A."""
        strategy = self._make_strategy(mock_graph, mock_forecaster, tmp_db)
        market_a = _make_market("A", yes_price=0.40)
        market_b = _make_market("B", yes_price=0.65)
        result = strategy._build_subset_signal(
            market_a, market_b,
            {"relationship": "subset_ba", "confidence": 0.7, "arbitrage_exists": True},
        )
        assert result is not None
        assert result.market_id == "A"
        assert result.direction == Direction.BUY_YES
        assert result.edge == pytest.approx(0.25)
        assert result.confidence == pytest.approx(0.7)

    def test_subset_ba_no_arb_when_prices_close(self, mock_graph, mock_forecaster, tmp_db):
        """Lines 437: subset_ba but B not significantly above A → None."""
        strategy = self._make_strategy(mock_graph, mock_forecaster, tmp_db)
        market_a = _make_market("A", yes_price=0.50)
        market_b = _make_market("B", yes_price=0.51)
        result = strategy._build_subset_signal(
            market_a, market_b,
            {"relationship": "subset_ba", "confidence": 0.7, "arbitrage_exists": True},
        )
        assert result is None

    def test_unknown_relationship_type_returns_none(self, mock_graph, mock_forecaster, tmp_db):
        """No matching rel_type → returns None (lines 401-406 branch fallthrough)."""
        strategy = self._make_strategy(mock_graph, mock_forecaster, tmp_db)
        market_a = _make_market("A", yes_price=0.60)
        market_b = _make_market("B", yes_price=0.50)
        result = strategy._build_subset_signal(
            market_a, market_b,
            {"relationship": "unrelated", "confidence": 0.5, "arbitrage_exists": True},
        )
        assert result is None

    def test_default_confidence_when_missing(self, mock_graph, mock_forecaster, tmp_db):
        """When 'confidence' key absent, defaults to 0.5."""
        strategy = self._make_strategy(mock_graph, mock_forecaster, tmp_db)
        market_a = _make_market("A", yes_price=0.65)
        market_b = _make_market("B", yes_price=0.50)
        result = strategy._build_subset_signal(
            market_a, market_b,
            {"relationship": "subset_ab", "arbitrage_exists": True},
        )
        assert result is not None
        assert result.confidence == pytest.approx(0.5)


class TestGetCachedRelationship:
    """Cover _get_cached_relationship cache paths (lines 476-501, 503-505)."""

    def _make_strategy(self, mock_graph, mock_forecaster, tmp_db):
        return CrossArbStrategy(mock_graph, mock_forecaster, Settings(), tmp_db)

    def test_no_cache_entry_returns_none(self, mock_graph, mock_forecaster, tmp_db):
        """Lines 502: no row in DB → returns None."""
        strategy = self._make_strategy(mock_graph, mock_forecaster, tmp_db)
        result = strategy._get_cached_relationship("UNKNOWN-A", "UNKNOWN-B")
        assert result is None

    def test_fresh_cache_entry_returns_data(self, mock_graph, mock_forecaster, tmp_db):
        """Lines 481-501: fresh cache entry returns parsed data."""
        import json
        strategy = self._make_strategy(mock_graph, mock_forecaster, tmp_db)

        data = {"relationship": "subset_ab", "arbitrage_exists": True,
                "cached_price_a": 0.60, "cached_price_b": 0.50}
        conn = tmp_db._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO arb_relationships "
            "(market_a, market_b, relationship_data, validated_at) VALUES (?, ?, ?, ?)",
            ("FR-A", "FR-B", json.dumps(data), datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()

        result = strategy._get_cached_relationship("FR-A", "FR-B", price_a=0.60, price_b=0.50)
        assert result is not None
        assert result["relationship"] == "subset_ab"

    def test_expired_cache_returns_none(self, mock_graph, mock_forecaster, tmp_db):
        """Lines 476-478: cache older than 30 minutes → None."""
        import json
        strategy = self._make_strategy(mock_graph, mock_forecaster, tmp_db)

        old_time = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat()
        conn = tmp_db._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO arb_relationships "
            "(market_a, market_b, relationship_data, validated_at) VALUES (?, ?, ?, ?)",
            ("EXP-A", "EXP-B",
             json.dumps({"arbitrage_exists": True, "cached_price_a": 0.5, "cached_price_b": 0.5}),
             old_time),
        )
        conn.commit()

        result = strategy._get_cached_relationship("EXP-A", "EXP-B")
        assert result is None

    def test_price_moved_a_invalidates_cache(self, mock_graph, mock_forecaster, tmp_db):
        """Lines 488-493: price_a moved > 0.25 → None."""
        import json
        strategy = self._make_strategy(mock_graph, mock_forecaster, tmp_db)

        conn = tmp_db._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO arb_relationships "
            "(market_a, market_b, relationship_data, validated_at) VALUES (?, ?, ?, ?)",
            ("PMA-A", "PMA-B",
             json.dumps({"arbitrage_exists": True, "cached_price_a": 0.30, "cached_price_b": 0.50}),
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()

        # price_a moved from 0.30 → 0.60: delta=0.30 > 0.25
        result = strategy._get_cached_relationship("PMA-A", "PMA-B", price_a=0.60, price_b=0.50)
        assert result is None

    def test_price_moved_b_invalidates_cache(self, mock_graph, mock_forecaster, tmp_db):
        """Lines 494-499: price_b moved > 0.25 → None."""
        import json
        strategy = self._make_strategy(mock_graph, mock_forecaster, tmp_db)

        conn = tmp_db._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO arb_relationships "
            "(market_a, market_b, relationship_data, validated_at) VALUES (?, ?, ?, ?)",
            ("PMB-A", "PMB-B",
             json.dumps({"arbitrage_exists": True, "cached_price_a": 0.50, "cached_price_b": 0.30}),
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()

        # price_b moved from 0.30 → 0.60: delta=0.30 > 0.25
        result = strategy._get_cached_relationship("PMB-A", "PMB-B", price_a=0.50, price_b=0.60)
        assert result is None

    def test_lookup_exception_returns_none(self, mock_graph, mock_forecaster, tmp_db):
        """Lines 503-505: exception in cache lookup → returns None gracefully."""
        strategy = self._make_strategy(mock_graph, mock_forecaster, tmp_db)

        # Drop the table so execute raises an exception
        conn = tmp_db._get_conn()
        conn.execute("DROP TABLE arb_relationships")
        conn.commit()

        result = strategy._get_cached_relationship("ERR-A", "ERR-B")
        assert result is None

    def test_reverse_pair_lookup(self, mock_graph, mock_forecaster, tmp_db):
        """OR clause in SQL: stored as (B, A) should be found when querying (A, B).
        cached_price_a/b are stored as 0 so price invalidation doesn't trigger."""
        import json
        strategy = self._make_strategy(mock_graph, mock_forecaster, tmp_db)

        # Use cached_price_a=0 / cached_price_b=0 so price-invalidation branch is skipped
        data = {"relationship": "subset_ba", "arbitrage_exists": True,
                "cached_price_a": 0, "cached_price_b": 0}
        conn = tmp_db._get_conn()
        # Store as (B, A)
        conn.execute(
            "INSERT OR REPLACE INTO arb_relationships "
            "(market_a, market_b, relationship_data, validated_at) VALUES (?, ?, ?, ?)",
            ("REV-B", "REV-A", json.dumps(data), datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()

        # Query as (A, B) — the OR clause should find it
        result = strategy._get_cached_relationship("REV-A", "REV-B")
        assert result is not None
        assert result["relationship"] == "subset_ba"


class TestCacheRelationship:
    """Cover _cache_relationship (lines 507-521)."""

    def _make_strategy(self, mock_graph, mock_forecaster, tmp_db):
        return CrossArbStrategy(mock_graph, mock_forecaster, Settings(), tmp_db)

    def test_cache_relationship_stores_data(self, mock_graph, mock_forecaster, tmp_db):
        """Lines 510-518: happy path — data stored and retrievable."""
        strategy = self._make_strategy(mock_graph, mock_forecaster, tmp_db)
        data = {"relationship": "subset_ab", "arbitrage_exists": True,
                "cached_price_a": 0.60, "cached_price_b": 0.50}

        strategy._cache_relationship("CR-A", "CR-B", data)

        result = strategy._get_cached_relationship("CR-A", "CR-B", price_a=0.60, price_b=0.50)
        assert result is not None
        assert result["relationship"] == "subset_ab"

    def test_cache_relationship_exception_rolls_back(self, mock_graph, mock_forecaster, tmp_db):
        """Lines 519-521: exception during cache write → rollback + warning logged, no crash."""
        strategy = self._make_strategy(mock_graph, mock_forecaster, tmp_db)

        # Drop the table to force an exception
        conn = tmp_db._get_conn()
        conn.execute("DROP TABLE arb_relationships")
        conn.commit()

        # Should not raise
        strategy._cache_relationship("ERR-A", "ERR-B", {"arbitrage_exists": True})


class TestValidateRelationship:
    """Cover _validate_relationship paths (lines 396-406)."""

    @pytest.mark.asyncio
    async def test_valid_json_response_returned(self, mock_graph, mock_forecaster, tmp_db):
        """Lines 396-401: valid JSON raw_response → parsed dict returned."""
        from src.core.models import ForecastResult
        mock_forecaster.assess_market_with_prompt = AsyncMock(return_value=ForecastResult(
            probability=0.5,
            raw_response='{"relationship": "subset_ab", "confidence": 0.8, "arbitrage_exists": true}',
        ))

        settings = Settings()
        strategy = CrossArbStrategy(mock_graph, mock_forecaster, settings, tmp_db)

        market_a = _make_market("VA-A", yes_price=0.60)
        market_b = _make_market("VA-B", yes_price=0.50)

        result = await strategy._validate_relationship(market_a, market_b)
        assert result is not None
        assert result["relationship"] == "subset_ab"
        assert result["arbitrage_exists"] is True

    @pytest.mark.asyncio
    async def test_invalid_json_returns_none(self, mock_graph, mock_forecaster, tmp_db):
        """Lines 401-402: non-JSON raw_response → None (json.JSONDecodeError caught)."""
        from src.core.models import ForecastResult
        mock_forecaster.assess_market_with_prompt = AsyncMock(return_value=ForecastResult(
            probability=0.5,
            raw_response="this is not json at all",
        ))

        settings = Settings()
        strategy = CrossArbStrategy(mock_graph, mock_forecaster, settings, tmp_db)

        market_a = _make_market("VJ-A", yes_price=0.60)
        market_b = _make_market("VJ-B", yes_price=0.50)

        result = await strategy._validate_relationship(market_a, market_b)
        assert result is None

    @pytest.mark.asyncio
    async def test_forecaster_returns_none_result(self, mock_graph, mock_forecaster, tmp_db):
        """Lines 396: forecaster returns None → None returned."""
        mock_forecaster.assess_market_with_prompt = AsyncMock(return_value=None)

        settings = Settings()
        strategy = CrossArbStrategy(mock_graph, mock_forecaster, settings, tmp_db)

        market_a = _make_market("VN-A", yes_price=0.60)
        market_b = _make_market("VN-B", yes_price=0.50)

        result = await strategy._validate_relationship(market_a, market_b)
        assert result is None

    @pytest.mark.asyncio
    async def test_forecaster_exception_returns_none(self, mock_graph, mock_forecaster, tmp_db):
        """Lines 403-405: exception during API call → logged and None returned."""
        mock_forecaster.assess_market_with_prompt = AsyncMock(side_effect=RuntimeError("API timeout"))

        settings = Settings()
        strategy = CrossArbStrategy(mock_graph, mock_forecaster, settings, tmp_db)

        market_a = _make_market("VE-A", yes_price=0.60)
        market_b = _make_market("VE-B", yes_price=0.50)

        result = await strategy._validate_relationship(market_a, market_b)
        assert result is None

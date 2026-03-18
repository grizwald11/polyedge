"""Tests for the backtest script — mocks Kalshi API and Claude to verify flow."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.models import ForecastResult
from src.scripts.backtest import (
    build_settled_market,
    fetch_settled_events,
    filter_backtestable_markets,
    get_actual_outcome,
    run_backtest,
)


# ──────────────────────────────────────
# Fixtures
# ──────────────────────────────────────

def _make_settled_market(
    ticker: str = "TEST-MKT",
    title: str = "Will X happen?",
    result: str = "yes",
    volume: str = "50000.00",
    last_price: str = "0.65",
    category: str = "Politics",
    volume_24h: str = "0.00",
) -> dict:
    """Build a raw settled market dict matching Kalshi API format."""
    return {
        "ticker": ticker,
        "title": title,
        "subtitle": "Test subtitle",
        "rules_primary": "Resolves YES if X happens.",
        "result": result,
        "status": "settled",
        "yes_bid_dollars": "0.00",
        "yes_ask_dollars": "0.00",
        "no_bid_dollars": "0.00",
        "no_ask_dollars": "0.00",
        "last_price_dollars": last_price,
        "volume_fp": volume,
        "volume_24h_fp": volume_24h,
        "open_interest_fp": "0.00",
        "close_time": (datetime.now(timezone.utc) - timedelta(days=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "event_ticker": "TEST-EVENT",
        "_event_category": category,
    }


@pytest.fixture
def settled_markets() -> list[dict]:
    """A batch of settled markets for testing."""
    return [
        _make_settled_market("MKT-YES", "Will A happen?", "yes", "50000.00", "0.70"),
        _make_settled_market("MKT-NO", "Will B happen?", "no", "30000.00", "0.30"),
        _make_settled_market("MKT-LOW", "Will C happen?", "yes", "10.00", "0.50"),  # low volume
        _make_settled_market("MKT-NONE", "Will D happen?", "", "50000.00", "0.50"),  # no result
    ]


# ──────────────────────────────────────
# Unit tests
# ──────────────────────────────────────

class TestFilterBacktestableMarkets:
    def test_filters_by_result(self, settled_markets):
        filtered = filter_backtestable_markets(settled_markets)
        tickers = [m["ticker"] for m in filtered]
        # MKT-NONE has no result, should be excluded
        assert "MKT-NONE" not in tickers
        assert "MKT-YES" in tickers
        assert "MKT-NO" in tickers

    def test_filters_low_volume(self, settled_markets):
        filtered = filter_backtestable_markets(settled_markets)
        tickers = [m["ticker"] for m in filtered]
        # MKT-LOW has volume=10, below 100 threshold
        assert "MKT-LOW" not in tickers

    def test_filters_missing_title(self):
        raw = [_make_settled_market()]
        raw[0]["title"] = ""
        raw[0]["question"] = ""
        filtered = filter_backtestable_markets(raw)
        assert len(filtered) == 0

    def test_empty_input(self):
        assert filter_backtestable_markets([]) == []


class TestBuildSettledMarket:
    def test_builds_market_from_settled_data(self):
        raw = _make_settled_market(last_price="0.65")
        market = build_settled_market(raw)
        assert market is not None
        assert market.ticker == "TEST-MKT"
        assert market.question == "Will X happen?"
        # Settled market with zeroed book should use last_price
        assert market.yes_price == pytest.approx(0.65, abs=0.01)

    def test_returns_none_for_invalid_data(self):
        raw = {"status": "settled"}  # missing ticker
        market = build_settled_market(raw)
        assert market is None


class TestGetActualOutcome:
    def test_yes_result(self):
        assert get_actual_outcome({"result": "yes"}) is True

    def test_no_result(self):
        assert get_actual_outcome({"result": "no"}) is False

    def test_missing_result(self):
        assert get_actual_outcome({}) is False


# ──────────────────────────────────────
# Async tests
# ──────────────────────────────────────

@pytest.mark.asyncio
class TestFetchSettledEvents:
    async def test_fetches_and_flattens_events(self):
        mock_kalshi = AsyncMock()
        mock_kalshi._request = AsyncMock(return_value={
            "events": [
                {
                    "category": "Politics",
                    "markets": [
                        _make_settled_market("A", "Q1?", "yes"),
                        _make_settled_market("B", "Q2?", "no"),
                    ],
                },
                {
                    "category": "Economics",
                    "markets": [
                        _make_settled_market("C", "Q3?", "yes"),
                    ],
                },
            ],
            "cursor": None,
        })

        markets = await fetch_settled_events(mock_kalshi, max_events=200)
        assert len(markets) == 3
        # Verify event category is tagged
        assert markets[0]["_event_category"] == "Politics"
        assert markets[2]["_event_category"] == "Economics"

    async def test_handles_empty_response(self):
        mock_kalshi = AsyncMock()
        mock_kalshi._request = AsyncMock(return_value={"events": [], "cursor": None})

        markets = await fetch_settled_events(mock_kalshi, max_events=200)
        assert markets == []

    async def test_handles_none_response(self):
        mock_kalshi = AsyncMock()
        mock_kalshi._request = AsyncMock(return_value=None)

        markets = await fetch_settled_events(mock_kalshi, max_events=200)
        assert markets == []

    async def test_paginates(self):
        """Should follow cursor for multiple pages."""
        mock_kalshi = AsyncMock()

        call_count = 0

        async def mock_request(method, path, params=None, json_body=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {
                    "events": [{"category": "Politics", "markets": [
                        _make_settled_market("A", "Q1?", "yes"),
                    ]}],
                    "cursor": "page2",
                }
            else:
                return {
                    "events": [{"category": "Economics", "markets": [
                        _make_settled_market("B", "Q2?", "no"),
                    ]}],
                    "cursor": None,
                }

        mock_kalshi._request = mock_request
        markets = await fetch_settled_events(mock_kalshi, max_events=200)
        assert len(markets) == 2
        assert call_count == 2


@pytest.mark.asyncio
class TestRunBacktest:
    async def test_dry_run_does_not_call_claude(self, tmp_db, settings, capsys):
        """Dry run should list markets but not invoke Claude."""
        with patch("src.scripts.backtest.fetch_settled_events") as mock_fetch:
            mock_fetch.return_value = [
                _make_settled_market("MKT-1", "Will A?", "yes", "50000.00"),
                _make_settled_market("MKT-2", "Will B?", "no", "30000.00"),
            ]

            with patch("src.scripts.backtest.KalshiClient") as MockKalshi:
                mock_instance = AsyncMock()
                MockKalshi.return_value = mock_instance

                await run_backtest(
                    settings=settings,
                    db=tmp_db,
                    limit=10,
                    delay=0,
                    dry_run=True,
                )

        output = capsys.readouterr().out
        assert "DRY RUN" in output
        assert "MKT-1" in output
        assert "MKT-2" in output

    async def test_full_run_stores_predictions(self, tmp_db, settings):
        """Full run should call Claude and store predictions + resolutions."""
        mock_forecast = ForecastResult(
            probability=0.70,
            confidence_low=0.60,
            confidence_high=0.80,
            key_factors_for=["Factor A"],
            key_factors_against=["Factor B"],
            uncertainties=["Uncertain"],
            reasoning="Test reasoning",
            model_used="claude-sonnet-4-6",
            tokens_used=500,
            latency_ms=1000,
        )

        with patch("src.scripts.backtest.fetch_settled_events") as mock_fetch, \
             patch("src.scripts.backtest.KalshiClient") as MockKalshi, \
             patch("src.scripts.backtest.ClaudeForecaster") as MockForecaster, \
             patch("src.scripts.backtest.NewsResearcher") as MockNews:

            mock_fetch.return_value = [
                _make_settled_market("MKT-1", "Will A?", "yes", "50000.00", "0.65"),
            ]

            mock_kalshi_instance = AsyncMock()
            MockKalshi.return_value = mock_kalshi_instance

            mock_forecaster = AsyncMock()
            mock_forecaster.assess_market = AsyncMock(return_value=mock_forecast)
            MockForecaster.return_value = mock_forecaster

            mock_news = AsyncMock()
            mock_news.get_context = AsyncMock(return_value="Test news context")
            MockNews.return_value = mock_news

            await run_backtest(
                settings=settings,
                db=tmp_db,
                limit=10,
                delay=0,
                dry_run=False,
            )

        # Verify prediction was stored and resolved
        resolved = tmp_db.get_resolved_predictions()
        assert len(resolved) == 1
        assert resolved[0]["market_id"] == "MKT-1"
        assert resolved[0]["predicted_probability"] == pytest.approx(0.70)
        assert resolved[0]["actual_outcome"] == 1  # YES
        assert resolved[0]["brier_score"] == pytest.approx((0.70 - 1.0) ** 2)

    async def test_handles_forecast_error_gracefully(self, tmp_db, settings, capsys):
        """Should continue processing after a forecast error."""
        with patch("src.scripts.backtest.fetch_settled_events") as mock_fetch, \
             patch("src.scripts.backtest.KalshiClient") as MockKalshi, \
             patch("src.scripts.backtest.ClaudeForecaster") as MockForecaster, \
             patch("src.scripts.backtest.NewsResearcher") as MockNews:

            mock_fetch.return_value = [
                _make_settled_market("MKT-ERR", "Will X?", "yes", "50000.00"),
                _make_settled_market("MKT-OK", "Will Y?", "no", "50000.00", "0.40"),
            ]

            MockKalshi.return_value = AsyncMock()

            mock_forecaster = AsyncMock()
            call_count = 0

            async def mock_assess(*args, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise RuntimeError("API error")
                return ForecastResult(
                    probability=0.30,
                    confidence_low=0.20,
                    confidence_high=0.40,
                    key_factors_for=[],
                    key_factors_against=[],
                    uncertainties=[],
                    reasoning="test",
                    model_used="test",
                    tokens_used=0,
                    latency_ms=0,
                )

            mock_forecaster.assess_market = mock_assess
            MockForecaster.return_value = mock_forecaster

            mock_news = AsyncMock()
            mock_news.get_context = AsyncMock(return_value="")
            MockNews.return_value = mock_news

            await run_backtest(
                settings=settings,
                db=tmp_db,
                limit=10,
                delay=0,
                dry_run=False,
            )

        output = capsys.readouterr().out
        assert "ERR" in output
        # Second market should still be processed
        resolved = tmp_db.get_resolved_predictions()
        assert len(resolved) == 1
        assert resolved[0]["market_id"] == "MKT-OK"

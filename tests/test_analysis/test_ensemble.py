"""Tests for ensemble forecasting."""

from src.analysis.ensemble import ensemble_forecast
from src.core.models import ForecastResult, EnsembleForecast


def _make_forecast(prob: float) -> ForecastResult:
    return ForecastResult(
        probability=prob,
        confidence_low=max(0, prob - 0.1),
        confidence_high=min(1, prob + 0.1),
        reasoning="test",
    )


class TestEnsembleForecast:
    def test_weighted_average(self):
        forecast = _make_forecast(0.70)
        result = ensemble_forecast(forecast, market_price=0.50, claude_weight=0.7)
        # 0.70 * 0.7 + 0.50 * 0.3 = 0.49 + 0.15 = 0.64
        assert abs(result.final_probability - 0.64) < 0.01

    def test_edge_positive(self):
        forecast = _make_forecast(0.70)
        result = ensemble_forecast(forecast, market_price=0.50)
        assert result.edge > 0  # Claude thinks YES is underpriced

    def test_edge_negative(self):
        forecast = _make_forecast(0.30)
        result = ensemble_forecast(forecast, market_price=0.50)
        assert result.edge < 0  # Claude thinks YES is overpriced

    def test_edge_zero_when_agreement(self):
        forecast = _make_forecast(0.50)
        result = ensemble_forecast(forecast, market_price=0.50)
        assert abs(result.edge) < 0.01

    def test_confidence_decreases_with_disagreement(self):
        # Small disagreement → higher confidence
        f1 = _make_forecast(0.52)
        r1 = ensemble_forecast(f1, market_price=0.50)

        # Large disagreement → lower confidence
        f2 = _make_forecast(0.80)
        r2 = ensemble_forecast(f2, market_price=0.50)

        assert r1.confidence > r2.confidence

    def test_clamped_to_valid_range(self):
        forecast = _make_forecast(0.99)
        result = ensemble_forecast(forecast, market_price=0.99, claude_weight=1.0)
        assert result.final_probability <= 0.99
        assert result.final_probability >= 0.01

    def test_includes_individual_forecasts(self):
        forecast = _make_forecast(0.60)
        result = ensemble_forecast(forecast, market_price=0.50)
        assert len(result.individual_forecasts) == 1
        assert result.individual_forecasts[0].probability == 0.60

    def test_market_price_stored(self):
        forecast = _make_forecast(0.60)
        result = ensemble_forecast(forecast, market_price=0.45)
        assert result.market_price == 0.45

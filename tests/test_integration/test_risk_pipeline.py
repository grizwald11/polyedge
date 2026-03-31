"""Integration test: full risk pipeline (M-9 audit fix).

Tests signal -> Kelly sizing -> risk engine -> circuit breaker flow
in a single end-to-end scenario to catch interaction bugs.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.config import Settings
from src.core.models import (
    Direction,
    Market,
    MarketCategory,
    MarketToken,
    Signal,
    StrategyName,
)
from src.execution.position_manager import PositionManager
from src.risk.circuit_breaker import CircuitBreaker
from src.risk.kelly_sizer import KellySizer
from src.risk.risk_engine import RiskEngine
from src.storage.database import Database


@pytest.fixture
def pipeline_db(tmp_path) -> Database:
    db_path = str(tmp_path / "pipeline_test.db")
    db = Database(db_path=db_path, wal_mode=True)
    conn = db._get_conn()
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.commit()
    return db


@pytest.fixture
def pipeline_settings() -> Settings:
    return Settings(
        trading=Settings.model_fields["trading"].default_factory()
    )


@pytest.fixture
def pipeline_market() -> Market:
    return Market(
        ticker="PIPELINE-TEST-MKT",
        question="Will the pipeline test pass?",
        description="Resolves YES.",
        category=MarketCategory.POLITICS,
        tags=["Politics"],
        tokens=[
            MarketToken(token_id="PIPELINE-TEST-MKT_yes", outcome="Yes", price=0.40),
            MarketToken(token_id="PIPELINE-TEST-MKT_no", outcome="No", price=0.60),
        ],
        end_date=datetime.now(timezone.utc) + timedelta(days=30),
        volume_24h=50000.0,
        volume_total=500000.0,
        liquidity=20000.0,
        spread=0.02,
        active=True,
        closed=False,
    )


@pytest.fixture
def pipeline_signal() -> Signal:
    return Signal(
        strategy=StrategyName.AI_PROBABILITY,
        market_id="PIPELINE-TEST-MKT",
        market_question="Will the pipeline test pass?",
        direction=Direction.BUY_YES,
        edge=0.10,
        probability_estimate=0.50,
        market_price=0.40,
        confidence=0.75,
        reasoning="Test signal with 10% edge.",
    )


class TestRiskPipelineIntegration:
    """M-9: End-to-end test exercising Kelly -> RiskEngine -> CircuitBreaker."""

    def test_full_pipeline_pass(self, pipeline_db, pipeline_settings, pipeline_market, pipeline_signal):
        """Signal with good edge passes through entire pipeline."""
        cb = CircuitBreaker(pipeline_settings, pipeline_db)
        sizer = KellySizer(pipeline_settings)
        pm = PositionManager(pipeline_db, bankroll=pipeline_settings.trading.bankroll)
        risk = RiskEngine(pipeline_settings, pm, circuit_breaker=cb, db=pipeline_db)

        # Size the position
        contracts = sizer.calculate_position_size(
            edge=pipeline_signal.edge,
            probability=pipeline_signal.probability_estimate,
            bankroll=pipeline_settings.trading.bankroll,
            current_exposure=0.0,
            order_price=pipeline_market.yes_price,
            confidence=pipeline_signal.confidence,
        )
        assert contracts >= 1, "Kelly should size at least 1 contract"

        # Proposed cost
        proposed_cost = contracts * pipeline_market.yes_price

        # Run risk checks
        result = risk.check_all(
            signal=pipeline_signal,
            market=pipeline_market,
            proposed_size=contracts,
            proposed_cost=proposed_cost,
        )
        assert result.passed, f"Risk checks should pass, but failed: {result.failed_checks}"

    def test_circuit_breaker_blocks_after_losses(self, pipeline_db, pipeline_settings, pipeline_market, pipeline_signal):
        """Circuit breaker halts trading after excessive daily losses."""
        cb = CircuitBreaker(pipeline_settings, pipeline_db)
        pm = PositionManager(pipeline_db, bankroll=pipeline_settings.trading.bankroll)
        risk = RiskEngine(pipeline_settings, pm, circuit_breaker=cb, db=pipeline_db)

        # Insert a trade with large realized loss so get_daily_pnl() returns negative
        bankroll = pipeline_settings.trading.bankroll
        loss_amount = bankroll * 0.12  # 12% loss exceeds 10% daily limit
        now = datetime.now(timezone.utc).isoformat()
        conn = pipeline_db._get_conn()
        conn.execute(
            "INSERT INTO trades (order_id, market_id, token_id, side, price, size, "
            "fee, realized_pnl, strategy, paper, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("test-loss", "PIPELINE-TEST-MKT", "PIPELINE-TEST-MKT_yes", "sell",
             0.30, 10, 0.0, -loss_amount, "ai_probability", 1, now),
        )
        conn.commit()

        # Now check() reads daily PnL from DB and should trigger the halt
        cb.check(bankroll=bankroll, unrealized_pnl=0.0)

        assert cb.is_halted(), "Circuit breaker should be halted after 12% daily loss"

        # Size the position
        sizer = KellySizer(pipeline_settings)
        contracts = sizer.calculate_position_size(
            edge=pipeline_signal.edge,
            probability=pipeline_signal.probability_estimate,
            bankroll=pipeline_settings.trading.bankroll,
            current_exposure=0.0,
            order_price=pipeline_market.yes_price,
            confidence=pipeline_signal.confidence,
        )

        # Risk engine should block
        proposed_cost = contracts * pipeline_market.yes_price
        result = risk.check_all(
            signal=pipeline_signal,
            market=pipeline_market,
            proposed_size=contracts,
            proposed_cost=proposed_cost,
        )
        assert not result.passed, "Risk engine should block when circuit breaker is halted"
        assert any("circuit" in c.lower() or "halt" in c.lower() for c in result.failed_checks), \
            f"Should fail on circuit breaker check, got: {result.failed_checks}"

    def test_kelly_reduces_on_poor_calibration(self, pipeline_db, pipeline_settings, pipeline_market, pipeline_signal):
        """Kelly sizer reduces sizing when Brier score is poor."""
        sizer = KellySizer(pipeline_settings)

        # Good calibration — update multiplier then size
        sizer.update_calibration_multiplier(brier_score=0.15)
        good_contracts = sizer.calculate_position_size(
            edge=pipeline_signal.edge,
            probability=pipeline_signal.probability_estimate,
            bankroll=pipeline_settings.trading.bankroll,
            current_exposure=0.0,
            order_price=pipeline_market.yes_price,
            confidence=pipeline_signal.confidence,
        )

        # Poor calibration — update multiplier then size
        sizer.update_calibration_multiplier(brier_score=0.28)
        poor_contracts = sizer.calculate_position_size(
            edge=pipeline_signal.edge,
            probability=pipeline_signal.probability_estimate,
            bankroll=pipeline_settings.trading.bankroll,
            current_exposure=0.0,
            order_price=pipeline_market.yes_price,
            confidence=pipeline_signal.confidence,
        )

        assert poor_contracts < good_contracts, \
            f"Poor calibration should reduce sizing: good={good_contracts}, poor={poor_contracts}"

    def test_exposure_cap_limits_kelly(self, pipeline_db, pipeline_settings, pipeline_market, pipeline_signal):
        """Kelly sizing respects total exposure cap from risk settings."""
        sizer = KellySizer(pipeline_settings)

        # Nearly at exposure limit (39% of 40% cap)
        current_exposure = pipeline_settings.trading.bankroll * 0.39

        contracts = sizer.calculate_position_size(
            edge=pipeline_signal.edge,
            probability=pipeline_signal.probability_estimate,
            bankroll=pipeline_settings.trading.bankroll,
            current_exposure=current_exposure,
            order_price=pipeline_market.yes_price,
            confidence=pipeline_signal.confidence,
        )

        max_remaining = pipeline_settings.trading.bankroll * pipeline_settings.trading.max_total_exposure_pct - current_exposure
        cost = contracts * pipeline_market.yes_price
        assert cost <= max_remaining + 0.01, \
            f"Cost ${cost:.2f} should not exceed remaining exposure cap ${max_remaining:.2f}"

    def test_excluded_category_rejected(self, pipeline_db, pipeline_settings):
        """Crypto market rejected by risk engine even with good signal."""
        crypto_market = Market(
            ticker="BTC-TEST",
            question="BTC up?",
            category=MarketCategory.CRYPTO,
            tags=["Crypto Prices"],
            tokens=[
                MarketToken(token_id="BTC-TEST_yes", outcome="Yes", price=0.50),
                MarketToken(token_id="BTC-TEST_no", outcome="No", price=0.50),
            ],
            volume_24h=100000.0,
            active=True,
        )
        crypto_signal = Signal(
            strategy=StrategyName.AI_PROBABILITY,
            market_id="BTC-TEST",
            market_question="BTC up?",
            direction=Direction.BUY_YES,
            edge=0.10,
            probability_estimate=0.60,
            market_price=0.50,
            confidence=0.80,
            reasoning="Test",
        )

        cb = CircuitBreaker(pipeline_settings, pipeline_db)
        pm = PositionManager(pipeline_db, bankroll=pipeline_settings.trading.bankroll)
        risk = RiskEngine(pipeline_settings, pm, circuit_breaker=cb, db=pipeline_db)

        result = risk.check_all(
            signal=crypto_signal,
            market=crypto_market,
            proposed_size=5,
            proposed_cost=25.0,
        )
        assert not result.passed, "Crypto market should be rejected"

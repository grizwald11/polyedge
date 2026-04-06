"""Integration test: position tracking → exit triggers → risk recording.

Tests that positions identified for exit (stop-loss, time-limit, edge-gone)
flow correctly through exit candidate detection and risk engine recording.
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
    Position,
    Side,
    Signal,
    StrategyName,
    Trade,
)
from src.execution.position_manager import (
    PositionManager,
    DEFAULT_STOP_LOSS_PCT,
    DEFAULT_MAX_HOLD_DAYS,
    DEFAULT_EDGE_GONE_THRESHOLD,
)
from src.risk.circuit_breaker import CircuitBreaker
from src.risk.risk_engine import RiskEngine
from src.storage.database import Database


@pytest.fixture
def exit_db(tmp_path) -> Database:
    db_path = str(tmp_path / "exit_test.db")
    db = Database(db_path=db_path, wal_mode=True)
    conn = db._get_conn()
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.commit()
    return db


@pytest.fixture
def exit_settings() -> Settings:
    return Settings(
        trading=Settings.model_fields["trading"].default_factory()
    )


@pytest.fixture
def exit_market() -> Market:
    return Market(
        ticker="EXIT-MKT",
        question="Exit test market?",
        category=MarketCategory.POLITICS,
        tokens=[
            MarketToken(token_id="EXIT-MKT_yes", outcome="Yes", price=0.30),
            MarketToken(token_id="EXIT-MKT_no", outcome="No", price=0.70),
        ],
        end_date=datetime.now(timezone.utc) + timedelta(days=30),
        volume_24h=50000.0,
        liquidity=20000.0,
        active=True,
    )


class TestStopLossExitIntegration:
    """Position opened → price drops past stop-loss → exit candidate → cooldown recorded."""

    def test_stop_loss_triggers_exit_candidate(self, exit_db, exit_settings):
        """Position with large unrealized loss should be flagged for stop-loss exit."""
        pm = PositionManager(exit_db, bankroll=exit_settings.trading.bankroll)

        # Open position at 0.50
        buy = Trade(
            order_id="buy-1",
            market_id="EXIT-MKT",
            token_id="EXIT-MKT_yes",
            side=Side.BUY,
            price=0.50,
            size=10,
            strategy=StrategyName.AI_PROBABILITY,
        )
        pm.update_from_trade(buy)

        pos = pm.get_position("EXIT-MKT")
        assert pos is not None
        assert pos.avg_entry_price == 0.50

        # Update with much lower price (simulate mark-to-market)
        # Stop-loss triggers at DEFAULT_STOP_LOSS_PCT (20%)
        # Price drops from 0.50 to 0.35 → unrealized PnL = (0.35-0.50)*10 = -1.50
        # Loss % = 1.50 / (10 * 0.50) = 30% > 20% stop-loss
        drop_market = Market(
            ticker="EXIT-MKT",
            question="Exit test?",
            category=MarketCategory.POLITICS,
            tokens=[
                MarketToken(token_id="EXIT-MKT_yes", outcome="Yes", price=0.35),
                MarketToken(token_id="EXIT-MKT_no", outcome="No", price=0.65),
            ],
            volume_24h=50000.0,
            active=True,
        )

        pm.update_price("EXIT-MKT", drop_market.yes_price, drop_market.no_price)
        pos = pm.get_position("EXIT-MKT")
        assert pos.unrealized_pnl < 0

        # Check exit candidates
        candidates = pm.get_exit_candidates(markets={"EXIT-MKT": drop_market})
        exit_mkt_ids = [p.market_id for p, reason in candidates]
        exit_reasons = {p.market_id: reason for p, reason in candidates}

        assert "EXIT-MKT" in exit_mkt_ids, \
            f"Should be flagged for exit, candidates: {exit_mkt_ids}"
        assert "stop" in exit_reasons.get("EXIT-MKT", "").lower(), \
            f"Exit reason should mention stop-loss, got: {exit_reasons.get('EXIT-MKT')}"

    def test_stop_loss_exit_records_cooldown(self, exit_db, exit_settings):
        """After stop-loss exit, risk engine should record a loss cooldown."""
        cb = CircuitBreaker(exit_settings, exit_db)
        pm = PositionManager(exit_db, bankroll=exit_settings.trading.bankroll)
        risk = RiskEngine(exit_settings, pm, circuit_breaker=cb, db=exit_db)

        # Record exit with loss
        risk.record_exit("EXIT-MKT", pnl=-5.0)

        # Verify in-memory cooldown
        assert "EXIT-MKT" in risk._cooldowns
        # Duration should be loss cooldown (4h)
        assert risk._cooldown_durations["EXIT-MKT"] == risk.cooldown_loss_seconds

        # Verify DB persistence
        conn = exit_db._get_conn()
        row = conn.execute(
            "SELECT * FROM cooldowns WHERE market_id = 'EXIT-MKT'"
        ).fetchone()
        assert row is not None


class TestTimeLimitExitIntegration:
    """Positions held past max hold time should be exit candidates."""

    def test_old_position_flagged_for_exit(self, exit_db, exit_settings):
        """Position older than DEFAULT_MAX_HOLD_DAYS should be an exit candidate."""
        pm = PositionManager(exit_db, bankroll=exit_settings.trading.bankroll)

        # Open position
        buy = Trade(
            order_id="old-buy",
            market_id="OLD-MKT",
            token_id="OLD-MKT_yes",
            side=Side.BUY,
            price=0.50,
            size=10,
            strategy=StrategyName.AI_PROBABILITY,
        )
        pm.update_from_trade(buy)

        # Backdate the position
        pos = pm.get_position("OLD-MKT")
        pos.opened_at = datetime.now(timezone.utc) - timedelta(days=DEFAULT_MAX_HOLD_DAYS + 1)

        # Create market for the position
        old_market = Market(
            ticker="OLD-MKT",
            question="Old market?",
            category=MarketCategory.POLITICS,
            tokens=[
                MarketToken(token_id="OLD-MKT_yes", outcome="Yes", price=0.52),
                MarketToken(token_id="OLD-MKT_no", outcome="No", price=0.48),
            ],
            volume_24h=50000.0,
            active=True,
        )
        pm.update_price("OLD-MKT", old_market.yes_price, old_market.no_price)

        candidates = pm.get_exit_candidates(markets={"OLD-MKT": old_market})
        exit_mkt_ids = [p.market_id for p, reason in candidates]
        exit_reasons = {p.market_id: reason for p, reason in candidates}

        assert "OLD-MKT" in exit_mkt_ids
        reason = exit_reasons.get("OLD-MKT", "")
        assert "hold" in reason.lower() or "time" in reason.lower() or "days" in reason.lower(), \
            f"Reason should mention time limit, got: {reason}"


class TestEdgeGoneExitIntegration:
    """Position where edge has largely evaporated should be flagged for exit."""

    def test_edge_gone_flagged(self, exit_db, exit_settings):
        """Position where price moved to capture most edge should be exit candidate."""
        pm = PositionManager(exit_db, bankroll=exit_settings.trading.bankroll)

        # Open BUY_YES at 0.40, edge was (0.50 - 0.40 = 0.10)
        buy = Trade(
            order_id="edge-buy",
            market_id="EDGE-MKT",
            token_id="EDGE-MKT_yes",
            side=Side.BUY,
            price=0.40,
            size=10,
            strategy=StrategyName.AI_PROBABILITY,
        )
        pm.update_from_trade(buy)

        # Backdate so MIN_HOLD_BEFORE_EDGE_GONE (24h) is satisfied
        pos = pm.get_position("EDGE-MKT")
        pos.opened_at = datetime.now(timezone.utc) - timedelta(hours=25)

        # Price rose to 0.90 — nearly all upside captured
        # Remaining edge: (1.0 - 0.90) / (1.0 - 0.40) = 0.10 / 0.60 ≈ 0.167
        # This is below DEFAULT_EDGE_GONE_THRESHOLD (0.20)
        edge_market = Market(
            ticker="EDGE-MKT",
            question="Edge gone market?",
            category=MarketCategory.POLITICS,
            tokens=[
                MarketToken(token_id="EDGE-MKT_yes", outcome="Yes", price=0.90),
                MarketToken(token_id="EDGE-MKT_no", outcome="No", price=0.10),
            ],
            end_date=datetime.now(timezone.utc) + timedelta(days=30),
            volume_24h=50000.0,
            active=True,
        )
        pm.update_price("EDGE-MKT", edge_market.yes_price, edge_market.no_price)

        candidates = pm.get_exit_candidates(markets={"EDGE-MKT": edge_market})
        exit_mkt_ids = [p.market_id for p, reason in candidates]

        # Should be flagged — but may also trigger take-profit first
        assert "EDGE-MKT" in exit_mkt_ids, \
            f"EDGE-MKT should be an exit candidate, got: {exit_mkt_ids}"

    def test_fresh_position_not_edge_gone(self, exit_db, exit_settings):
        """Position younger than MIN_HOLD_BEFORE_EDGE_GONE should not trigger edge-gone."""
        pm = PositionManager(exit_db, bankroll=exit_settings.trading.bankroll)

        buy = Trade(
            order_id="fresh-buy",
            market_id="FRESH-MKT",
            token_id="FRESH-MKT_yes",
            side=Side.BUY,
            price=0.40,
            size=10,
            strategy=StrategyName.AI_PROBABILITY,
        )
        pm.update_from_trade(buy)

        # Don't backdate — position is fresh
        fresh_market = Market(
            ticker="FRESH-MKT",
            question="Fresh market?",
            category=MarketCategory.POLITICS,
            tokens=[
                MarketToken(token_id="FRESH-MKT_yes", outcome="Yes", price=0.55),
                MarketToken(token_id="FRESH-MKT_no", outcome="No", price=0.45),
            ],
            end_date=datetime.now(timezone.utc) + timedelta(days=30),
            volume_24h=50000.0,
            active=True,
        )
        pm.update_price("FRESH-MKT", fresh_market.yes_price, fresh_market.no_price)

        candidates = pm.get_exit_candidates(markets={"FRESH-MKT": fresh_market})
        edge_gone_exits = [
            (p, r) for p, r in candidates
            if p.market_id == "FRESH-MKT" and "edge" in r.lower()
        ]
        assert len(edge_gone_exits) == 0, \
            "Fresh position should not trigger edge-gone exit"


class TestExitToCooldownFullCycle:
    """Full cycle: open → exit candidate → execute exit → cooldown blocks re-entry."""

    def test_full_exit_to_cooldown_cycle(self, exit_db, exit_settings, exit_market):
        """Open position, detect stop-loss, record exit, verify cooldown blocks re-entry."""
        cb = CircuitBreaker(exit_settings, exit_db)
        pm = PositionManager(exit_db, bankroll=exit_settings.trading.bankroll)
        risk = RiskEngine(exit_settings, pm, circuit_breaker=cb, db=exit_db)

        # Step 1: Open position
        buy = Trade(
            order_id="cycle-buy",
            market_id="EXIT-MKT",
            token_id="EXIT-MKT_yes",
            side=Side.BUY,
            price=0.50,
            size=10,
            strategy=StrategyName.AI_PROBABILITY,
        )
        pm.update_from_trade(buy)

        # Step 2: Price crashes, detect stop-loss
        crash_market = Market(
            ticker="EXIT-MKT",
            question="Exit test?",
            category=MarketCategory.POLITICS,
            tokens=[
                MarketToken(token_id="EXIT-MKT_yes", outcome="Yes", price=0.35),
                MarketToken(token_id="EXIT-MKT_no", outcome="No", price=0.65),
            ],
            volume_24h=50000.0,
            active=True,
        )
        pm.update_price("EXIT-MKT", crash_market.yes_price, crash_market.no_price)
        candidates = pm.get_exit_candidates(markets={"EXIT-MKT": crash_market})
        assert any(p.market_id == "EXIT-MKT" for p, _ in candidates)

        # Step 3: Execute exit trade
        sell = Trade(
            order_id="cycle-sell",
            market_id="EXIT-MKT",
            token_id="EXIT-MKT_yes",
            side=Side.SELL,
            price=0.35,
            size=10,
            realized_pnl=-1.50,
            strategy=StrategyName.AI_PROBABILITY,
        )
        pm.update_from_trade(sell)
        assert pm.get_position("EXIT-MKT") is None  # Position closed

        # Step 4: Record exit with risk engine
        risk.record_exit("EXIT-MKT", pnl=-1.50)

        # Step 5: Attempt re-entry — should be blocked by cooldown
        reentry_signal = Signal(
            strategy=StrategyName.AI_PROBABILITY,
            market_id="EXIT-MKT",
            market_question="Exit test?",
            direction=Direction.BUY_YES,
            edge=0.10,
            probability_estimate=0.50,
            market_price=0.35,
            confidence=0.75,
        )
        result = risk.check_all(
            signal=reentry_signal,
            market=crash_market,
            proposed_size=5,
            proposed_cost=5 * 0.35,
        )
        assert not result.passed
        assert any("cooldown" in c.lower() for c in result.failed_checks), \
            f"Re-entry should be blocked by cooldown, got: {result.failed_checks}"

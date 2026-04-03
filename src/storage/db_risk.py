"""Circuit breaker, cooldown, and settings database operations mixin.

Split from database.py (M-10) to reduce file size while preserving
backward compatibility via the mixin pattern.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class RiskMixin:
    """Database mixin for circuit breaker state, cooldowns, and key-value settings."""

    # ──────────────────────────────────────
    # Circuit Breaker State
    # ──────────────────────────────────────

    def save_circuit_breaker_state(
        self,
        consecutive_losing_days: int,
        reduced_sizing: bool,
        halted: bool,
        halt_reason: Optional[str] = None,
        halt_time: Optional[str] = None,
        last_recorded_day: Optional[str] = None,
        high_water_mark: Optional[float] = None,
    ):
        """Persist circuit breaker state (singleton row, id=1)."""
        now = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()
        conn.execute("""
            INSERT INTO circuit_breaker_state
                (id, consecutive_losing_days, reduced_sizing, halted,
                 halt_reason, halt_time, last_recorded_day, high_water_mark, last_updated)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                consecutive_losing_days=excluded.consecutive_losing_days,
                reduced_sizing=excluded.reduced_sizing,
                halted=excluded.halted,
                halt_reason=excluded.halt_reason,
                halt_time=excluded.halt_time,
                last_recorded_day=excluded.last_recorded_day,
                high_water_mark=excluded.high_water_mark,
                last_updated=excluded.last_updated
        """, (
            consecutive_losing_days,
            int(reduced_sizing),
            int(halted),
            halt_reason,
            halt_time,
            last_recorded_day,
            high_water_mark,
            now,
        ))
        conn.commit()

    def load_circuit_breaker_state(self) -> Optional[dict]:
        """Load persisted circuit breaker state. Returns None if no state saved."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM circuit_breaker_state WHERE id=1"
        ).fetchone()
        return dict(row) if row else None

    # ──────────────────────────────────────
    # Cooldowns
    # ──────────────────────────────────────

    def save_cooldown(self, market_id: str, exit_time: datetime, duration_seconds: int | None = None):
        """Persist a cooldown entry with optional duration (H-15)."""
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO cooldowns (market_id, exit_time, duration_seconds) VALUES (?, ?, ?)",
            (market_id, exit_time.isoformat(), duration_seconds),
        )
        conn.commit()

    def load_cooldowns(self, max_age_seconds: int = 3600) -> dict[str, datetime]:
        """Load valid cooldowns, deleting expired ones.

        Also returns durations so RiskEngine can restore loss-specific cooldowns
        after restart (H-15).

        Args:
            max_age_seconds: Maximum cooldown age in seconds (used when no
                per-entry duration is stored).

        Returns:
            Dict of market_id -> exit_time for still-active cooldowns.
        """
        conn = self._get_conn()
        rows = conn.execute("SELECT market_id, exit_time, duration_seconds FROM cooldowns").fetchall()
        now = datetime.now(timezone.utc)
        active: dict[str, datetime] = {}
        expired: list[str] = []

        for row in rows:
            try:
                exit_time = datetime.fromisoformat(row["exit_time"])
            except (ValueError, TypeError):
                logger.warning(f"Invalid cooldown datetime for {row['market_id']}: {row['exit_time']!r} — removing")
                expired.append(row["market_id"])
                continue
            if exit_time.tzinfo is None:
                exit_time = exit_time.replace(tzinfo=timezone.utc)
            duration = row["duration_seconds"] if row["duration_seconds"] is not None else max_age_seconds
            if (now - exit_time).total_seconds() < duration:
                active[row["market_id"]] = exit_time
            else:
                expired.append(row["market_id"])

        # Clean up expired
        if expired:
            conn.executemany(
                "DELETE FROM cooldowns WHERE market_id=?",
                [(m,) for m in expired],
            )
            conn.commit()

        return active

    def load_cooldown_durations(self) -> dict[str, int]:
        """Load persisted cooldown durations (H-15)."""
        conn = self._get_conn()
        rows = conn.execute("SELECT market_id, duration_seconds FROM cooldowns WHERE duration_seconds IS NOT NULL").fetchall()
        return {row["market_id"]: row["duration_seconds"] for row in rows}

    def delete_cooldown(self, market_id: str):
        """Remove a cooldown entry."""
        conn = self._get_conn()
        conn.execute("DELETE FROM cooldowns WHERE market_id=?", (market_id,))
        conn.commit()

    # ──────────────────────────────────────
    # Key-value settings (for runtime state persistence)
    # ──────────────────────────────────────

    def save_setting(self, key: str, value: str) -> None:
        """Persist a key-value setting (e.g., live bankroll)."""
        conn = self._get_conn()
        conn.execute(
            "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        )
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )
        conn.commit()

    def load_setting(self, key: str) -> str | None:
        """Load a persisted setting by key. Returns None if not found."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else None
        except Exception as e:
            # M-1: Log rather than silently swallowing -- aids debugging of
            # schema mismatches, lock contention, and corruption.
            logger.debug(f"load_setting('{key}') failed (table may not exist yet): {e}")
            return None

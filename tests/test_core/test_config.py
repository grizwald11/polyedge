"""Tests for config loading — validates YAML parsing, defaults, and error handling."""

from __future__ import annotations

import os
import tempfile

import pytest

from src.config import Settings, load_settings


class TestLoadSettings:
    def test_loads_defaults_when_no_yaml(self, tmp_path):
        """Missing YAML file should use all defaults."""
        settings = load_settings(tmp_path / "nonexistent.yaml")
        assert settings.trading.bankroll == 500.0
        assert settings.trading.mode == "paper"
        assert settings.kalshi.use_demo is True

    def test_loads_valid_yaml(self, tmp_path):
        yaml_file = tmp_path / "settings.yaml"
        yaml_file.write_text(
            "trading:\n"
            "  bankroll: 1000.0\n"
            "  mode: live\n"
            "  kelly_fraction: 0.25\n"
        )
        settings = load_settings(yaml_file)
        assert settings.trading.bankroll == 1000.0
        assert settings.trading.mode == "live"
        assert settings.trading.kelly_fraction == 0.25

    def test_malformed_yaml_raises(self, tmp_path):
        """Malformed YAML should raise RuntimeError, not silently use defaults."""
        yaml_file = tmp_path / "bad.yaml"
        yaml_file.write_text("trading:\n  bankroll: [invalid\n  unclosed bracket")
        with pytest.raises(RuntimeError, match="Failed to parse config"):
            load_settings(yaml_file)

    def test_empty_yaml_uses_defaults(self, tmp_path):
        yaml_file = tmp_path / "empty.yaml"
        yaml_file.write_text("")
        settings = load_settings(yaml_file)
        assert settings.trading.bankroll == 500.0

    def test_env_vars_overlay(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KALSHI_API_KEY_ID", "test-key-123")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("POLYEDGE_LIVE_ENABLED", "true")

        settings = load_settings(tmp_path / "nonexistent.yaml")
        assert settings.kalshi_api_key_id == "test-key-123"
        assert settings.anthropic_api_key == "sk-ant-test"
        assert settings.live_enabled is True

    def test_live_enabled_default_false(self, tmp_path, monkeypatch):
        monkeypatch.delenv("POLYEDGE_LIVE_ENABLED", raising=False)
        settings = load_settings(tmp_path / "nonexistent.yaml")
        assert settings.live_enabled is False


class TestSettingsValidation:
    def test_bankroll_must_be_positive(self):
        with pytest.raises(ValueError, match="bankroll must be > 0"):
            Settings(trading={"bankroll": 0})

    def test_kelly_fraction_range(self):
        with pytest.raises(ValueError, match="kelly_fraction"):
            Settings(trading={"kelly_fraction": 0})
        with pytest.raises(ValueError, match="kelly_fraction"):
            Settings(trading={"kelly_fraction": 1.5})

    def test_pct_fields_range(self):
        with pytest.raises(ValueError, match="max_position_pct"):
            Settings(trading={"max_position_pct": 0})
        with pytest.raises(ValueError, match="daily_loss_limit_pct"):
            Settings(trading={"daily_loss_limit_pct": 1.5})

    def test_valid_settings_accepted(self):
        s = Settings(trading={"bankroll": 100, "kelly_fraction": 0.5})
        assert s.trading.bankroll == 100
        assert s.trading.kelly_fraction == 0.5


class TestMinEdgeValidation:
    def test_negative_min_edge_rejected(self):
        with pytest.raises(ValueError, match="min_edge"):
            Settings(trading={"min_edge_ai": -0.05})

    def test_zero_min_edge_accepted(self):
        s = Settings(trading={"min_edge_ai": 0.0})
        assert s.trading.min_edge_ai == 0.0

    def test_valid_min_edge_accepted(self):
        s = Settings(trading={"min_edge_arb": 0.03})
        assert s.trading.min_edge_arb == 0.03


class TestTradingModeValidation:
    def test_paper_accepted(self):
        s = Settings(trading={"bankroll": 100, "mode": "paper"})
        assert s.trading.mode == "paper"

    def test_live_accepted(self):
        s = Settings(trading={"bankroll": 100, "mode": "live"})
        assert s.trading.mode == "live"

    def test_invalid_mode_rejected(self):
        with pytest.raises(ValueError, match="mode"):
            Settings(trading={"bankroll": 100, "mode": "LIVE"})

    def test_typo_mode_rejected(self):
        with pytest.raises(ValueError, match="mode"):
            Settings(trading={"bankroll": 100, "mode": "papers"})


class TestExecutionConfig:
    def test_defaults(self):
        s = Settings()
        assert s.execution.stale_order_age_seconds == 1800
        assert s.execution.order_poll_timeout_seconds == 10
        assert s.execution.order_poll_delay_seconds == 2.0
        assert s.execution.max_poll_attempts == 5
        assert s.execution.cycle_timeout_seconds == 300

    def test_yaml_override(self, tmp_path):
        yaml_file = tmp_path / "settings.yaml"
        yaml_file.write_text(
            "execution:\n"
            "  stale_order_age_seconds: 900\n"
            "  cycle_timeout_seconds: 600\n"
            "  max_poll_attempts: 10\n"
        )
        s = load_settings(yaml_file)
        assert s.execution.stale_order_age_seconds == 900
        assert s.execution.cycle_timeout_seconds == 600
        assert s.execution.max_poll_attempts == 10
        # Defaults preserved for fields not overridden
        assert s.execution.order_poll_timeout_seconds == 10

    def test_snapshot_retention_days(self):
        s = Settings()
        assert s.database.snapshot_retention_days == 30


class TestValidateRequiredKeys:
    def test_warns_missing_anthropic_key(self):
        s = Settings()
        s.anthropic_api_key = None
        warnings = s.validate_required_keys()
        assert any("ANTHROPIC_API_KEY" in w for w in warnings)

    def test_warns_missing_kalshi_credentials(self):
        s = Settings()
        s.kalshi_api_key_id = None
        s.kalshi_private_key_path = None
        warnings = s.validate_required_keys()
        assert any("Kalshi API credentials" in w for w in warnings)

    def test_warns_nonexistent_key_path(self, tmp_path):
        s = Settings()
        s.kalshi_api_key_id = "test-key"
        s.kalshi_private_key_path = str(tmp_path / "nonexistent_key.pem")
        s.anthropic_api_key = "sk-test"
        warnings = s.validate_required_keys()
        assert any("does not exist" in w for w in warnings)

    def test_valid_key_path_no_warning(self, tmp_path):
        key_file = tmp_path / "valid_key.pem"
        key_file.write_text("fake-key-content")
        s = Settings()
        s.kalshi_api_key_id = "test-key"
        s.kalshi_private_key_path = str(key_file)
        s.anthropic_api_key = "sk-test"
        warnings = s.validate_required_keys()
        assert not any("does not exist" in w for w in warnings)
        assert not any("not readable" in w for w in warnings)

    def test_no_warnings_when_all_set(self, tmp_path):
        key_file = tmp_path / "valid_key.pem"
        key_file.write_text("fake-key-content")
        s = Settings()
        s.kalshi_api_key_id = "test-key"
        s.kalshi_private_key_path = str(key_file)
        s.anthropic_api_key = "sk-test"
        warnings = s.validate_required_keys()
        assert len(warnings) == 0

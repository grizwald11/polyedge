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
        with pytest.raises(ValueError, match="percentage"):
            Settings(trading={"max_position_pct": 0})
        with pytest.raises(ValueError, match="percentage"):
            Settings(trading={"daily_loss_limit_pct": 1.5})

    def test_valid_settings_accepted(self):
        s = Settings(trading={"bankroll": 100, "kelly_fraction": 0.5})
        assert s.trading.bankroll == 100
        assert s.trading.kelly_fraction == 0.5

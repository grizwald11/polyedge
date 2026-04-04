"""Tests for RSA private key loader (H-5).

Covers: valid PEM loading, missing file, empty path, passphrase-protected key,
permission check mode.
"""

from __future__ import annotations

import os
import stat
from unittest.mock import patch

import pytest

from src.core.key_loader import load_rsa_private_key


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _generate_test_pem() -> bytes:
    """Generate a fresh RSA private key for testing (not used for real auth)."""
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )


_VALID_PEM = _generate_test_pem()


def _write_key_file(path, content: bytes = _VALID_PEM, mode: int = 0o600):
    """Write a PEM file with specified permissions."""
    path.write_bytes(content)
    os.chmod(str(path), mode)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLoadRSAPrivateKey:
    def test_returns_none_for_empty_path(self):
        assert load_rsa_private_key("") is None

    def test_returns_none_for_missing_file(self, tmp_path):
        result = load_rsa_private_key(str(tmp_path / "nonexistent.pem"))
        assert result is None

    def test_loads_valid_pem(self, tmp_path):
        key_file = tmp_path / "test.pem"
        _write_key_file(key_file)
        key = load_rsa_private_key(str(key_file))
        assert key is not None

    def test_permission_check_fixes_permissive(self, tmp_path):
        key_file = tmp_path / "test.pem"
        _write_key_file(key_file, mode=0o644)
        key = load_rsa_private_key(str(key_file), check_permissions=True)
        assert key is not None
        actual_mode = os.stat(str(key_file)).st_mode & 0o777
        assert actual_mode == 0o600

    def test_permission_check_skipped_by_default(self, tmp_path):
        key_file = tmp_path / "test.pem"
        _write_key_file(key_file, mode=0o644)
        key = load_rsa_private_key(str(key_file), check_permissions=False)
        assert key is not None
        # Permissions should NOT have been changed
        actual_mode = os.stat(str(key_file)).st_mode & 0o777
        assert actual_mode == 0o644

    def test_returns_none_for_passphrase_protected(self, tmp_path):
        """Passphrase-protected keys should return None with a helpful log."""
        key_file = tmp_path / "encrypted.pem"
        _write_key_file(key_file)
        with patch(
            "cryptography.hazmat.primitives.serialization.load_pem_private_key",
            side_effect=TypeError("Password was not given but private key is encrypted"),
        ):
            result = load_rsa_private_key(str(key_file))
        assert result is None

    def test_returns_none_for_invalid_pem(self, tmp_path):
        key_file = tmp_path / "garbage.pem"
        key_file.write_bytes(b"not a valid PEM file")
        result = load_rsa_private_key(str(key_file))
        assert result is None

    def test_returns_none_for_value_error_encrypted(self, tmp_path):
        """Some cryptography versions raise ValueError for encrypted keys."""
        key_file = tmp_path / "encrypted.pem"
        _write_key_file(key_file)
        with patch(
            "cryptography.hazmat.primitives.serialization.load_pem_private_key",
            side_effect=ValueError("Could not deserialize key data: encrypted"),
        ):
            result = load_rsa_private_key(str(key_file))
        assert result is None

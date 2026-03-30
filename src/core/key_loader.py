"""Shared RSA private key loader for Kalshi API authentication.

Extracted from kalshi_client.py and websocket_client.py to eliminate
duplication (L-2). Both modules import load_rsa_private_key() from here.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def load_rsa_private_key(
    private_key_path: str,
    check_permissions: bool = False,
) -> Optional[Any]:
    """Load an RSA private key from a PEM file.

    Args:
        private_key_path: Filesystem path to the PEM-encoded private key.
        check_permissions: If True, verify and fix file permissions to 0o600.

    Returns:
        The loaded private key object, or None on failure.
    """
    if not private_key_path:
        return None

    try:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        if check_permissions:
            import os
            import stat
            key_stat = os.stat(private_key_path)
            mode = key_stat.st_mode & 0o777
            if mode & (stat.S_IRWXG | stat.S_IRWXO):
                try:
                    os.chmod(private_key_path, 0o600)
                    logger.warning(
                        f"Private key had permissive mode {oct(mode)} — fixed to 0o600. "
                        f"Review file security."
                    )
                except OSError as chmod_err:
                    raise RuntimeError(
                        f"Private key file {private_key_path} has insecure permissions "
                        f"{oct(mode)} and cannot be fixed: {chmod_err}. "
                        f"Manually run: chmod 600 {private_key_path}"
                    ) from chmod_err

        with open(private_key_path, "rb") as f:
            key = load_pem_private_key(f.read(), password=None)
        return key
    except RuntimeError:
        raise  # Re-raise permission/security errors
    except Exception as e:
        logger.error(f"Failed to load private key from {private_key_path}: {e}", exc_info=True)
        return None

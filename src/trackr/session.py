"""Helpers de session pour ré-auth automatique quand un token expire."""

from __future__ import annotations

from trackr.config import Config, save_config
from trackr.trackers import torr9 as torr9_api
from trackr.trackers.base import AuthError, TrackerError


def ensure_torr9_jwt(cfg: Config) -> str:
    """Retourne un JWT Torr9 valide. Tente un re-login si expiré et password connu."""
    if cfg.torr9_jwt_valid():
        return cfg.torr9_jwt
    if not (cfg.torr9_username and cfg.torr9_password):
        raise AuthError(
            "JWT Torr9 expiré et identifiants non mémorisés. "
            "Va dans Configuration → Identifiants Torr9 pour te reconnecter."
        )
    result = torr9_api.login(cfg.torr9_username, cfg.torr9_password)
    cfg.torr9_jwt = result.token
    cfg.torr9_jwt_expires_at = result.token_expires_at.isoformat() if result.token_expires_at else ""
    if not cfg.torr9_passkey:
        cfg.torr9_passkey = result.profile.passkey
    save_config(cfg)
    return cfg.torr9_jwt

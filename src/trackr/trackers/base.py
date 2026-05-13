from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


class TrackerError(RuntimeError):
    """Erreur de communication avec un tracker (réseau, auth, validation)."""


class AuthError(TrackerError):
    """Authentification refusée (mauvais credentials, token expiré)."""


class MFARequired(TrackerError):
    """Login partiel : un code OTP/TOTP est requis pour finaliser."""

    def __init__(self, methods: list[str]):
        super().__init__("MFA requis")
        self.methods = methods


@dataclass
class Profile:
    username: str = ""
    email: str = ""
    user_id: int = 0
    role: str = ""
    passkey: str = ""
    ratio: float | None = None
    uploaded_bytes: int = 0
    downloaded_bytes: int = 0
    bonus: int = 0
    permissions: list[str] = field(default_factory=list)


@dataclass
class AuthResult:
    profile: Profile
    token: str = ""
    token_expires_at: datetime | None = None
    session_cookie: str = ""
    session_expires_at: datetime | None = None
    api_key: str = ""
    api_key_provisioned: bool = False

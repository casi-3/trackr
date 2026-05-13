"""Vérification de la validité des accès stockés.

Au lancement de trackr, on ping rapidement chaque service configuré pour
détecter les credentials expirés/révoqués avant que l'user lance un upload.

Pour qBittorrent en mode Login : tentative de re-login automatique si le
cookie SID est expiré et que le password est mémorisé (la nouvelle valeur
est persistée dans le config).

Pour Torr9 : pareil avec le password mémorisé → re-login si JWT expiré.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from trackr import qbittorrent as qbt
from trackr.config import Config, save_config
from trackr.trackers import c411 as c411_api
from trackr.trackers import torr9 as torr9_api
from trackr.trackers.base import AuthError, TrackerError


class Status(str, Enum):
    OK = "ok"                # tout va bien, prêt à publier
    NOT_CONFIGURED = "off"   # pas configuré
    STALE = "stale"          # creds expirés / refusés
    ERROR = "error"          # réseau ou autre


@dataclass
class Check:
    service: str
    status: Status
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == Status.OK


def check_c411(cfg: Config) -> Check:
    if not cfg.is_c411_ready():
        return Check("C411", Status.NOT_CONFIGURED, "pas configuré")
    # Bearer (upload) : on ping /api/user/drafts
    try:
        c411_api.validate_api_key(cfg.c411_api_key)
    except AuthError as e:
        return Check("C411", Status.STALE, str(e))
    except TrackerError as e:
        return Check("C411", Status.ERROR, str(e))
    # Session web (delete) : optionnelle, on signale si expirée
    detail = ""
    if cfg.c411_session and not cfg.c411_session_valid():
        detail = "session web expirée (delete indisponible)"
    return Check("C411", Status.OK, detail or "OK")


def check_torr9(cfg: Config) -> Check:
    if not cfg.is_torr9_ready():
        return Check("Torr9", Status.NOT_CONFIGURED, "pas configuré")
    # Si JWT expiré et password connu : refresh auto
    if not cfg.torr9_jwt_valid():
        if cfg.torr9_username and cfg.torr9_password:
            try:
                result = torr9_api.login(cfg.torr9_username, cfg.torr9_password)
                cfg.torr9_jwt = result.token
                cfg.torr9_jwt_expires_at = (
                    result.token_expires_at.isoformat() if result.token_expires_at else ""
                )
                if not cfg.torr9_passkey:
                    cfg.torr9_passkey = result.profile.passkey
                save_config(cfg)
                return Check("Torr9", Status.OK, "JWT refresh auto")
            except AuthError as e:
                return Check("Torr9", Status.STALE, f"refresh refusé : {e}")
            except TrackerError as e:
                return Check("Torr9", Status.ERROR, str(e))
        return Check("Torr9", Status.STALE, "JWT expiré (password non mémorisé)")
    # JWT en théorie valide → on vérifie qu'il marche encore (révocation possible)
    try:
        torr9_api.fetch_profile(cfg.torr9_jwt)
    except AuthError as e:
        return Check("Torr9", Status.STALE, str(e))
    except TrackerError as e:
        return Check("Torr9", Status.ERROR, str(e))
    return Check("Torr9", Status.OK, "OK")


def check_qbittorrent(cfg: Config) -> Check:
    if not cfg.qbt_url or not cfg.qbt_auth_mode:
        return Check("qBittorrent", Status.NOT_CONFIGURED, "pas configuré")

    if cfg.qbt_auth_mode == "api_key":
        if not cfg.qbt_api_key:
            return Check("qBittorrent", Status.NOT_CONFIGURED, "clé manquante")
        try:
            ident = qbt.whoami(cfg.qbt_url, api_key=cfg.qbt_api_key)
        except qbt.QbtAuthError as e:
            return Check("qBittorrent", Status.STALE, str(e))
        except qbt.QbtError as e:
            return Check("qBittorrent", Status.ERROR, str(e))
        return Check("qBittorrent", Status.OK, f"v{ident.app_version} (API key)")

    if cfg.qbt_auth_mode == "login":
        if not (cfg.qbt_username and cfg.qbt_password):
            return Check("qBittorrent", Status.NOT_CONFIGURED, "creds manquants")
        # Essai SID en cache d'abord
        if cfg.qbt_sid_cookie:
            try:
                ident = qbt.whoami(cfg.qbt_url, sid=cfg.qbt_sid_cookie)
                return Check("qBittorrent", Status.OK, f"v{ident.app_version}")
            except qbt.QbtAuthError:
                pass  # SID expiré, on retente avec un login
            except qbt.QbtError as e:
                return Check("qBittorrent", Status.ERROR, str(e))
        # Re-login auto
        try:
            sid = qbt.login(cfg.qbt_url, cfg.qbt_username, cfg.qbt_password)
            ident = qbt.whoami(cfg.qbt_url, sid=sid)
            cfg.qbt_sid_cookie = sid
            save_config(cfg)
        except qbt.QbtAuthError as e:
            return Check("qBittorrent", Status.STALE, str(e))
        except qbt.QbtError as e:
            return Check("qBittorrent", Status.ERROR, str(e))
        return Check("qBittorrent", Status.OK, f"v{ident.app_version} (SID refresh auto)")

    return Check("qBittorrent", Status.NOT_CONFIGURED, "mode auth invalide")


def run_all(cfg: Config) -> list[Check]:
    return [check_c411(cfg), check_torr9(cfg), check_qbittorrent(cfg)]

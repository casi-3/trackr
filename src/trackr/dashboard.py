"""Récupération des données pour le dashboard d'accueil.

Fetch parallèle (ThreadPoolExecutor) avec cache mémoire 60s pour ne pas
ralentir chaque retour à la home. Chaque section est indépendante : si un
tracker rame ou est down, les autres s'affichent quand même.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from trackr import pending as pending_mod
from trackr import qbittorrent as qbt
from trackr.config import Config
from trackr.trackers import c411 as c411_api
from trackr.trackers import torr9 as torr9_api
from trackr.trackers.base import AuthError, TrackerError

CACHE_TTL_SECONDS = 60.0


@dataclass
class TrackerSnapshot:
    """Vue par tracker (C411 / Torr9)."""

    name: str
    configured: bool = False
    error: str = ""
    # Profile
    username: str = ""
    ratio: float | None = None
    uploaded_bytes: int = 0
    bonus: int = 0
    bonus_unit: str = "pts"  # "pts" pour C411 (points), "bytes" pour Torr9 (traffic bonus)
    role: str = ""
    # Uploads (liste normalisée)
    uploads: list[dict] = field(default_factory=list)
    # Rejets (uniquement C411 pour l'instant : torrent_revision_requested)
    rejections: list = field(default_factory=list)  # list[c411_api.Rejection]

    @property
    def pending(self) -> list[dict]:
        return [u for u in self.uploads if (u.get("status") or "").lower() == "pending"]

    @property
    def active(self) -> list[dict]:
        return [u for u in self.uploads if (u.get("status") or "").lower() in ("active", "approved")]


@dataclass
class QbtSnapshot:
    configured: bool = False
    error: str = ""
    app_version: str = ""
    torrents_count: int = 0
    seeding_count: int = 0
    total_upload_bytes: int = 0


@dataclass
class Dashboard:
    c411: TrackerSnapshot
    torr9: TrackerSnapshot
    qbt: QbtSnapshot
    fetched_at: float = 0.0


_cache: Dashboard | None = None
_cached_at: float = 0.0


def merge_recent(dash: "Dashboard", limit: int = 8) -> list[tuple[str, dict]]:
    """Fusionne les uploads de tous les trackers configurés, triés par date desc.

    Retourne `[(tracker_name, upload_dict), ...]` — chaque dict est déjà normalisé
    (cf. `_normalize_c411_upload` / `_normalize_torr9_upload`).
    """
    merged: list[tuple[str, dict]] = []
    for snap in (dash.c411, dash.torr9):
        for u in snap.uploads:
            merged.append((snap.name, u))
    # Tri par created_at desc — les ISO sont comparables alphabétiquement
    merged.sort(key=lambda x: x[1].get("created_at") or "", reverse=True)
    return merged[:limit]


def _normalize_c411_upload(item: dict) -> dict:
    """Aplati une entry C411 en {title, status, info_hash, created_at}."""
    return {
        "title": item.get("title") or item.get("name") or "",
        "status": (item.get("status") or "").lower(),
        "info_hash": item.get("infoHash") or item.get("info_hash") or "",
        "created_at": item.get("createdAt") or item.get("created_at") or "",
        "id": item.get("id") or 0,
    }


def _normalize_torr9_upload(item: dict) -> dict:
    return {
        "title": item.get("title") or "",
        "status": (item.get("status") or "").lower(),
        "info_hash": item.get("info_hash") or "",
        "created_at": item.get("upload_date") or item.get("created_at") or "",
        "age": item.get("age") or "",
        "id": item.get("id") or 0,
    }


def _fetch_c411(cfg: Config) -> TrackerSnapshot:
    snap = TrackerSnapshot(name="C411", configured=cfg.is_c411_ready())
    if not snap.configured:
        return snap
    snap.username = cfg.c411_username
    snap.bonus_unit = "pts"
    # Profil étendu (ratio, bonus) seulement si session web encore valide
    if cfg.c411_session and cfg.c411_session_valid():
        try:
            cookies = {c411_api.SESSION_COOKIE_NAME: cfg.c411_session}
            prof = c411_api.fetch_profile(cookies)
            snap.username = prof.username or snap.username
            snap.ratio = prof.ratio
            snap.uploaded_bytes = prof.uploaded_bytes
            snap.bonus = prof.bonus
            snap.role = prof.role
        except (AuthError, TrackerError):
            pass  # best-effort, on garde ce qu'on a
    # Uploads (Bearer marche)
    try:
        items = c411_api.list_my_uploads(cfg.c411_api_key, snap.username, limit=20)
        snap.uploads = [_normalize_c411_upload(i) for i in items]
    except (AuthError, TrackerError) as e:
        snap.error = str(e)
    # Rejets (= notifications torrent_revision_requested). Session web requise.
    if cfg.c411_session and cfg.c411_session_valid():
        try:
            snap.rejections = c411_api.list_rejections(cfg.c411_session)
        except (AuthError, TrackerError):
            pass  # best-effort
    # Pending validation : poll les info_hash tracked localement.
    # list_my_uploads ne renvoie que les approved → on garde la liste à jour ici.
    _resolve_c411_pending(cfg, snap)
    return snap


def _resolve_c411_pending(cfg: Config, snap: TrackerSnapshot) -> None:
    """Pour chaque pending tracked localement, ping le serveur et nettoie si validé."""
    pendings = pending_mod.list_for("c411")
    if not pendings:
        return
    still_pending: list[dict] = []
    for p in pendings:
        try:
            t = c411_api.fetch_torrent(cfg.c411_session, p.info_hash)
            status = (t.get("status") or "").lower()
            if status in ("approved", "active"):
                pending_mod.remove(p.info_hash)  # désormais visible via list_my_uploads
                continue
            if status == "revision_requested":
                pending_mod.remove(p.info_hash)  # bascule dans le système de rejets
                continue
            # status == "pending" ou autre → toujours en attente
            still_pending.append({
                "title": p.title,
                "status": "pending",
                "info_hash": p.info_hash,
                "created_at": p.posted_at,
                "id": 0,
            })
        except TrackerError:
            # 404 = pas encore visible côté tracker → toujours pending
            still_pending.append({
                "title": p.title,
                "status": "pending",
                "info_hash": p.info_hash,
                "created_at": p.posted_at,
                "id": 0,
            })
        except AuthError:
            # Session expirée → on ne touche pas au cache local
            still_pending.append({
                "title": p.title,
                "status": "pending",
                "info_hash": p.info_hash,
                "created_at": p.posted_at,
                "id": 0,
            })
    # Préfixe la liste uploads : pending d'abord (plus pertinent pour l'user).
    snap.uploads = still_pending + snap.uploads


def _fetch_torr9(cfg: Config) -> TrackerSnapshot:
    snap = TrackerSnapshot(name="Torr9", configured=cfg.is_torr9_ready())
    if not snap.configured:
        return snap
    if not cfg.torr9_jwt_valid():
        snap.error = "JWT expiré"
        return snap
    snap.username = cfg.torr9_username
    snap.bonus_unit = "bytes"
    try:
        prof = torr9_api.fetch_profile(cfg.torr9_jwt)
        snap.username = prof.username or snap.username
        snap.ratio = prof.ratio
        snap.uploaded_bytes = prof.uploaded_bytes
        snap.bonus = prof.bonus
        snap.role = prof.role
    except (AuthError, TrackerError) as e:
        snap.error = str(e)
        return snap
    try:
        items = torr9_api.list_my_uploads(cfg.torr9_jwt, limit=20)
        snap.uploads = [_normalize_torr9_upload(i) for i in items]
    except (AuthError, TrackerError) as e:
        snap.error = str(e)
    return snap


def _has_trackr_tag(torrent: dict) -> bool:
    """Match les tags `trackr-<TRACKER>` (multi-tags ajoutés au moment du seed)."""
    raw = torrent.get("tags") or ""
    parts = [t.strip() for t in raw.split(",") if t.strip()]
    return any(t.startswith("trackr-") or t == "trackr" for t in parts)


def _fetch_qbt(cfg: Config) -> QbtSnapshot:
    snap = QbtSnapshot(configured=cfg.is_qbt_ready())
    if not snap.configured:
        return snap
    try:
        if cfg.qbt_auth_mode == "api_key":
            ident = qbt.whoami(cfg.qbt_url, api_key=cfg.qbt_api_key)
            all_torrents = qbt.list_torrents(cfg.qbt_url, api_key=cfg.qbt_api_key)
        else:
            sid = cfg.qbt_sid_cookie
            if not sid and cfg.qbt_username and cfg.qbt_password:
                sid = qbt.login(cfg.qbt_url, cfg.qbt_username, cfg.qbt_password)
            ident = qbt.whoami(cfg.qbt_url, sid=sid)
            all_torrents = qbt.list_torrents(cfg.qbt_url, sid=sid)
    except (qbt.QbtAuthError, qbt.QbtError) as e:
        snap.error = str(e)
        return snap
    torrents = [t for t in all_torrents if _has_trackr_tag(t)]
    snap.app_version = ident.app_version
    snap.torrents_count = len(torrents)
    snap.seeding_count = sum(
        1 for t in torrents if (t.get("state") or "").lower() in ("uploading", "stalledup", "queuedup", "forcedup")
    )
    snap.total_upload_bytes = sum(int(t.get("uploaded") or 0) for t in torrents)
    return snap


def get(cfg: Config, *, force_refresh: bool = False) -> Dashboard:
    """Récupère le dashboard, depuis cache si frais (sauf force_refresh)."""
    global _cache, _cached_at
    now = time.time()
    if not force_refresh and _cache and (now - _cached_at) < CACHE_TTL_SECONDS:
        return _cache

    c411 = TrackerSnapshot(name="C411")
    torr9 = TrackerSnapshot(name="Torr9")
    qbt_snap = QbtSnapshot()

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            pool.submit(_fetch_c411, cfg): "c411",
            pool.submit(_fetch_torr9, cfg): "torr9",
            pool.submit(_fetch_qbt, cfg): "qbt",
        }
        for fut in as_completed(futures):
            kind = futures[fut]
            try:
                result = fut.result(timeout=10)
            except Exception as e:  # noqa: BLE001 — protection ultime, on dégrade pas la home
                if kind == "c411":
                    c411.error = f"fetch failed: {e}"
                elif kind == "torr9":
                    torr9.error = f"fetch failed: {e}"
                else:
                    qbt_snap.error = f"fetch failed: {e}"
                continue
            if kind == "c411":
                c411 = result
            elif kind == "torr9":
                torr9 = result
            else:
                qbt_snap = result

    dash = Dashboard(c411=c411, torr9=torr9, qbt=qbt_snap, fetched_at=now)
    _cache = dash
    _cached_at = now
    return dash


def invalidate() -> None:
    """Force le prochain get() à re-fetch."""
    global _cache, _cached_at
    _cache = None
    _cached_at = 0.0

"""Tracker local des uploads C411 en attente de validation.

`list_my_uploads` ne retourne que les torrents approuvés côté C411. Pour
voir les pending et les afficher dans le dashboard, on stocke localement
les uploads tout juste postés (info_hash + titre + date) et on les poll
un par un via `GET /api/torrents/{hash}` à chaque refresh.

Cycle de vie d'un info_hash dans ce fichier :
- ajouté par `add()` lors d'un POST réussi (mode upload normal) ou d'un
  resubmit réussi après rejet.
- supprimé par le dashboard quand le tracker confirme `approved`/`active`
  (le torrent passera désormais dans `list_my_uploads`) ou `revision_requested`
  (géré par le flow de rejection).
- supprimé manuellement par l'user (via menu si besoin) sinon.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from platformdirs import user_cache_dir

_PENDING_PATH = Path(user_cache_dir("trackr")) / "pending_uploads.json"


@dataclass
class PendingUpload:
    tracker: str       # "c411"
    info_hash: str
    title: str
    posted_at: str     # ISO UTC

    @classmethod
    def from_dict(cls, d: dict) -> "PendingUpload":
        return cls(
            tracker=str(d.get("tracker") or ""),
            info_hash=str(d.get("info_hash") or ""),
            title=str(d.get("title") or ""),
            posted_at=str(d.get("posted_at") or ""),
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load() -> list[PendingUpload]:
    if not _PENDING_PATH.exists():
        return []
    try:
        raw = json.loads(_PENDING_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(raw, list):
        return []
    return [PendingUpload.from_dict(d) for d in raw if isinstance(d, dict)]


def _save(items: list[PendingUpload]) -> None:
    _PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    _PENDING_PATH.write_text(
        json.dumps([asdict(i) for i in items], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def add(tracker: str, info_hash: str, title: str) -> None:
    """Enregistre (ou met à jour) un upload en attente. No-op si info_hash vide."""
    if not info_hash:
        return
    items = [p for p in _load() if p.info_hash != info_hash]
    items.append(PendingUpload(tracker=tracker, info_hash=info_hash, title=title, posted_at=_now_iso()))
    _save(items)


def remove(info_hash: str) -> None:
    if not info_hash:
        return
    items = [p for p in _load() if p.info_hash != info_hash]
    _save(items)


def list_for(tracker: str) -> list[PendingUpload]:
    return [p for p in _load() if p.tracker == tracker]


def list_all() -> list[PendingUpload]:
    return _load()

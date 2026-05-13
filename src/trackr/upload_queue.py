"""File de reprise pour les uploads partiellement échoués.

Quand un upload échoue sur un tracker mais pas sur les autres, on garde l'état
suffisant pour retenter **uniquement** le ou les trackers en erreur, sans
régénérer le .torrent, le NFO ou la description.

Persistance JSON dans `user_cache_dir/trackr/queue/<job_id>.json` — survit
aux redémarrages, lisible à l'œil.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from platformdirs import user_cache_dir

QUEUE_VERSION = 1
QUEUE_SUBDIR = "queue"


def _queue_dir() -> Path:
    p = Path(user_cache_dir("trackr")) / QUEUE_SUBDIR
    p.mkdir(parents=True, exist_ok=True)
    return p


@dataclass
class TrackerJob:
    """État d'un tracker au sein d'un upload."""

    name: str  # "c411" / "torr9" / etc.
    title: str
    announce_url: str
    source_tag: str
    category_id: int
    category_name: str
    subcategory_id: int
    subcategory_name: str
    options: dict = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    torrent_path: str = ""  # local .torrent
    tracker_torrent_path: str = ""  # version re-signée (si récupérée)
    info_hash: str = ""
    piece_size: int = 0
    piece_count: int = 0
    total_size: int = 0
    status: str = "pending"  # "pending" | "ok" | "failed" | "rolled_back"
    last_error: str = ""
    last_attempt_at: str = ""
    url_hint: str = ""
    torrent_id: int = 0


@dataclass
class UploadJob:
    """Un upload (potentiellement multi-trackers), serializable JSON."""

    id: str
    created_at: str
    updated_at: str
    source_file: str
    nfo_path: str
    description_path: str
    manifest_path: str
    release_title: str
    # TMDB / catégorie source data (utile pour le retry sans relire le manifest)
    tmdb_id: int = 0
    tmdb_type: str = "movie"
    tmdb_title: str = ""
    tmdb_year: str = ""
    media_type: str = "movie"  # "movie" / "series" / ...
    # État par tracker
    trackers: list[TrackerJob] = field(default_factory=list)
    version: int = QUEUE_VERSION

    @property
    def failed_trackers(self) -> list[TrackerJob]:
        return [t for t in self.trackers if t.status == "failed"]

    @property
    def ok_trackers(self) -> list[TrackerJob]:
        return [t for t in self.trackers if t.status == "ok"]

    @property
    def is_done(self) -> bool:
        """Tous les trackers OK (ou rolled_back) → plus rien à retenter."""
        return all(t.status in ("ok", "rolled_back") for t in self.trackers)


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _job_path(job_id: str) -> Path:
    return _queue_dir() / f"{job_id}.json"


def save(job: UploadJob) -> Path:
    job.updated_at = now_iso()
    path = _job_path(job.id)
    path.write_text(
        json.dumps(asdict(job), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def _from_dict(data: dict) -> UploadJob:
    trackers = [TrackerJob(**t) for t in data.get("trackers", [])]
    fields_ok = {k: v for k, v in data.items() if k != "trackers"}
    return UploadJob(trackers=trackers, **fields_ok)


def load(job_id: str) -> UploadJob | None:
    path = _job_path(job_id)
    if not path.exists():
        return None
    try:
        return _from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def remove(job_id: str) -> None:
    path = _job_path(job_id)
    if path.exists():
        path.unlink()


def list_jobs() -> list[UploadJob]:
    """Tous les jobs valides triés par updated_at desc (plus récents en premier)."""
    out: list[UploadJob] = []
    for p in _queue_dir().glob("*.json"):
        try:
            out.append(_from_dict(json.loads(p.read_text(encoding="utf-8"))))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    out.sort(key=lambda j: j.updated_at or j.created_at, reverse=True)
    return out


def pending_count() -> int:
    """Nombre de jobs ayant au moins un tracker en `failed`."""
    return sum(1 for j in list_jobs() if j.failed_trackers)

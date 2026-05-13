"""Recherche de métadonnées TMDB via les proxys C411 (riche) ou Torr9 (basique).

- C411 `/api/tmdb/search` : renvoie poster, synopsis, **genreIds** (IDs TMDB
  standards), réalisateurs, casting, etc. Nécessite la session web.
- Torr9 `/api/v1/torrents/search-media` : renvoie titre/année/note/synopsis,
  sans les genres. Nécessite le JWT.

`search()` tente C411 d'abord (plus riche) puis tombe sur Torr9.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx

from trackr.http import make_client
from trackr.trackers.base import AuthError, TrackerError
from trackr.trackers.torr9 import API_BASE as TORR9_API_BASE

C411_WEB_BASE = "https://c411.org"


@dataclass
class MediaHit:
    tmdb_id: int
    title: str
    year: str
    description: str
    poster_url: str
    rating: float
    media_type: str  # "movie" or "tv"
    genre_ids: list[int] = field(default_factory=list)  # IDs TMDB standards


def _to_int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _to_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _search_torr9(query: str, jwt: str, *, category: str, limit: int) -> list[MediaHit]:
    try:
        with make_client(base_url=TORR9_API_BASE) as client:
            resp = client.post(
                "/api/v1/torrents/search-media",
                json={"query": query, "category": category},
                headers={"Authorization": f"Bearer {jwt}"},
            )
    except httpx.HTTPError as e:
        raise TrackerError(f"Recherche TMDB : connexion Torr9 impossible : {e}") from e
    if resp.status_code in (401, 403):
        raise AuthError("Recherche TMDB : JWT Torr9 refusé (expiré ?)")
    if resp.status_code >= 400:
        raise TrackerError(f"Recherche TMDB : HTTP {resp.status_code}")
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError) as e:
        raise TrackerError(f"Recherche TMDB : JSON invalide ({e})") from e
    results = data.get("results") or []
    return [
        MediaHit(
            tmdb_id=_to_int(r.get("id")),
            title=r.get("title") or "",
            year=str(r.get("year") or ""),
            description=r.get("description") or "",
            poster_url=r.get("poster_url") or "",
            rating=_to_float(r.get("rating")),
            media_type=r.get("media_type") or "",
        )
        for r in results[:limit]
    ]


def _search_c411(query: str, session_cookie: str, *, category: str, limit: int) -> list[MediaHit]:
    """C411 /api/tmdb/search — réponse riche (genreIds, casting, etc.).

    Session web requise. `category` mappe vers TMDB type (film → movie, tv → tv).
    """
    media_type = "movie" if category in ("film", "movie", "film-vo") else "tv"
    cookies = {"__Host-c411_session": session_cookie}
    try:
        with make_client(base_url=C411_WEB_BASE, cookies=cookies) as client:
            resp = client.get("/api/tmdb/search", params={"q": query, "type": media_type})
    except httpx.HTTPError as e:
        raise TrackerError(f"Recherche TMDB : connexion C411 impossible : {e}") from e
    if resp.status_code in (401, 403):
        raise AuthError("Recherche TMDB : session C411 expirée")
    if resp.status_code >= 400:
        raise TrackerError(f"Recherche TMDB : HTTP {resp.status_code}")
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError) as e:
        raise TrackerError(f"Recherche TMDB : JSON invalide ({e})") from e
    results = (data.get("data") or {}).get("results") or []
    out: list[MediaHit] = []
    for r in results[:limit]:
        release = r.get("releaseDate") or ""
        year = str(r.get("year") or "") or (release[:4] if release else "")
        # genreIds : list[int] | genres : list de noms ou objects {id,name}
        genre_ids = list(r.get("genreIds") or [])
        if not genre_ids:
            for g in r.get("genres") or []:
                if isinstance(g, dict) and g.get("id"):
                    genre_ids.append(int(g["id"]))
        out.append(
            MediaHit(
                tmdb_id=_to_int(r.get("id")),
                title=r.get("title") or r.get("originalTitle") or "",
                year=year,
                description=r.get("overview") or r.get("description") or "",
                poster_url=r.get("posterUrl") or r.get("poster_url") or "",
                rating=_to_float(r.get("rating")),
                media_type=r.get("type") or media_type,
                genre_ids=genre_ids,
            )
        )
    return out


def search(
    query: str,
    *,
    c411_session: str = "",
    torr9_jwt: str = "",
    category: str = "film",
    limit: int = 10,
) -> list[MediaHit]:
    """Recherche TMDB. C411 prioritaire (renvoie les genres), Torr9 en fallback."""
    last_err: Exception | None = None
    if c411_session:
        try:
            hits = _search_c411(query, c411_session, category=category, limit=limit)
            if hits:
                return hits
        except (AuthError, TrackerError) as e:
            last_err = e
    if torr9_jwt:
        try:
            return _search_torr9(query, torr9_jwt, category=category, limit=limit)
        except (AuthError, TrackerError) as e:
            last_err = e
    if last_err:
        raise last_err
    raise AuthError("Recherche TMDB : ni C411 ni Torr9 disponible")


def lookup_by_id(
    tmdb_id: int,
    *,
    c411_session: str = "",
    torr9_jwt: str = "",
    category: str = "film",
) -> MediaHit | None:
    hits = search(
        str(tmdb_id),
        c411_session=c411_session,
        torr9_jwt=torr9_jwt,
        category=category,
        limit=10,
    )
    for h in hits:
        if h.tmdb_id == tmdb_id:
            return h
    return hits[0] if hits else None

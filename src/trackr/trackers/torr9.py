from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

from trackr.http import make_client
from trackr.trackers.base import AuthError, AuthResult, Profile, TrackerError

API_BASE = "https://api.torr9.net"


def _decode_jwt_exp(token: str) -> datetime | None:
    try:
        _, payload_b64, _ = token.split(".")
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
        if isinstance(exp, (int, float)):
            return datetime.fromtimestamp(exp, tz=timezone.utc)
    except (ValueError, json.JSONDecodeError, KeyError):
        return None
    return None


def _profile_from_user(user: dict) -> Profile:
    uploaded = int(user.get("total_uploaded_bytes") or 0)
    downloaded = int(user.get("total_downloaded_bytes") or 0)
    ratio: float | None = None
    if downloaded > 0:
        ratio = round(uploaded / downloaded, 2)
    elif uploaded > 0:
        ratio = float("inf")
    return Profile(
        username=user.get("username") or "",
        email=user.get("email") or "",
        user_id=int(user.get("id") or 0),
        role=user.get("role") or "",
        passkey=user.get("passkey") or "",
        ratio=ratio,
        uploaded_bytes=uploaded,
        downloaded_bytes=downloaded,
        bonus=int(user.get("bonus_uploaded") or 0),
    )


def login(username: str, password: str) -> AuthResult:
    """Login Torr9 — retourne un AuthResult avec JWT, passkey et profil."""
    try:
        with make_client(base_url=API_BASE) as client:
            resp = client.post(
                "/api/v1/auth/login",
                json={"username": username, "password": password},
            )
    except httpx.HTTPError as e:
        raise TrackerError(f"Connexion Torr9 impossible : {e}") from e

    if resp.status_code in (400, 401):
        try:
            err = resp.json()
            msg = err.get("error") or err.get("message") or "Identifiants refusés"
        except (json.JSONDecodeError, ValueError):
            msg = "Identifiants refusés"
        raise AuthError(f"Torr9 : {msg}")

    if resp.status_code >= 500:
        raise TrackerError(f"Torr9 a renvoyé {resp.status_code} — réessaie plus tard")

    if resp.status_code != 200:
        raise TrackerError(f"Torr9 : réponse inattendue ({resp.status_code})")

    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError) as e:
        raise TrackerError(f"Torr9 : JSON invalide ({e})") from e

    token = data.get("token") or ""
    user = data.get("user") or {}
    if not token or not user:
        raise TrackerError("Torr9 : réponse de login incomplète (token/user manquant)")

    return AuthResult(
        profile=_profile_from_user(user),
        token=token,
        token_expires_at=_decode_jwt_exp(token),
    )


@dataclass
class UploadResult:
    info_hash: str
    status: str
    message: str
    announce_url: str
    download_url: str
    magnet_link: str
    raw: dict

    @property
    def torrent_id(self) -> int:
        """Extrait l'id numérique depuis download_url (`/api/v1/torrents/{id}/download`)."""
        import re

        if not self.download_url:
            return 0
        m = re.search(r"/torrents/(\d+)/", self.download_url)
        return int(m.group(1)) if m else 0


def delete_torrent(jwt: str, torrent_id: int) -> str:
    """DELETE /api/v1/torrents/{id}/delete — supprime un upload (uploader uniquement).

    Retourne le message du serveur en cas de succès.
    """
    if not torrent_id:
        raise TrackerError("Torr9 delete : id numérique manquant")
    try:
        with make_client(base_url=API_BASE) as client:
            resp = client.delete(
                f"/api/v1/torrents/{torrent_id}/delete",
                headers={"Authorization": f"Bearer {jwt}"},
            )
    except httpx.HTTPError as e:
        raise TrackerError(f"Connexion Torr9 impossible : {e}") from e
    if resp.status_code in (401, 403):
        raise AuthError("Torr9 : JWT refusé pour la suppression")
    if resp.status_code == 404:
        raise TrackerError("Torr9 : torrent introuvable (déjà supprimé ?)")
    if resp.status_code >= 400:
        try:
            err = resp.json()
            msg = err.get("error") or err.get("message") or f"HTTP {resp.status_code}"
        except (json.JSONDecodeError, ValueError):
            msg = f"HTTP {resp.status_code}"
        raise TrackerError(f"Torr9 delete : {msg}")
    try:
        data = resp.json()
        return str(data.get("message") or "Supprimé.")
    except (json.JSONDecodeError, ValueError):
        return "Supprimé."


def upload(
    jwt: str,
    *,
    torrent_path: Path,
    title: str,
    description: str,
    category: str,
    subcategory: str,
    nfo_text: str = "",
    tags: list[str] | None = None,
    is_exclusive: bool = False,
    is_anonymous: bool = False,
    tmdb_id: int = 0,
    imdb_id: str = "",
    tvdb_id: str = "",
    mal_id: str = "",
) -> UploadResult:
    """POST /api/v1/torrents/upload — multipart.

    ⚠️ `category` et `subcategory` doivent être les **noms d'affichage** (ex:
    'Films', 'Films d'animation'), pas les slugs.
    ⚠️ Le NFO est attendu en string text, pas en file part.
    """
    files = {
        "torrent_file": (torrent_path.name, torrent_path.read_bytes(), "application/x-bittorrent"),
    }
    data = {
        "title": title,
        "description": description,
        "category": category,
        "subcategory": subcategory,
        "is_exclusive": "true" if is_exclusive else "false",
        "is_anonymous": "true" if is_anonymous else "false",
    }
    if nfo_text:
        data["nfo"] = nfo_text
    if tags:
        data["tags"] = ", ".join(tags)
    if tmdb_id:
        data["tmdb_id"] = str(tmdb_id)
    if imdb_id:
        data["imdb_id"] = imdb_id
    if tvdb_id:
        data["tvdb_id"] = tvdb_id
    if mal_id:
        data["mal_id"] = mal_id

    try:
        with make_client(base_url=API_BASE) as client:
            resp = client.post(
                "/api/v1/torrents/upload",
                data=data,
                files=files,
                headers={"Authorization": f"Bearer {jwt}"},
            )
    except httpx.HTTPError as e:
        raise TrackerError(f"Connexion Torr9 impossible : {e}") from e

    if resp.status_code in (401, 403):
        raise AuthError("Torr9 upload : JWT invalide ou expiré")
    if resp.status_code >= 400:
        try:
            err = resp.json()
            msg = err.get("error") or err.get("message") or f"HTTP {resp.status_code}"
        except (json.JSONDecodeError, ValueError):
            msg = f"HTTP {resp.status_code}"
        raise TrackerError(f"Torr9 upload : {msg}")
    try:
        payload = resp.json()
    except (json.JSONDecodeError, ValueError) as e:
        raise TrackerError(f"Torr9 upload : JSON invalide ({e})") from e

    return UploadResult(
        info_hash=str(payload.get("info_hash") or ""),
        status=str(payload.get("status") or ""),
        message=str(payload.get("message") or ""),
        announce_url=str(payload.get("announce_url") or ""),
        download_url=str(payload.get("download_url") or ""),
        magnet_link=str(payload.get("magnet_link") or ""),
        raw=payload,
    )


def download_torrent(jwt: str, torrent_id: int, out_path: Path) -> Path:
    """GET /api/v1/torrents/{id}/download — retourne le .torrent signé par Torr9.

    Le serveur substitue l'announce avec ta passkey — c'est ce .torrent qu'il
    faut utiliser pour le seed.
    """
    if not torrent_id:
        raise TrackerError("Torr9 download : id manquant")
    try:
        with make_client(base_url=API_BASE) as client:
            resp = client.get(
                f"/api/v1/torrents/{torrent_id}/download",
                headers={"Authorization": f"Bearer {jwt}"},
            )
    except httpx.HTTPError as e:
        raise TrackerError(f"Connexion Torr9 impossible : {e}") from e
    if resp.status_code in (401, 403):
        raise AuthError("Torr9 download : JWT refusé")
    if resp.status_code == 404:
        raise TrackerError("Torr9 download : torrent introuvable")
    if resp.status_code != 200:
        raise TrackerError(f"Torr9 download : HTTP {resp.status_code}")
    content = resp.content
    if not content or content[:1] != b"d":
        raise TrackerError("Torr9 download : contenu ne ressemble pas à un .torrent")
    out_path.write_bytes(content)
    return out_path


def list_my_uploads(jwt: str, limit: int = 20) -> list[dict]:
    """GET /api/v1/torrents/my-uploads?limit=N — liste les uploads de l'user JWT.

    Chaque entry a `status` (`pending`/`active`/...), `title`, `id`, `info_hash`,
    `created_at`, `age`, etc.
    """
    try:
        with make_client(base_url=API_BASE) as client:
            resp = client.get(
                "/api/v1/torrents/my-uploads",
                params={"limit": str(limit)},
                headers={"Authorization": f"Bearer {jwt}"},
            )
    except httpx.HTTPError as e:
        raise TrackerError(f"Connexion Torr9 impossible : {e}") from e
    if resp.status_code in (401, 403):
        raise AuthError("Torr9 : JWT refusé")
    if resp.status_code != 200:
        raise TrackerError(f"Torr9 /my-uploads : HTTP {resp.status_code}")
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError) as e:
        raise TrackerError(f"Torr9 : JSON my-uploads invalide ({e})") from e
    items = data.get("torrents") if isinstance(data, dict) else data
    return items if isinstance(items, list) else []


def fetch_profile(jwt: str) -> Profile:
    """Récupère /api/v1/users/me avec un JWT existant."""
    try:
        with make_client(base_url=API_BASE) as client:
            resp = client.get(
                "/api/v1/users/me",
                headers={"Authorization": f"Bearer {jwt}"},
            )
    except httpx.HTTPError as e:
        raise TrackerError(f"Connexion Torr9 impossible : {e}") from e

    if resp.status_code in (401, 403):
        raise AuthError("Torr9 : JWT invalide ou expiré")
    if resp.status_code != 200:
        raise TrackerError(f"Torr9 : /users/me a renvoyé {resp.status_code}")

    try:
        return _profile_from_user(resp.json())
    except (json.JSONDecodeError, ValueError) as e:
        raise TrackerError(f"Torr9 : JSON profile invalide ({e})") from e

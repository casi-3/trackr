"""Client minimal pour l'API WebUI de qBittorrent.

Deux modes d'auth :
- **API Key** (qBittorrent ≥ 5.2.0, WebAPI ≥ 2.14.1) :
  `Authorization: Bearer qbt_xxx`. Stateless, pas de cookie.
- **Login** (toutes versions) : `POST /api/v2/auth/login` avec username+password,
  serveur renvoie un cookie `SID` à réutiliser dans les requêtes suivantes.

Réfs : wiki officiel qBittorrent / WebUI-API.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from trackr.http import make_client


class QbtError(RuntimeError):
    pass


class QbtAuthError(QbtError):
    pass


@dataclass
class QbtIdentity:
    """Infos serveur après auth réussie."""

    app_version: str
    webapi_version: str
    auth_mode: str  # "api_key" | "login"
    supports_api_key: bool  # True si webapi_version >= 2.14.1


def _normalize_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url:
        return ""
    if not urlparse(url).scheme:
        url = "http://" + url
    return url


def _version_tuple(v: str) -> tuple[int, ...]:
    parts = []
    for chunk in v.split("."):
        try:
            parts.append(int("".join(c for c in chunk if c.isdigit()) or "0"))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def _sid_cookie_name(base_url: str) -> str:
    """qBit récent utilise `QBT_SID_<port>` ; legacy `SID`. On retourne le bon nom."""
    parsed = urlparse(_normalize_url(base_url))
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return f"QBT_SID_{port}"


def _request(
    base_url: str,
    method: str,
    path: str,
    *,
    api_key: str = "",
    sid: str = "",
    data: dict | None = None,
) -> httpx.Response:
    base = _normalize_url(base_url)
    if not base:
        raise QbtError("URL qBittorrent manquante")
    headers = {"Referer": base}
    cookies = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if sid:
        # On essaye les deux noms : moderne et legacy.
        cookies[_sid_cookie_name(base)] = sid
        cookies["SID"] = sid
    try:
        with make_client(base_url=base, cookies=cookies or None) as client:
            return client.request(method, path, data=data, headers=headers)
    except httpx.HTTPError as e:
        raise QbtError(f"Connexion qBittorrent impossible : {e}") from e


def login(url: str, username: str, password: str) -> str:
    """Mode legacy : POST /api/v2/auth/login → retourne le SID cookie."""
    base = _normalize_url(url)
    headers = {"Referer": base}
    try:
        with make_client(base_url=base) as client:
            resp = client.post(
                "/api/v2/auth/login",
                data={"username": username, "password": password},
                headers=headers,
            )
    except httpx.HTTPError as e:
        raise QbtError(f"Connexion qBittorrent impossible : {e}") from e
    if resp.status_code == 403:
        raise QbtAuthError("qBittorrent : 403 IP bannie temporairement (trop de tentatives)")
    # qBit moderne renvoie 204 No Content + cookie ; legacy 200 avec body "Ok."/"Fails."
    if resp.status_code not in (200, 204):
        raise QbtError(f"qBittorrent /auth/login : HTTP {resp.status_code}")
    if resp.status_code == 200 and resp.text.strip().lower() != "ok.":
        raise QbtAuthError("qBittorrent : identifiants refusés")
    # Cookie : essai noms modernes (QBT_SID_<port>) puis legacy (SID)
    sid = resp.cookies.get(_sid_cookie_name(base)) or resp.cookies.get("SID")
    if not sid:
        # Dernier recours : scanner les Set-Cookie pour un SID-like
        for name, value in resp.cookies.items():
            if "SID" in name.upper():
                sid = value
                break
    if not sid:
        raise QbtError("qBittorrent : login OK mais cookie SID absent")
    return sid


def app_version(url: str, *, api_key: str = "", sid: str = "") -> str:
    resp = _request(url, "GET", "/api/v2/app/version", api_key=api_key, sid=sid)
    if resp.status_code in (401, 403):
        raise QbtAuthError(f"qBittorrent /app/version : HTTP {resp.status_code} (auth refusée)")
    if resp.status_code != 200:
        raise QbtError(f"qBittorrent /app/version : HTTP {resp.status_code}")
    return resp.text.strip()


def webapi_version(url: str, *, api_key: str = "", sid: str = "") -> str:
    """N'importe quel client (auth ou pas) peut interroger webapiVersion."""
    resp = _request(url, "GET", "/api/v2/app/webapiVersion", api_key=api_key, sid=sid)
    if resp.status_code != 200:
        raise QbtError(f"qBittorrent /webapiVersion : HTTP {resp.status_code}")
    return resp.text.strip()


def probe(url: str) -> tuple[str, bool]:
    """Test la joignabilité du WebUI.

    Retourne `(webapi_version, requires_auth)` :
    - Si l'instance autorise un accès non-authentifié (bypass localhost typiquement)
      → version connue, `requires_auth=False`.
    - Si `/webapiVersion` renvoie 403 (cas remote/tunnel sans bypass) → version
      inconnue à ce stade, `requires_auth=True`. La version sera connue après login.
    Lève `QbtError` uniquement si l'URL est injoignable ou non-qBit.
    """
    base = _normalize_url(url)
    if not base:
        raise QbtError("URL qBittorrent manquante")
    try:
        with make_client(base_url=base) as client:
            resp = client.get("/api/v2/app/webapiVersion", headers={"Referer": base})
    except httpx.HTTPError as e:
        raise QbtError(f"Connexion qBittorrent impossible : {e}") from e
    if resp.status_code == 200:
        return resp.text.strip(), False
    if resp.status_code in (401, 403):
        return "", True
    raise QbtError(f"qBittorrent /webapiVersion : HTTP {resp.status_code}")


def get_preferences(url: str, *, api_key: str = "", sid: str = "") -> dict:
    """GET /api/v2/app/preferences — retourne tout le dict de préférences."""
    resp = _request(url, "GET", "/api/v2/app/preferences", api_key=api_key, sid=sid)
    if resp.status_code in (401, 403):
        raise QbtAuthError(f"qBittorrent /preferences : HTTP {resp.status_code}")
    if resp.status_code != 200:
        raise QbtError(f"qBittorrent /preferences : HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError as e:
        raise QbtError(f"qBittorrent /preferences : JSON invalide ({e})") from e


def list_torrents(
    url: str,
    *,
    api_key: str = "",
    sid: str = "",
    tag: str = "",
) -> list[dict]:
    """GET /api/v2/torrents/info — liste des torrents (filtrable par tag)."""
    params = {}
    if tag:
        params["tag"] = tag
    base = _normalize_url(url)
    if not base:
        raise QbtError("URL qBittorrent manquante")
    headers = {"Referer": base}
    cookies = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if sid:
        cookies[_sid_cookie_name(base)] = sid
        cookies["SID"] = sid
    try:
        with make_client(base_url=base, cookies=cookies or None) as client:
            resp = client.get("/api/v2/torrents/info", params=params, headers=headers)
    except httpx.HTTPError as e:
        raise QbtError(f"Connexion qBittorrent impossible : {e}") from e
    if resp.status_code in (401, 403):
        raise QbtAuthError(f"qBittorrent /torrents/info : HTTP {resp.status_code}")
    if resp.status_code != 200:
        raise QbtError(f"qBittorrent /torrents/info : HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError as e:
        raise QbtError(f"qBittorrent /torrents/info : JSON invalide ({e})") from e
    return data if isinstance(data, list) else []


def add_torrent(
    url: str,
    torrent_path: Path,
    save_path: str,
    *,
    api_key: str = "",
    sid: str = "",
    category: str = "",
    tags: list[str] | None = None,
    paused: bool = False,
    skip_checking: bool = False,
) -> None:
    """Ajoute un .torrent à qBit dans le but de seeder les fichiers déjà sur disque.

    `save_path` doit être le dossier qui contient le(s) fichier(s) référencés par
    le .torrent. Pour un .torrent qui décrit un fichier seul `Film.mkv`, si le
    fichier est à `/a/b/Film.mkv` alors save_path = `/a/b`. qBit lance un recheck
    et passe direct en seeding si tout correspond — pas de re-download.
    """
    base = _normalize_url(url)
    if not base:
        raise QbtError("URL qBittorrent manquante")
    if not torrent_path.exists():
        raise QbtError(f"Fichier .torrent introuvable : {torrent_path}")
    headers = {"Referer": base}
    cookies = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if sid:
        cookies[_sid_cookie_name(base)] = sid
        cookies["SID"] = sid
    data = {
        "savepath": save_path,
        "paused": "true" if paused else "false",
        "skip_checking": "true" if skip_checking else "false",
        "autoTMM": "false",
    }
    if category:
        data["category"] = category
    if tags:
        data["tags"] = ",".join(tags)
    try:
        with torrent_path.open("rb") as fh, make_client(base_url=base, cookies=cookies or None) as client:
            files = {"torrents": (torrent_path.name, fh, "application/x-bittorrent")}
            resp = client.post(
                "/api/v2/torrents/add",
                data=data,
                files=files,
                headers=headers,
            )
    except httpx.HTTPError as e:
        raise QbtError(f"Connexion qBittorrent impossible : {e}") from e
    if resp.status_code in (401, 403):
        raise QbtAuthError(f"qBittorrent /torrents/add : HTTP {resp.status_code} (auth refusée)")
    if resp.status_code != 200:
        raise QbtError(f"qBittorrent /torrents/add : HTTP {resp.status_code}")
    if resp.text.strip().lower().startswith("fail"):
        raise QbtError(
            "qBittorrent a refusé le .torrent (.torrent invalide, save_path inaccessible, "
            "ou hash déjà présent)."
        )


def whoami(url: str, *, api_key: str = "", sid: str = "") -> QbtIdentity:
    """Vérifie l'auth en interrogeant les endpoints `app/*` et renvoie l'identité serveur."""
    app_v = app_version(url, api_key=api_key, sid=sid)
    api_v = webapi_version(url, api_key=api_key, sid=sid)
    supports = _version_tuple(api_v) >= (2, 14, 1)
    mode = "api_key" if api_key else "login"
    return QbtIdentity(
        app_version=app_v,
        webapi_version=api_v,
        auth_mode=mode,
        supports_api_key=supports,
    )

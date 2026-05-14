from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from trackr.http import make_client
from trackr.trackers.base import (
    AuthError,
    AuthResult,
    Profile,
    TrackerError,
)

WEB_BASE = "https://c411.org"
SESSION_COOKIE_NAME = "__Host-c411_session"
CSRF_COOKIE_NAME = "__csrf"
SESSION_TTL = timedelta(days=7)

_META_RX = re.compile(r'name="csrf-token"\s+content="([^"]+)"')


def _extract_error_msg(resp: httpx.Response, default: str) -> str:
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError):
        return default
    msg = data.get("message")
    if isinstance(msg, str) and msg:
        return msg
    err = data.get("error")
    if isinstance(err, str) and err:
        return err
    return default


@dataclass
class _PendingLogin:
    """État intermédiaire d'un login en cours (étape 2 → 3, MFA)."""

    cookies: httpx.Cookies
    csrf_meta: str


def _extract_csrf(html: str) -> str:
    m = _META_RX.search(html)
    if not m:
        raise TrackerError("C411 : impossible d'extraire le token CSRF de la page de login")
    return m.group(1)


def _profile_from_me(payload: dict) -> Profile:
    user = payload.get("user") or {}
    ratio = user.get("ratio")
    if isinstance(ratio, (int, float)):
        ratio_f: float | None = float(ratio)
    else:
        ratio_f = None
    return Profile(
        username=user.get("username") or "",
        email=user.get("email") or "",
        user_id=int(user.get("id") or 0),
        role=user.get("role") or "",
        ratio=ratio_f,
        uploaded_bytes=int(user.get("totalUploaded") or 0),
        bonus=int(user.get("bonus") or 0),
        permissions=list(user.get("permissions") or []),
    )


def login_step1(username: str, password: str) -> tuple[_PendingLogin, bool]:
    """Étape 1 : retourne (pending, mfa_required).

    `pending.csrf_meta` est toujours rempli (utile pour POST/DELETE ultérieurs).
    Si `mfa_required` est False, le login est déjà complet — appeler `finalize(pending)`.
    """
    try:
        with make_client(base_url=WEB_BASE) as client:
            r1 = client.get("/login")
            if r1.status_code != 200:
                raise TrackerError(f"C411 /login : HTTP {r1.status_code}")
            csrf_meta = _extract_csrf(r1.text)
            r2 = client.post(
                "/api/auth/login",
                json={"username": username, "password": password},
                headers={
                    "csrf-token": csrf_meta,
                    "Referer": f"{WEB_BASE}/login",
                    "Content-Type": "application/json",
                },
            )
            cookies = client.cookies
    except httpx.HTTPError as e:
        raise TrackerError(f"Connexion C411 impossible : {e}") from e

    if r2.status_code in (401, 403):
        msg = _extract_error_msg(r2, default="Identifiants refusés")
        raise AuthError(f"C411 : {msg}")
    if r2.status_code >= 500:
        raise TrackerError(f"C411 a renvoyé {r2.status_code} — réessaie plus tard")
    if r2.status_code != 200:
        raise TrackerError(f"C411 : login HTTP {r2.status_code}")

    try:
        data = r2.json()
    except (json.JSONDecodeError, ValueError) as e:
        raise TrackerError(f"C411 : JSON de login invalide ({e})") from e

    pending = _PendingLogin(cookies=cookies, csrf_meta=csrf_meta)
    if data.get("mfaRequired"):
        return pending, True
    if not data.get("success"):
        msg = data.get("message") if isinstance(data.get("message"), str) else None
        raise AuthError(f"C411 : {msg or 'login refusé'}")
    return pending, False


def submit_totp(pending: _PendingLogin, code: str) -> _PendingLogin:
    """Étape 2 : valide un code TOTP. Retourne le pending mis à jour (cookies enrichis)."""
    try:
        with make_client(base_url=WEB_BASE, cookies=pending.cookies) as client:
            resp = client.post(
                "/api/auth/mfa/totp",
                json={"code": code},
                headers={
                    "csrf-token": pending.csrf_meta,
                    "Referer": f"{WEB_BASE}/login",
                    "Content-Type": "application/json",
                },
            )
            cookies = client.cookies
    except httpx.HTTPError as e:
        raise TrackerError(f"Connexion C411 impossible : {e}") from e

    if resp.status_code in (401, 403):
        raise AuthError("C411 : code TOTP refusé")
    if resp.status_code != 200:
        raise TrackerError(f"C411 MFA : HTTP {resp.status_code}")

    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError) as e:
        raise TrackerError(f"C411 : JSON MFA invalide ({e})") from e

    if not data.get("success"):
        msg = data.get("message") if isinstance(data.get("message"), str) else None
        raise AuthError(f"C411 MFA : {msg or 'refusé'}")

    pending.cookies = cookies
    return pending


def finalize(pending: _PendingLogin, *, provision_api_key: bool = True, api_key_label: str = "trackr-cli") -> AuthResult:
    """Étape 3 : fetch profile + passkey + provisionne (optionnellement) une clé API.

    Provisionner = rotation : on supprime l'ancienne clé portant ce label (la
    valeur n'étant plus récupérable après création), puis on en crée une fraîche.
    """
    cookies = pending.cookies
    session_value = cookies.get(SESSION_COOKIE_NAME, domain="c411.org") or cookies.get(SESSION_COOKIE_NAME)
    if not session_value:
        raise TrackerError("C411 : cookie de session manquant après login")

    profile = fetch_profile(cookies)
    profile.passkey = fetch_passkey(cookies)

    api_key = ""
    api_key_ok = False
    if provision_api_key:
        try:
            existing = list_api_keys(cookies)
            for k in existing:
                if k.get("label") == api_key_label and k.get("id") is not None:
                    delete_api_key(cookies, pending.csrf_meta, k["id"])
            # Si l'user a 5 clés et aucune trackr-cli, on échoue avec un message clair.
            remaining = [k for k in existing if k.get("label") != api_key_label]
            if len(remaining) >= 5:
                raise TrackerError(
                    "Limite de 5 clés API atteinte côté C411. Supprime une clé dans "
                    "Profil → Intégrations API avant de réessayer."
                )
            api_key = create_api_key(cookies, pending.csrf_meta, label=api_key_label)
            api_key_ok = True
        except (AuthError, TrackerError):
            # Best-effort : on n'interrompt pas la config, l'user pourra coller manuellement
            api_key = ""
            api_key_ok = False

    return AuthResult(
        profile=profile,
        session_cookie=session_value,
        session_expires_at=datetime.now(timezone.utc) + SESSION_TTL,
        api_key=api_key,
        api_key_provisioned=api_key_ok,
    )


def fetch_profile(cookies: httpx.Cookies | dict) -> Profile:
    try:
        with make_client(base_url=WEB_BASE, cookies=cookies) as client:
            resp = client.get("/api/auth/me")
    except httpx.HTTPError as e:
        raise TrackerError(f"Connexion C411 impossible : {e}") from e
    if resp.status_code in (401, 403):
        raise AuthError("C411 : session expirée")
    if resp.status_code != 200:
        raise TrackerError(f"C411 /auth/me : HTTP {resp.status_code}")
    try:
        return _profile_from_me(resp.json())
    except (json.JSONDecodeError, ValueError) as e:
        raise TrackerError(f"C411 : JSON /auth/me invalide ({e})") from e


def list_api_keys(cookies: httpx.Cookies | dict) -> list[dict]:
    """Retourne la liste des clés API existantes (valeurs tronquées)."""
    try:
        with make_client(base_url=WEB_BASE, cookies=cookies) as client:
            resp = client.get("/api/profile/api-keys")
    except httpx.HTTPError as e:
        raise TrackerError(f"Connexion C411 impossible : {e}") from e
    if resp.status_code in (401, 403):
        raise AuthError("C411 : session expirée")
    if resp.status_code != 200:
        raise TrackerError(f"C411 /profile/api-keys : HTTP {resp.status_code}")
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError) as e:
        raise TrackerError(f"C411 : JSON api-keys invalide ({e})") from e
    keys = data.get("keys")
    if not isinstance(keys, list):
        return []
    return keys


DEFAULT_API_KEY_SCOPES = ("torznab:read", "upload:write", "drafts:rw")


def create_api_key(
    cookies: httpx.Cookies | dict,
    csrf_meta: str,
    label: str = "trackr-cli",
    scopes: tuple[str, ...] = DEFAULT_API_KEY_SCOPES,
) -> str:
    """Crée une nouvelle clé API et retourne la valeur en clair (visible une seule fois).

    ⚠️ Par défaut une clé créée via l'API n'a que `torznab:read` (lecture Prowlarr).
    Pour uploader, il faut explicitement `upload:write` (et `drafts:rw` pour les brouillons).
    """
    try:
        with make_client(base_url=WEB_BASE, cookies=cookies) as client:
            resp = client.post(
                "/api/profile/api-keys",
                json={"label": label, "scopes": list(scopes)},
                headers={
                    "csrf-token": csrf_meta,
                    "Referer": f"{WEB_BASE}/user/integrations",
                    "Content-Type": "application/json",
                },
            )
    except httpx.HTTPError as e:
        raise TrackerError(f"Connexion C411 impossible : {e}") from e
    if resp.status_code in (401, 403):
        raise AuthError("C411 : session expirée ou CSRF refusé")
    if resp.status_code >= 400:
        msg = _extract_error_msg(resp, default=f"HTTP {resp.status_code}")
        raise TrackerError(f"C411 création clé API : {msg}")
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError) as e:
        raise TrackerError(f"C411 : JSON création clé invalide ({e})") from e
    api_key = data.get("apiKey") or data.get("key") or data.get("token")
    if not api_key or not isinstance(api_key, str):
        raise TrackerError("C411 : la réponse ne contient pas de clé API en clair")
    return api_key


def delete_api_key(cookies: httpx.Cookies | dict, csrf_meta: str, key_id) -> None:
    try:
        with make_client(base_url=WEB_BASE, cookies=cookies) as client:
            resp = client.delete(
                f"/api/profile/api-keys/{key_id}",
                headers={
                    "csrf-token": csrf_meta,
                    "Referer": f"{WEB_BASE}/user/integrations",
                },
            )
    except httpx.HTTPError as e:
        raise TrackerError(f"Connexion C411 impossible : {e}") from e
    if resp.status_code in (401, 403):
        raise AuthError("C411 : session expirée ou CSRF refusé")
    if resp.status_code not in (200, 204):
        msg = _extract_error_msg(resp, default=f"HTTP {resp.status_code}")
        raise TrackerError(f"C411 suppression clé API : {msg}")


def fetch_passkey(cookies: httpx.Cookies | dict) -> str:
    try:
        with make_client(base_url=WEB_BASE, cookies=cookies) as client:
            resp = client.get("/api/profile/tracker")
    except httpx.HTTPError as e:
        raise TrackerError(f"Connexion C411 impossible : {e}") from e
    if resp.status_code in (401, 403):
        raise AuthError("C411 : session expirée")
    if resp.status_code != 200:
        raise TrackerError(f"C411 /profile/tracker : HTTP {resp.status_code}")
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError) as e:
        raise TrackerError(f"C411 : JSON /profile/tracker invalide ({e})") from e

    # On scanne récursivement la réponse pour trouver la passkey
    candidate_keys = ("passkey", "pass_key", "tracker_key", "announce_key")
    found = _find_first(data, candidate_keys)
    if not found:
        announce = _find_first(data, ("announce", "announceUrl", "announce_url"))
        if announce:
            m = re.search(r"/announce/([a-f0-9]{16,})", announce)
            if m:
                found = m.group(1)
    if not found:
        raise TrackerError("C411 : passkey introuvable dans /profile/tracker")
    return str(found)


def _find_first(obj, keys: tuple[str, ...]):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and isinstance(v, (str, int)):
                return v
        for v in obj.values():
            found = _find_first(v, keys)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_first(v, keys)
            if found:
                return found
    return None


def list_categories(api_key: str) -> list[dict]:
    """Récupère le catalogue des catégories C411 via Bearer."""
    try:
        with make_client(base_url=WEB_BASE) as client:
            resp = client.get(
                "/api/categories",
                headers={"Authorization": f"Bearer {api_key}"},
            )
    except httpx.HTTPError as e:
        raise TrackerError(f"Connexion C411 impossible : {e}") from e
    if resp.status_code in (401, 403):
        raise AuthError("C411 : API key refusée")
    if resp.status_code != 200:
        raise TrackerError(f"C411 /api/categories : HTTP {resp.status_code}")
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError) as e:
        raise TrackerError(f"C411 : JSON catégories invalide ({e})") from e
    # La réponse a la forme {data: [...]} sur l'API actuelle
    if isinstance(data, dict) and "data" in data:
        cats = data["data"]
    elif isinstance(data, list):
        cats = data
    else:
        cats = []
    return cats if isinstance(cats, list) else []


def get_subcategory_options(api_key: str, subcat_id: int) -> list[dict]:
    """Récupère les options dynamiques (Langue, Genre, Type…) pour une sous-catégorie.

    Format de retour :
    [
        {
            "id": 1,
            "name": "Langue",
            "slug": "langue",
            "allowsMultiple": True,
            "isRequired": True,
            "values": [{"id": 1, "value": "Anglais", "slug": "anglais"}, ...]
        },
        ...
    ]
    """
    try:
        with make_client(base_url=WEB_BASE) as client:
            resp = client.get(
                f"/api/categories/{subcat_id}/options",
                headers={"Authorization": f"Bearer {api_key}"},
            )
    except httpx.HTTPError as e:
        raise TrackerError(f"Connexion C411 impossible : {e}") from e
    if resp.status_code in (401, 403):
        raise AuthError("C411 : API key refusée")
    if resp.status_code != 200:
        raise TrackerError(f"C411 /categories/{subcat_id}/options : HTTP {resp.status_code}")
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError) as e:
        raise TrackerError(f"C411 : JSON options invalide ({e})") from e
    return data.get("data", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])


@dataclass
class UploadResult:
    info_hash: str
    status: str  # "pending" / "active" / etc.
    message: str
    raw: dict


def upload(
    api_key: str,
    *,
    torrent_path: Path,
    nfo_path: Path,
    title: str,
    category_id: int,
    subcategory_id: int,
    description: str,
    options: dict,
    tmdb_id: int = 0,
    tmdb_type: str = "movie",
    imdb_id: str = "",
    year: str = "",
    rawg_data: dict | None = None,
    description_format: str = "standard",
    custom_poster_url: str = "",
    is_exclusive: bool = False,
    uploader_note: str = "",
    bypassed_warnings: list | None = None,
) -> UploadResult:
    """POST /api/torrents — upload final.

    `options` est un dict `{option_id: value_id | [value_id]}` selon que
    l'option accepte multi ou non. Ex : `{1: [2], 5: [49, 50]}`.

    Pour un film/série : passer `tmdb_id`/`tmdb_type`/`year` (→ `tmdbData`).
    Pour un jeu vidéo  : passer `rawg_data` (objet RAWG complet) → `rawgData`.
    """
    import json as _json

    files = {
        "torrent": (torrent_path.name, torrent_path.read_bytes(), "application/x-bittorrent"),
        "nfo": (nfo_path.name, nfo_path.read_bytes(), "text/plain"),
    }
    data = {
        "title": title,
        "categoryId": str(category_id),
        "subcategoryId": str(subcategory_id),
        "description": description,
        "descriptionFormat": description_format,
        "options": _json.dumps(options),
        "customPosterUrl": custom_poster_url or "",
        "isExclusive": "true" if is_exclusive else "false",
        "uploaderNote": uploader_note or "",
        "bypassedWarnings": _json.dumps(bypassed_warnings or []),
    }
    if rawg_data is not None:
        data["rawgData"] = _json.dumps(rawg_data)
    else:
        tmdb_data = {
            "tmdbId": tmdb_id or None,
            "tmdbType": tmdb_type or None,
            "imdbId": imdb_id or None,
            "title": title,
            "year": year or None,
        }
        data["tmdbData"] = _json.dumps(tmdb_data)
    try:
        with make_client(base_url=WEB_BASE) as client:
            resp = client.post(
                "/api/torrents",
                data=data,
                files=files,
                headers={"Authorization": f"Bearer {api_key}"},
            )
    except httpx.HTTPError as e:
        raise TrackerError(f"Connexion C411 impossible : {e}") from e

    if resp.status_code in (401, 403):
        msg = _extract_error_msg(resp, default="API key refusée ou CSRF requis")
        raise AuthError(f"C411 upload : {msg}")
    if resp.status_code >= 400:
        msg = _extract_error_msg(resp, default=f"HTTP {resp.status_code}")
        raise TrackerError(f"C411 upload : {msg}")
    try:
        payload = resp.json()
    except (json.JSONDecodeError, ValueError) as e:
        raise TrackerError(f"C411 upload : JSON invalide ({e})") from e

    body = payload.get("data") or payload
    return UploadResult(
        info_hash=str(body.get("infoHash") or body.get("info_hash") or ""),
        status=str(body.get("status") or ""),
        message=str(payload.get("message") or ""),
        raw=payload,
    )


def download_torrent(session_cookie: str, identifier: str, out_path: Path) -> Path:
    """GET /api/torrents/{id_OR_hash}/download — retourne le .torrent signé par C411.

    Requiert la session web : le Bearer renvoie 401 sur cet endpoint. Le serveur
    injecte la passkey dans l'announce — utile en filet de sécurité si le
    .torrent local n'a pas la même casse `source` que celle normalisée serveur.
    """
    if not identifier:
        raise TrackerError("C411 download : identifiant manquant")
    if not session_cookie:
        raise AuthError(
            "C411 download : session web requise. Reconfigure C411 en mode Guidé "
            "pour rafraîchir la session."
        )
    cookies = {SESSION_COOKIE_NAME: session_cookie}
    try:
        with make_client(base_url=WEB_BASE, cookies=cookies) as client:
            resp = client.get(f"/api/torrents/{identifier}/download")
    except httpx.HTTPError as e:
        raise TrackerError(f"Connexion C411 impossible : {e}") from e
    if resp.status_code in (401, 403):
        raise AuthError("C411 download : session refusée — reconfigure en Guidé")
    if resp.status_code == 404:
        raise TrackerError("C411 download : torrent introuvable")
    if resp.status_code != 200:
        raise TrackerError(f"C411 download : HTTP {resp.status_code}")
    content = resp.content
    if not content or content[:1] != b"d":
        raise TrackerError("C411 download : contenu ne ressemble pas à un .torrent")
    out_path.write_bytes(content)
    return out_path


def delete_torrent(session_cookie: str, identifier: str) -> str:
    """DELETE /api/torrents/{id} — requiert **session web + CSRF**, le Bearer ne suffit pas.

    `identifier` accepte info_hash ou id numérique.
    """
    if not session_cookie:
        raise AuthError(
            "C411 : session web requise pour supprimer (Bearer refusé). "
            "Reconfigure C411 en mode Guidé pour rafraîchir la session."
        )
    cookies = {SESSION_COOKIE_NAME: session_cookie}
    try:
        with make_client(base_url=WEB_BASE, cookies=cookies) as client:
            # Récupère le CSRF token (signature HMAC de __csrf) depuis une page SSR
            rp = client.get("/login")
            if rp.status_code != 200:
                raise TrackerError(f"C411 /login : HTTP {rp.status_code}")
            csrf_meta = _extract_csrf(rp.text)
            resp = client.delete(
                f"/api/torrents/{identifier}",
                headers={"csrf-token": csrf_meta, "Referer": f"{WEB_BASE}/"},
            )
    except httpx.HTTPError as e:
        raise TrackerError(f"Connexion C411 impossible : {e}") from e
    if resp.status_code in (401, 403):
        raise AuthError("C411 : session expirée ou CSRF refusé. Reconfigure en Guidé.")
    if resp.status_code == 404:
        raise TrackerError("C411 : torrent introuvable (déjà supprimé ?)")
    if resp.status_code >= 400:
        msg = _extract_error_msg(resp, default=f"HTTP {resp.status_code}")
        raise TrackerError(f"C411 delete : {msg}")
    try:
        data = resp.json()
        return str(data.get("message") or "Supprimé.")
    except (json.JSONDecodeError, ValueError):
        return "Supprimé."


def list_my_uploads(api_key: str, username: str, limit: int = 20) -> list[dict]:
    """GET /api/torrents?uploader=<username>&limit=N — liste les uploads de cet user.

    Retourne la liste brute (chaque entry contient `title`, `status`, `infoHash`,
    `createdAt`, etc.). Filtrer côté caller.
    """
    if not username:
        return []
    try:
        with make_client(base_url=WEB_BASE) as client:
            resp = client.get(
                "/api/torrents",
                params={"uploader": username, "limit": str(limit)},
                headers={"Authorization": f"Bearer {api_key}"},
            )
    except httpx.HTTPError as e:
        raise TrackerError(f"Connexion C411 impossible : {e}") from e
    if resp.status_code in (401, 403):
        raise AuthError("C411 : API key refusée")
    if resp.status_code != 200:
        raise TrackerError(f"C411 /api/torrents : HTTP {resp.status_code}")
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError) as e:
        raise TrackerError(f"C411 : JSON uploads invalide ({e})") from e
    if isinstance(data, dict):
        items = data.get("data") or data.get("torrents") or []
    elif isinstance(data, list):
        items = data
    else:
        items = []
    return items if isinstance(items, list) else []


@dataclass
class Rejection:
    """Une demande de révision (= rejet) C411 extraite des notifications."""
    notification_id: int
    info_hash: str
    torrent_name: str
    reason: str
    created_at: str  # ISO timestamp


def list_notifications(session_cookie: str, limit: int = 50) -> list[dict]:
    """GET /api/notifications — liste brute des notifs (session web requise)."""
    if not session_cookie:
        raise AuthError("C411 notifications : session web requise.")
    cookies = {SESSION_COOKIE_NAME: session_cookie}
    try:
        with make_client(base_url=WEB_BASE, cookies=cookies) as client:
            resp = client.get("/api/notifications", params={"limit": str(limit)})
    except httpx.HTTPError as e:
        raise TrackerError(f"Connexion C411 impossible : {e}") from e
    if resp.status_code in (401, 403):
        raise AuthError("C411 notifications : session refusée — reconfigure en Guidé.")
    if resp.status_code != 200:
        raise TrackerError(f"C411 /api/notifications : HTTP {resp.status_code}")
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError) as e:
        raise TrackerError(f"C411 : JSON notifications invalide ({e})") from e
    items = data.get("data") if isinstance(data, dict) else data
    return items if isinstance(items, list) else []


def list_rejections(session_cookie: str, *, include_read: bool = False) -> list[Rejection]:
    """Filtre les notifications `torrent_revision_requested` (= torrent rejeté).

    Par défaut on exclut les notifs déjà marquées lues — c'est ce que
    trackr fait automatiquement après un resubmit réussi, donc « non lu »
    correspond aux rejets restant à traiter. Pour un audit complet, passer
    `include_read=True`.

    En complément, on cross-check le statut serveur du torrent : si la notif
    est non-lue mais que le torrent n'est plus en `revision_requested`
    (corrigé hors trackr, supprimé…), on l'exclut aussi.
    """
    out: list[Rejection] = []
    for n in list_notifications(session_cookie):
        if n.get("type") != "torrent_revision_requested":
            continue
        if not include_read and n.get("isRead"):
            continue
        d = n.get("data") or {}
        info_hash = str(d.get("infoHash") or "")
        # Vérif statut serveur — best-effort, ne bloque pas si le GET échoue
        if info_hash:
            try:
                t = fetch_torrent(session_cookie, info_hash)
                if t.get("status") != "revision_requested":
                    continue
            except (AuthError, TrackerError):
                pass
        out.append(
            Rejection(
                notification_id=int(n.get("id") or 0),
                info_hash=info_hash,
                torrent_name=str(d.get("torrentName") or n.get("title") or ""),
                reason=str(d.get("reason") or n.get("message") or ""),
                created_at=str(n.get("createdAt") or ""),
            )
        )
    return out


def _fetch_csrf_token(client: httpx.Client) -> str:
    """Récupère le csrf-token meta depuis la home (la page /login peut rediriger pour un user loggué)."""
    r = client.get("/")
    if r.status_code != 200:
        raise TrackerError(f"C411 / : HTTP {r.status_code}")
    return _extract_csrf(r.text)


def mark_notification_read(session_cookie: str, notification_id: int) -> None:
    """PATCH /api/notifications/{id} body {isRead: true}."""
    if not session_cookie:
        raise AuthError("C411 : session web requise.")
    cookies = {SESSION_COOKIE_NAME: session_cookie}
    try:
        with make_client(base_url=WEB_BASE, cookies=cookies) as client:
            csrf = _fetch_csrf_token(client)
            resp = client.patch(
                f"/api/notifications/{notification_id}",
                json={"isRead": True},
                headers={"csrf-token": csrf, "Referer": f"{WEB_BASE}/", "Content-Type": "application/json"},
            )
    except httpx.HTTPError as e:
        raise TrackerError(f"Connexion C411 impossible : {e}") from e
    if resp.status_code in (401, 403):
        raise AuthError("C411 mark-read : session/CSRF refusé.")
    if resp.status_code >= 400:
        raise TrackerError(f"C411 mark-read : HTTP {resp.status_code}")


@dataclass
class EditResult:
    success: bool
    message: str
    modified_fields: list[str]
    set_to_pending: bool


def edit_torrent(
    session_cookie: str,
    info_hash: str,
    *,
    title: str | None = None,
    description: str | None = None,
    description_format: str = "standard",
    options: dict | None = None,
) -> EditResult:
    """PATCH /api/torrents/{infoHash}. Pour resoumettre après rejet.

    Champs acceptés par le serveur : title, description, descriptionFormat
    ('standard'|'html'), options. Toute autre clé est rejetée.
    `descriptionFormat='standard'` accepte du BBCode (le serveur le rend en
    HTML côté affichage).
    """
    if not session_cookie:
        raise AuthError("C411 edit : session web requise.")
    if not info_hash:
        raise TrackerError("C411 edit : info_hash manquant.")

    payload: dict = {"descriptionFormat": description_format}
    if title is not None:
        payload["title"] = title
    if description is not None:
        payload["description"] = description
    if options is not None:
        payload["options"] = options

    cookies = {SESSION_COOKIE_NAME: session_cookie}
    try:
        with make_client(base_url=WEB_BASE, cookies=cookies) as client:
            csrf = _fetch_csrf_token(client)
            resp = client.patch(
                f"/api/torrents/{info_hash}",
                json=payload,
                headers={"csrf-token": csrf, "Referer": f"{WEB_BASE}/", "Content-Type": "application/json"},
            )
    except httpx.HTTPError as e:
        raise TrackerError(f"Connexion C411 impossible : {e}") from e

    if resp.status_code in (401, 403):
        msg = _extract_error_msg(resp, default="session/CSRF refusé")
        raise AuthError(f"C411 edit : {msg}")
    if resp.status_code >= 400:
        msg = _extract_error_msg(resp, default=f"HTTP {resp.status_code}")
        raise TrackerError(f"C411 edit : {msg}")
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError) as e:
        raise TrackerError(f"C411 edit : JSON invalide ({e})") from e

    return EditResult(
        success=bool(data.get("success")),
        message=str(data.get("message") or ""),
        modified_fields=list(data.get("modifiedFields") or []),
        set_to_pending=bool(data.get("setToPending")),
    )


def resubmit_torrent(session_cookie: str, info_hash: str) -> str:
    """POST /api/torrents/{hash}/resubmit — renvoie le torrent à la validation.

    À appeler après un `edit_torrent` réussi : sans cet appel le torrent reste
    en `revision_requested`. Retourne le message du serveur.
    """
    if not session_cookie:
        raise AuthError("C411 resubmit : session web requise.")
    if not info_hash:
        raise TrackerError("C411 resubmit : info_hash manquant.")
    cookies = {SESSION_COOKIE_NAME: session_cookie}
    try:
        with make_client(base_url=WEB_BASE, cookies=cookies) as client:
            csrf = _fetch_csrf_token(client)
            resp = client.post(
                f"/api/torrents/{info_hash}/resubmit",
                json={},
                headers={"csrf-token": csrf, "Referer": f"{WEB_BASE}/", "Content-Type": "application/json"},
            )
    except httpx.HTTPError as e:
        raise TrackerError(f"Connexion C411 impossible : {e}") from e
    if resp.status_code in (401, 403):
        raise AuthError("C411 resubmit : session/CSRF refusé.")
    if resp.status_code >= 400:
        msg = _extract_error_msg(resp, default=f"HTTP {resp.status_code}")
        raise TrackerError(f"C411 resubmit : {msg}")
    try:
        return str(resp.json().get("message") or "Renvoyé à validation.")
    except (json.JSONDecodeError, ValueError):
        return "Renvoyé à validation."


def fetch_torrent(session_cookie: str, info_hash: str) -> dict:
    """GET /api/torrents/{hash} — utile pour vérifier statut + options en place."""
    cookies = {SESSION_COOKIE_NAME: session_cookie} if session_cookie else {}
    try:
        with make_client(base_url=WEB_BASE, cookies=cookies) as client:
            resp = client.get(f"/api/torrents/{info_hash}")
    except httpx.HTTPError as e:
        raise TrackerError(f"Connexion C411 impossible : {e}") from e
    if resp.status_code == 404:
        raise TrackerError("C411 : torrent introuvable.")
    if resp.status_code != 200:
        raise TrackerError(f"C411 /api/torrents/{info_hash} : HTTP {resp.status_code}")
    try:
        return resp.json()
    except (json.JSONDecodeError, ValueError) as e:
        raise TrackerError(f"C411 : JSON torrent invalide ({e})") from e


def validate_api_key(api_key: str) -> Profile:
    """Valide un Bearer API key en pingant un endpoint qui exige le scope upload.

    Le Bearer ne donne pas accès à /api/auth/me ; on tape /api/user/drafts qui
    renvoie 401 sur une clé invalide ou sans scope. Renvoie un Profile vide
    (la passkey n'est pas exposée via Bearer, à saisir manuellement).
    """
    try:
        with make_client(base_url=WEB_BASE) as client:
            resp = client.get(
                "/api/user/drafts",
                headers={"Authorization": f"Bearer {api_key}"},
            )
    except httpx.HTTPError as e:
        raise TrackerError(f"Connexion C411 impossible : {e}") from e

    if resp.status_code in (401, 403):
        raise AuthError("C411 : API key refusée")
    if resp.status_code != 200:
        raise TrackerError(f"C411 /api/user/drafts : HTTP {resp.status_code}")

    return Profile()


# ───────────────────────────── RAWG (jeux vidéo) ─────────────────────────────


@dataclass
class RawgResult:
    rawg_id: int
    slug: str
    title: str
    year: str
    image_url: str
    genres: tuple[str, ...]
    platforms: tuple[str, ...]
    raw: dict


def rawg_search(session_cookie: str, query: str, *, limit: int = 8) -> list[RawgResult]:
    """GET /api/rawg/search?q=... — recherche RAWG via le wrapper C411.

    Requiert la session web (le Bearer ne fonctionne pas sur cet endpoint).
    Renvoie au plus `limit` résultats.
    """
    if not session_cookie:
        raise AuthError("C411 RAWG : session web requise (mode Guidé).")
    q = (query or "").strip()
    if not q:
        return []
    cookies = {SESSION_COOKIE_NAME: session_cookie}
    try:
        with make_client(base_url=WEB_BASE, cookies=cookies) as client:
            resp = client.get("/api/rawg/search", params={"q": q})
    except httpx.HTTPError as e:
        raise TrackerError(f"Connexion C411 impossible : {e}") from e

    if resp.status_code in (401, 403):
        raise AuthError("C411 RAWG search : session expirée — relance la config C411 (mode Guidé).")
    if resp.status_code != 200:
        raise TrackerError(f"C411 RAWG search : HTTP {resp.status_code}")
    try:
        payload = resp.json()
    except (json.JSONDecodeError, ValueError) as e:
        raise TrackerError(f"C411 RAWG search : JSON invalide ({e})") from e

    body = payload.get("data") or payload
    results = body.get("results") if isinstance(body, dict) else None
    out: list[RawgResult] = []
    for item in (results or [])[:limit]:
        if not isinstance(item, dict):
            continue
        out.append(
            RawgResult(
                rawg_id=int(item.get("id") or 0),
                slug=str(item.get("slug") or ""),
                title=str(item.get("title") or item.get("name") or "?"),
                year=str(item.get("year") or ""),
                image_url=str(item.get("imageUrl") or item.get("backgroundUrl") or ""),
                genres=tuple(str(g) for g in (item.get("genres") or []) if isinstance(g, str)),
                platforms=tuple(str(p) for p in (item.get("platforms") or []) if isinstance(p, str)),
                raw=item,
            )
        )
    return out


@dataclass
class RawgLookup:
    game: dict                   # objet RAWG complet à renvoyer en `rawgData`
    presentation_html: str       # rendu HTML prêt à envoyer en descriptionFormat=html
    presentation_text: str       # version texte brut (sans markup)
    genre_option_ids: list[int]  # IDs option C411 à pré-cocher (option id 5 "genre")

    @property
    def presentation(self) -> str:
        """Compat : retourne le HTML par défaut."""
        return self.presentation_html


def rawg_lookup(session_cookie: str, rawg_id: int, *, presentation: bool = True) -> RawgLookup:
    """GET /api/rawg/lookup?id=X&presentation=true — détail RAWG + BBCode + genres mappés."""
    if not session_cookie:
        raise AuthError("C411 RAWG : session web requise (mode Guidé).")
    if not rawg_id:
        raise TrackerError("C411 RAWG lookup : id manquant")
    cookies = {SESSION_COOKIE_NAME: session_cookie}
    params = {"id": str(rawg_id)}
    if presentation:
        params["presentation"] = "true"
    try:
        with make_client(base_url=WEB_BASE, cookies=cookies) as client:
            resp = client.get("/api/rawg/lookup", params=params)
    except httpx.HTTPError as e:
        raise TrackerError(f"Connexion C411 impossible : {e}") from e

    if resp.status_code in (401, 403):
        raise AuthError("C411 RAWG lookup : session expirée — relance la config C411 (mode Guidé).")
    if resp.status_code == 404:
        raise TrackerError(f"C411 RAWG lookup : jeu id={rawg_id} introuvable")
    if resp.status_code != 200:
        raise TrackerError(f"C411 RAWG lookup : HTTP {resp.status_code}")
    try:
        payload = resp.json()
    except (json.JSONDecodeError, ValueError) as e:
        raise TrackerError(f"C411 RAWG lookup : JSON invalide ({e})") from e

    body = payload.get("data") or payload
    game = body.get("game") if isinstance(body, dict) else None
    if not isinstance(game, dict):
        raise TrackerError("C411 RAWG lookup : structure inattendue")
    pres_raw = body.get("presentation") if isinstance(body, dict) else None
    if isinstance(pres_raw, dict):
        pres_html = str(pres_raw.get("html") or "")
        pres_text = str(pres_raw.get("plainText") or pres_raw.get("text") or "")
    elif isinstance(pres_raw, str):
        pres_html = pres_raw
        pres_text = ""
    else:
        pres_html = ""
        pres_text = ""
    raw_genres = body.get("genreOptionIds") if isinstance(body, dict) else []
    return RawgLookup(
        game=game,
        presentation_html=pres_html,
        presentation_text=pres_text,
        genre_option_ids=[int(g) for g in (raw_genres or []) if str(g).isdigit() or isinstance(g, int)],
    )

"""Réupload de screenshots vers un host d'images (Catbox).

Sert à transformer des URLs distantes (ex: media.rawg.io) en URLs hébergées
de façon stable et neutre. Catbox accepte deux modes : `urlupload` (le serveur
va chercher l'image lui-même) et `fileupload` (multipart). On tente d'abord
`urlupload` (rapide, pas de transfert local), avec repli sur le download +
`fileupload` si le host source bloque Catbox.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from trackr.http import make_client

CATBOX_API = "https://catbox.moe/user/api.php"
_MAX_BYTES = 200 * 1024 * 1024  # garde-fou (Catbox limite à 200 Mo)


class ImageHostError(Exception):
    pass


@dataclass
class RehostResult:
    url: str          # URL finale (hébergée si OK, sinon URL d'origine)
    rehosted: bool    # True si effectivement réuploadé
    error: str = ""   # message si fallback sur l'URL d'origine


def _catbox_urlupload(remote_url: str) -> str:
    with make_client() as client:
        resp = client.post(
            CATBOX_API,
            data={"reqtype": "urlupload", "userhash": "", "url": remote_url},
            timeout=httpx.Timeout(90.0, connect=10.0),
        )
    body = (resp.text or "").strip()
    if resp.status_code == 200 and body.startswith("https://"):
        return body
    raise ImageHostError(body or f"HTTP {resp.status_code}")


def _download(url: str) -> tuple[bytes, str]:
    with make_client() as client:
        resp = client.get(url, timeout=httpx.Timeout(60.0, connect=10.0))
    if resp.status_code != 200:
        raise ImageHostError(f"download HTTP {resp.status_code}")
    data = resp.content
    if not data:
        raise ImageHostError("réponse vide")
    if len(data) > _MAX_BYTES:
        raise ImageHostError("image trop lourde")
    ctype = resp.headers.get("content-type", "").split(";")[0].strip()
    if ctype and not ctype.startswith("image/"):
        raise ImageHostError(f"pas une image ({ctype})")
    ext = {
        "image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png",
        "image/webp": "webp", "image/gif": "gif",
    }.get(ctype, url.rsplit(".", 1)[-1].split("?")[0][:4] or "jpg")
    return data, ext


def _catbox_fileupload(data: bytes, filename: str) -> str:
    with make_client() as client:
        resp = client.post(
            CATBOX_API,
            data={"reqtype": "fileupload", "userhash": ""},
            files={"fileToUpload": (filename, data, "application/octet-stream")},
            timeout=httpx.Timeout(120.0, connect=10.0),
        )
    body = (resp.text or "").strip()
    if resp.status_code == 200 and body.startswith("https://"):
        return body
    raise ImageHostError(body or f"HTTP {resp.status_code}")


def rehost_one(remote_url: str, host: str = "catbox", *, index: int = 0) -> RehostResult:
    """Réuploade une image. En cas d'échec, renvoie l'URL d'origine (jamais d'exception)."""
    if host != "catbox":
        return RehostResult(url=remote_url, rehosted=False, error=f"host '{host}' non géré")
    try:
        try:
            return RehostResult(url=_catbox_urlupload(remote_url), rehosted=True)
        except (ImageHostError, httpx.HTTPError):
            data, ext = _download(remote_url)
            hosted = _catbox_fileupload(data, f"screenshot_{index + 1}.{ext}")
            return RehostResult(url=hosted, rehosted=True)
    except (ImageHostError, httpx.HTTPError) as e:
        return RehostResult(url=remote_url, rehosted=False, error=str(e))


def rehost_many(urls: list[str], host: str = "catbox") -> list[RehostResult]:
    return [rehost_one(u, host, index=i) for i, u in enumerate(urls)]

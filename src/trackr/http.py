from __future__ import annotations

import httpx

BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


_AUTO = object()


def _resolve_proxy() -> str | None:
    """Charge l'URL du proxy depuis la config. Tolère l'absence de config."""
    try:
        from trackr.config import load_config

        url = (load_config().proxy_url or "").strip()
        return url or None
    except Exception:
        return None


def make_client(
    *,
    base_url: str = "",
    cookies: httpx.Cookies | dict | None = None,
    follow_redirects: bool = True,
    user_agent: str = BROWSER_UA,
    proxy: str | None | object = _AUTO,
) -> httpx.Client:
    """Crée un client httpx. Par défaut, applique le proxy configuré (`proxy_url`).

    proxy:
      - `_AUTO` (défaut) → lit `proxy_url` dans la config si défini.
      - `None` ou chaîne vide → désactive le proxy pour cet appel (utile pour le test sans proxy).
      - chaîne explicite → utilise cette URL (utile pour tester un candidat avant de l'enregistrer).
    """
    headers = {
        "User-Agent": user_agent,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    }
    if proxy is _AUTO:
        proxy_url = _resolve_proxy()
    elif proxy:
        proxy_url = str(proxy).strip() or None
    else:
        proxy_url = None

    kwargs: dict = dict(
        base_url=base_url,
        headers=headers,
        cookies=cookies,
        timeout=DEFAULT_TIMEOUT,
        follow_redirects=follow_redirects,
        http2=False,
    )
    if proxy_url:
        kwargs["proxy"] = proxy_url
    return httpx.Client(**kwargs)

from __future__ import annotations

from urllib.parse import urlparse

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


def _is_local_host(host: str) -> bool:
    """True si le hostname pointe vers localhost / un réseau privé / un domaine local.

    On bypass le proxy pour ces hosts (sinon un qBit local routé via SOCKS5 VPN
    fait timeout : le proxy distant ne peut pas joindre 127.0.0.1 sur l'host).
    """
    if not host:
        return False
    h = host.lower()
    if h in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return True
    # RFC 1918 : 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16
    if h.startswith("10.") or h.startswith("192.168."):
        return True
    if h.startswith("172."):
        try:
            second = int(h.split(".")[1])
            if 16 <= second <= 31:
                return True
        except (ValueError, IndexError):
            pass
    # Loopback IPv6 + link-local
    if h.startswith("fe80:") or h.startswith("fc") or h.startswith("fd"):
        return True
    # Tailscale CGNAT (100.64.0.0/10)
    if h.startswith("100."):
        try:
            second = int(h.split(".")[1])
            if 64 <= second <= 127:
                return True
        except (ValueError, IndexError):
            pass
    # Domaines locaux usuels (mDNS, LAN)
    if h.endswith((".local", ".lan", ".home", ".internal", ".localhost")):
        return True
    return False


def _is_local_url(url: str) -> bool:
    if not url:
        return False
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return False
    return _is_local_host(host)


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

    # Bypass proxy pour les hosts locaux/privés (qBit local, NAS LAN, Tailscale)
    # — un SOCKS5 distant ne peut pas joindre 127.0.0.1 ou 192.168.* côté host.
    if proxy_url and _is_local_url(base_url):
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

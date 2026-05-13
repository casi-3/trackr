from __future__ import annotations

import httpx

BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def make_client(
    *,
    base_url: str = "",
    cookies: httpx.Cookies | dict | None = None,
    follow_redirects: bool = True,
    user_agent: str = BROWSER_UA,
) -> httpx.Client:
    headers = {
        "User-Agent": user_agent,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    }
    return httpx.Client(
        base_url=base_url,
        headers=headers,
        cookies=cookies,
        timeout=DEFAULT_TIMEOUT,
        follow_redirects=follow_redirects,
        http2=False,
    )

"""Vérification + auto-update via GitHub Releases.

Logique :
- À chaque démarrage, GET /repos/casi-3/trackr/releases/latest (timeout court,
  silencieux en cas d'erreur réseau).
- Compare la tag à `__version__`.
- Si plus récent, propose à l'user de mettre à jour.

Modes d'installation supportés pour l'auto-update :
- `binary` (PyInstaller Linux/macOS) : download asset → swap → restart via execv.
- `git`    (clone source)            : `git pull --ff-only` → restart Python.
- `pip`    (pip install)             : affiche la commande à taper.
- `windows-binary`                   : affiche un lien vers la release.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

from trackr import __version__
from trackr.http import make_client

GITHUB_API = "https://api.github.com/repos/casi-3/trackr/releases/latest"
GITHUB_RELEASES_URL = "https://github.com/casi-3/trackr/releases/latest"


@dataclass
class UpdateInfo:
    latest_tag: str        # ex "v0.2.0"
    latest_version: str    # ex "0.2.0"
    current_version: str   # ex "0.1.0"
    html_url: str
    notes: str
    asset_url: str = ""    # URL du binaire correspondant à la plateforme courante
    asset_name: str = ""   # ex "trackr-linux-x64"


# ─────────────────────────── version helpers ───────────────────────────


def _version_tuple(v: str) -> tuple[int, ...]:
    """Parse `0.1.0` ou `v0.1.0-rc1` en tuple. Pre-release ignoré (rc1 → 0)."""
    v = v.lstrip("v").strip()
    out: list[int] = []
    for part in v.split("."):
        num = ""
        for ch in part:
            if ch.isdigit():
                num += ch
            else:
                break
        out.append(int(num) if num else 0)
    return tuple(out)


def _is_newer(latest: str, current: str) -> bool:
    return _version_tuple(latest) > _version_tuple(current)


# ─────────────────────────── platform / install detection ───────────────────────────


def _expected_asset_name() -> str:
    """Nom de l'asset GitHub correspondant à la plateforme courante."""
    sysname = sys.platform
    if sysname == "linux":
        return "trackr-linux-x64"
    if sysname == "darwin":
        # Seul arm64 est buildé par le workflow actuel.
        return "trackr-macos-arm64"
    if sysname == "win32":
        return "trackr-windows-x64.exe"
    return ""


def detect_install_mode() -> str:
    """Renvoie 'binary' | 'windows-binary' | 'git' | 'pip'."""
    if getattr(sys, "frozen", False):
        return "windows-binary" if sys.platform == "win32" else "binary"
    try:
        root = Path(__file__).resolve().parents[2]  # src/trackr/updater.py → repo root
        if (root / ".git").exists():
            return "git"
    except (OSError, IndexError):
        pass
    return "pip"


# ─────────────────────────── HTTP ───────────────────────────


def fetch_latest_release(timeout: float = 3.0) -> UpdateInfo | None:
    """Renvoie l'info de la release la plus récente, ou None en cas d'erreur réseau."""
    try:
        with make_client(
            user_agent=f"trackr/{__version__}",
        ) as c:
            c.timeout = httpx.Timeout(timeout, connect=min(timeout, 5.0))
            r = c.get(GITHUB_API, headers={"Accept": "application/vnd.github+json"})
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    try:
        d = r.json()
    except json.JSONDecodeError:
        return None
    tag = str(d.get("tag_name") or "")
    if not tag:
        return None
    info = UpdateInfo(
        latest_tag=tag,
        latest_version=tag.lstrip("v"),
        current_version=__version__,
        html_url=str(d.get("html_url") or GITHUB_RELEASES_URL),
        notes=str(d.get("body") or ""),
    )
    expected = _expected_asset_name()
    for asset in d.get("assets", []) or []:
        if asset.get("name") == expected:
            info.asset_name = expected
            info.asset_url = str(asset.get("browser_download_url") or "")
            break
    return info


def check() -> UpdateInfo | None:
    """Renvoie l'UpdateInfo si une mise à jour est disponible, sinon None."""
    info = fetch_latest_release()
    if info is None:
        return None
    if not _is_newer(info.latest_version, info.current_version):
        return None
    return info


# ─────────────────────────── apply update ───────────────────────────


class UpdateError(RuntimeError):
    pass


def _download_to(url: str, dest: Path, timeout: float = 60.0) -> None:
    try:
        with make_client(user_agent=f"trackr/{__version__}") as c:
            c.timeout = httpx.Timeout(timeout, connect=min(timeout, 10.0))
            with c.stream("GET", url) as r:
                if r.status_code != 200:
                    raise UpdateError(f"download HTTP {r.status_code}")
                with dest.open("wb") as fh:
                    for chunk in r.iter_bytes(chunk_size=64 * 1024):
                        if chunk:
                            fh.write(chunk)
    except httpx.HTTPError as e:
        raise UpdateError(f"download échoué : {e}") from e


def _apply_binary(info: UpdateInfo) -> None:
    """Linux/macOS : swap du binaire en place (Unix permet le rename d'un exe qui tourne)."""
    if not info.asset_url:
        raise UpdateError(
            f"Aucun asset {info.asset_name or '(inconnu)'} dans la release {info.latest_tag}."
        )
    current = Path(sys.executable).resolve()
    if not current.exists():
        raise UpdateError(f"Binaire courant introuvable : {current}")
    tmp = current.with_name(current.name + ".new")
    _download_to(info.asset_url, tmp)
    # Sanity check basique
    if tmp.stat().st_size < 1_000_000:  # un PyInstaller onefile fait plusieurs Mo
        tmp.unlink(missing_ok=True)
        raise UpdateError("Téléchargement trop petit — annulé.")
    try:
        tmp.chmod(tmp.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        os.replace(tmp, current)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise UpdateError(f"Swap impossible : {e}") from e
    # Restart
    try:
        os.execv(str(current), [str(current), *sys.argv[1:]])
    except OSError as e:
        raise UpdateError(f"Redémarrage échoué : {e}") from e


def _apply_git(info: UpdateInfo) -> None:
    root = Path(__file__).resolve().parents[2]
    if not (root / ".git").exists():
        raise UpdateError(f"Pas de dépôt git à {root}")
    if not shutil.which("git"):
        raise UpdateError("`git` introuvable dans le PATH.")
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "pull", "--ff-only"],
            capture_output=True, text=True, timeout=60,
        )
    except (subprocess.SubprocessError, OSError) as e:
        raise UpdateError(f"git pull échoué : {e}") from e
    if proc.returncode != 0:
        raise UpdateError(f"git pull refusé : {proc.stderr.strip() or proc.stdout.strip()}")
    # Restart : on relance l'interpréteur courant avec le même argv.
    try:
        os.execv(sys.executable, [sys.executable, "-m", "trackr", *sys.argv[1:]])
    except OSError as e:
        raise UpdateError(f"Redémarrage échoué : {e}") from e


def apply_update(info: UpdateInfo) -> None:
    """Branche selon le mode d'install. Lève UpdateError si non supporté ou en échec."""
    mode = detect_install_mode()
    if mode == "binary":
        _apply_binary(info)
        return
    if mode == "git":
        _apply_git(info)
        return
    if mode == "windows-binary":
        raise UpdateError(
            "Sur Windows, télécharge manuellement le nouveau binaire depuis :\n"
            f"{info.html_url}"
        )
    # pip
    raise UpdateError(
        "Installation pip détectée — mets à jour avec :\n"
        "  pip install --upgrade git+https://github.com/casi-3/trackr.git"
    )


def manual_instructions(info: UpdateInfo) -> str:
    """Texte d'instructions selon le mode — pour fallback non-auto."""
    mode = detect_install_mode()
    if mode == "windows-binary":
        return (
            f"Télécharge `{info.asset_name or 'trackr-windows-x64.exe'}` depuis :\n"
            f"  {info.html_url}\n"
            "et remplace ton exe actuel."
        )
    if mode == "pip":
        return "pip install --upgrade git+https://github.com/casi-3/trackr.git"
    return f"Voir : {info.html_url}"

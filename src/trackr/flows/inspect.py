from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlparse

import questionary
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from trackr import ui
from trackr.media.mediainfo import MediaInfo, MediainfoError, probe, resolution_label

console = ui.console


def _normalize_path(raw: str) -> Path:
    """Nettoie un chemin issu d'un copier-coller / drag&drop.

    Gère : quotes, espaces échappés (`Mon\\ fichier.mkv`), URL-encoding (`%20`),
    schéma `file://`, et `~`.
    """
    s = raw.strip().strip("'\"")
    if s.startswith("file://"):
        parsed = urlparse(s)
        s = unquote(parsed.path)
    elif "%" in s:
        s = unquote(s)
    # Espaces échappés type shell : `\ ` → ` `
    s = s.replace("\\ ", " ")
    return Path(s).expanduser()


def _human_size(n: int) -> str:
    """Décimal (GB, MB) — cohérent avec qBit/OS/trackers."""
    if n <= 0:
        return "?"
    units = ["B", "kB", "MB", "GB", "TB"]
    size = float(n)
    for unit in units:
        if size < 1000 or unit == units[-1]:
            return f"{size:.2f} {unit}"
        size /= 1000
    return f"{size:.2f} {units[-1]}"


def _human_duration(seconds: float) -> str:
    if seconds <= 0:
        return "?"
    total = int(seconds // 1000) if seconds > 1e6 else int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    return f"{m}m {s:02d}s"


def _human_bitrate(n: int) -> str:
    if n <= 0:
        return "?"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f} Mb/s"
    if n >= 1_000:
        return f"{n / 1_000:.0f} kb/s"
    return f"{n} b/s"


def _render_panel(info: MediaInfo) -> Panel:
    header = Table.grid(padding=(0, 2))
    header.add_column(style="dim")
    header.add_column()
    header.add_row("Fichier", str(info.path))
    header.add_row("Conteneur", info.container or "?")
    header.add_row("Taille", _human_size(info.file_size))
    header.add_row("Durée", _human_duration(info.duration_s))
    header.add_row("Bitrate global", _human_bitrate(info.overall_bitrate))

    video = Table(title="Vidéo", title_style="bold cyan", show_header=False, expand=True)
    video.add_column(style="dim", width=14)
    video.add_column()
    v = info.video
    video.add_row("Codec", f"{v.codec} {v.profile}".strip() or "?")
    video.add_row("Résolution", f"{v.width}×{v.height}  ({resolution_label(info)})" if v.width else "?")
    video.add_row("FPS", f"{v.fps:.3f}" if v.fps else "?")
    video.add_row("Bitrate", _human_bitrate(v.bitrate))
    video.add_row("Bit depth", f"{v.bit_depth}-bit" if v.bit_depth else "?")
    video.add_row("Scan", v.scan_type or "?")

    audio = Table(title=f"Audio ({len(info.audio)})", title_style="bold magenta", expand=True)
    audio.add_column("#", style="dim", width=3)
    audio.add_column("Codec")
    audio.add_column("Canaux")
    audio.add_column("Bitrate")
    audio.add_column("Langue")
    audio.add_column("Titre", overflow="fold")
    for i, a in enumerate(info.audio, 1):
        audio.add_row(
            str(i),
            a.codec or "?",
            a.channels or "?",
            _human_bitrate(a.bitrate),
            a.language or "—",
            a.title or "—",
        )
    if not info.audio:
        audio.add_row("—", "aucune piste audio", "", "", "", "")

    subs = Table(title=f"Sous-titres ({len(info.subtitles)})", title_style="bold yellow", expand=True)
    subs.add_column("#", style="dim", width=3)
    subs.add_column("Codec")
    subs.add_column("Langue")
    subs.add_column("Forcé")
    subs.add_column("Titre", overflow="fold")
    for i, s in enumerate(info.subtitles, 1):
        subs.add_row(
            str(i),
            s.codec or "?",
            s.language or "—",
            "oui" if s.forced else "non",
            s.title or "—",
        )
    if not info.subtitles:
        subs.add_row("—", "aucun sous-titre", "", "", "")

    body = Group(header, Text(""), video, Text(""), audio, Text(""), subs)
    return Panel(body, title="[bold]Inspection MediaInfo[/]", border_style="cyan")


def run() -> None:
    ui.clear()
    console.print(ui.info_panel("Inspecter un fichier", "Chemin complet, drag&drop ou Tab pour compléter."))
    raw = questionary.path(
        "Fichier vidéo :",
        only_directories=False,
    ).ask()
    if not raw:
        return

    path = _normalize_path(raw)
    if not path.exists():
        console.print(ui.error_panel("Fichier introuvable", str(path)))
        ui.press_enter()
        return
    if path.is_dir():
        console.print(ui.error_panel("C'est un dossier, pas un fichier", str(path)))
        ui.press_enter()
        return

    try:
        with console.status("[cyan]Analyse mediainfo en cours…[/cyan]", spinner="dots"):
            info = probe(path)
    except MediainfoError as e:
        console.print(ui.error_panel("Erreur mediainfo", str(e)))
        ui.press_enter()
        return

    console.print(_render_panel(info))
    ui.press_enter()

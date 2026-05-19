"""Création de fichiers .torrent avec progress bar + ETA.

Utilise `torf` (pure Python, hashlib en C dessous) qui expose un callback
par pièce hashée. On le branche sur Rich Progress pour un retour visuel
propre avec ETA, vitesse, et % d'avancement.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from torf import ReadError, Torrent, TorfError

from trackr import ui


class TorrentBuildError(RuntimeError):
    pass


@dataclass
class TorrentBuildResult:
    path: Path
    info_hash: str
    piece_size: int
    piece_count: int
    total_size: int


def _format_piece_size(n: int) -> str:
    if n >= 1 << 20:
        return f"{n // (1 << 20)} MiB"
    if n >= 1 << 10:
        return f"{n // (1 << 10)} KiB"
    return f"{n} B"


def create_torrent(
    *,
    source_path: Path,
    announce_url: str,
    output_path: Path,
    source_tag: str = "",
    private: bool = True,
    piece_size: int | None = None,
    include_globs: list[str] | None = None,
    exclude_globs: list[str] | None = None,
    label: str = "Création du .torrent",
) -> TorrentBuildResult:
    """Crée un .torrent depuis `source_path` vers `output_path`.

    Affiche une progress bar Rich pendant le hashing. `piece_size` en bytes
    (laisser None pour auto-pick par torf selon la taille).

    `exclude_globs` retire les fichiers correspondants, `include_globs` les
    ré-inclut (whitelist) — utile pour ne garder que les vidéos d'un dossier
    pollué (artefacts Plex, vignettes, etc.).
    """
    if not source_path.exists():
        raise TorrentBuildError(f"Source introuvable : {source_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        torrent = Torrent(
            path=str(source_path),
            trackers=[announce_url],
            private=private,
            source=source_tag or None,
            created_by="trackr",
            piece_size=piece_size,
            exclude_globs=exclude_globs or [],
            include_globs=include_globs or [],
        )
    except TorfError as e:
        raise TorrentBuildError(f"Init .torrent : {e}") from e

    total_size = torrent.size
    pieces_total = torrent.pieces
    piece_sz = torrent.piece_size

    console = ui.console
    progress = Progress(
        TextColumn("[bold cyan]{task.description}"),
        BarColumn(bar_width=None, complete_style=ui.ACCENT, finished_style=ui.SUCCESS),
        TaskProgressColumn(),
        TextColumn("[dim]·[/dim]"),
        TransferSpeedColumn(),
        TextColumn("[dim]·[/dim]"),
        TimeElapsedColumn(),
        TextColumn("[dim]→[/dim]"),
        TimeRemainingColumn(compact=True),
        console=console,
        transient=False,
    )

    header_lines = [
        f"[dim]Source[/]      {source_path}",
        f"[dim]Pièces[/]      {pieces_total}  ([italic]{_format_piece_size(piece_sz)}/pièce[/italic])",
        f"[dim]Annonce[/]     {announce_url}",
    ]
    if source_tag:
        header_lines.append(f"[dim]Source tag[/]  {source_tag}")
    for line in header_lines:
        console.print(line)
    console.print()

    with progress:
        task = progress.add_task(label, total=total_size)

        def on_progress(t, filepath, pieces_done, pieces_total_cb):
            done_bytes = pieces_done * t.piece_size
            if done_bytes > total_size:
                done_bytes = total_size
            progress.update(task, completed=done_bytes)
            return None

        try:
            ok = torrent.generate(callback=on_progress, interval=0.2)
        except ReadError as e:
            raise TorrentBuildError(f"Lecture source impossible : {e}") from e
        except TorfError as e:
            raise TorrentBuildError(f"Hashing : {e}") from e

        if not ok:
            raise TorrentBuildError("Hashing interrompu")
        progress.update(task, completed=total_size)

    try:
        torrent.write(str(output_path), overwrite=True)
    except (TorfError, OSError) as e:
        raise TorrentBuildError(f"Écriture {output_path} : {e}") from e

    return TorrentBuildResult(
        path=output_path,
        info_hash=torrent.infohash,
        piece_size=piece_sz,
        piece_count=pieces_total,
        total_size=total_size,
    )

"""Reprise des uploads partiellement échoués.

L'user choisit un job en queue, on retente uniquement les trackers `failed`
(les `ok` restent publiés tels quels). Si tous les trackers passent, le job
est retiré de la queue.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import questionary
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from trackr import ui, upload_queue
from trackr.config import Config, load_config
from trackr.session import ensure_torr9_jwt
from trackr.trackers import c411 as c411_api
from trackr.trackers import torr9 as torr9_api
from trackr.trackers.base import AuthError, TrackerError


def run() -> None:
    ui.clear()
    ui.console.print(ui.banner())

    jobs = upload_queue.list_jobs()
    pending = [j for j in jobs if j.failed_trackers]
    if not pending:
        ui.console.print(
            ui.success_panel(
                "File vide",
                "Aucun upload en attente. Tous tes uploads sont passés sans accroc.",
            )
        )
        ui.press_enter()
        return

    ui.console.print(
        Panel(
            f"{len(pending)} upload(s) en attente de reprise.\n"
            f"[{ui.MUTED}][italic]Seuls les trackers en erreur seront retentés. "
            f"Ceux déjà publiés ne sont pas re-uploadés.[/italic][/]",
            title=f"[bold {ui.ACCENT}]Uploads en attente[/]",
            border_style=ui.ACCENT,
        )
    )

    _show_jobs_table(pending)

    cfg = load_config()
    while True:
        choices = [_job_choice(j) for j in pending]
        choices.append(questionary.Choice("← Retour", value="back"))
        action = questionary.select(
            "Quel upload retenter ?",
            choices=choices,
        ).ask()
        if action in (None, "back"):
            return

        job = next((j for j in pending if j.id == action), None)
        if not job:
            return

        _process_job(job, cfg)

        # Refresh : si le job est maintenant "done", on le retire de la liste
        jobs = upload_queue.list_jobs()
        pending = [j for j in jobs if j.failed_trackers]
        if not pending:
            ui.console.print(
                f"\n[{ui.SUCCESS}]✓ Plus rien en attente.[/]"
            )
            ui.press_enter()
            return


def _show_jobs_table(jobs: list[upload_queue.UploadJob]) -> None:
    table = Table.grid(padding=(0, 2), expand=False)
    table.add_column(style=ui.ACCENT, no_wrap=True)
    table.add_column(no_wrap=True)
    table.add_column(style=ui.MUTED, no_wrap=True)
    table.add_column(no_wrap=True)
    table.add_column(style=ui.MUTED, no_wrap=True)
    table.add_row("ID", "Titre", "Date", "Trackers", "Tracker(s) en erreur")
    for j in jobs:
        ok = ", ".join(t.name.upper() for t in j.ok_trackers) or "—"
        ko = ", ".join(t.name.upper() for t in j.failed_trackers) or "—"
        when = _fmt_age(j.updated_at)
        title = (j.release_title or j.tmdb_title)[:54]
        table.add_row(
            j.id[:8],
            title,
            when,
            Text(f"OK: {ok}", style=ui.SUCCESS) if j.ok_trackers else Text("—", style=ui.MUTED),
            Text(ko, style=ui.WARN),
        )
    ui.console.print(table)
    ui.console.print()


def _fmt_age(iso: str) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso[:10]
    delta = datetime.now(timezone.utc) - dt
    s = int(delta.total_seconds())
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}j"


def _job_choice(j: upload_queue.UploadJob) -> questionary.Choice:
    title = (j.release_title or j.tmdb_title)[:50]
    failed = ", ".join(t.name.upper() for t in j.failed_trackers)
    label = f"{j.id[:8]}  ·  {title}  ·  retenter {failed}"
    return questionary.Choice(label, value=j.id)


def _process_job(job: upload_queue.UploadJob, cfg: Config) -> None:
    """Retente chaque tracker `failed` du job et persiste l'état."""
    failed = job.failed_trackers
    if not failed:
        ui.console.print(f"[{ui.SUCCESS}]Rien à retenter sur ce job.[/]")
        return

    if not _job_artifacts_ok(job):
        ui.console.print(
            ui.error_panel(
                "Artefacts manquants",
                f"Le .torrent / NFO du job ont disparu. Supprime le job ou refais "
                f"un upload complet.\n[italic]Job id : {job.id}[/italic]",
            )
        )
        if questionary.confirm("Supprimer ce job de la queue ?", default=False).ask():
            upload_queue.remove(job.id)
        return

    names = [t.name.upper() for t in failed]
    confirm = questionary.confirm(
        f"Retenter {', '.join(names)} maintenant ?",
        default=True,
    ).ask()
    if not confirm:
        return

    for tj in failed:
        ui.console.print()
        ui.console.print(f"[bold {ui.ACCENT}]── Retry {tj.name.upper()} ──[/]")
        ok, message, info_hash, torrent_id, url_hint = _retry_one(tj, job, cfg)
        tj.last_attempt_at = upload_queue.now_iso()
        if ok:
            tj.status = "ok"
            tj.last_error = ""
            tj.info_hash = info_hash or tj.info_hash
            tj.torrent_id = torrent_id or tj.torrent_id
            tj.url_hint = url_hint or tj.url_hint
            ui.console.print(
                Panel(
                    Text.from_markup(
                        f"[bold {ui.SUCCESS}]✓ {tj.name.upper()} OK[/]\n"
                        f"[{ui.MUTED}]{message}[/]\n"
                        f"[{ui.MUTED}]info hash : {tj.info_hash}[/]"
                    ),
                    border_style=ui.SUCCESS,
                )
            )
        else:
            tj.status = "failed"
            tj.last_error = message
            ui.console.print(ui.error_panel(f"{tj.name.upper()} échoue encore", message))
        upload_queue.save(job)

    if job.is_done:
        ui.console.print(
            f"\n[{ui.SUCCESS}]✓ Tous les trackers du job sont OK — retrait de la queue.[/]"
        )
        upload_queue.remove(job.id)
    elif not job.failed_trackers:
        # Edge case : tous rolled_back / pending
        upload_queue.remove(job.id)


def _job_artifacts_ok(job: upload_queue.UploadJob) -> bool:
    """Vérifie que les fichiers nécessaires au retry existent encore sur disque."""
    if job.nfo_path and not Path(job.nfo_path).exists():
        return False
    for tj in job.failed_trackers:
        if not tj.torrent_path or not Path(tj.torrent_path).exists():
            return False
    return True


def _retry_one(
    tj: upload_queue.TrackerJob,
    job: upload_queue.UploadJob,
    cfg: Config,
) -> tuple[bool, str, str, int, str]:
    """Tente un upload pour un tracker. Renvoie (ok, message, info_hash, torrent_id, url_hint)."""
    try:
        with ui.console.status(
            f"[cyan]POST sur {tj.name.upper()}…[/cyan]", spinner="dots"
        ):
            if tj.name == "c411":
                return _retry_c411(tj, job, cfg)
            if tj.name == "torr9":
                return _retry_torr9(tj, job, cfg)
            return (False, f"Tracker inconnu : {tj.name}", "", 0, "")
    except AuthError as e:
        return (False, f"Auth refusée : {e}", "", 0, "")
    except TrackerError as e:
        return (False, f"Échec : {e}", "", 0, "")


def _retry_c411(
    tj: upload_queue.TrackerJob,
    job: upload_queue.UploadJob,
    cfg: Config,
) -> tuple[bool, str, str, int, str]:
    nfo_path = Path(job.nfo_path)
    desc_text = Path(job.description_path).read_text(encoding="utf-8") if job.description_path and Path(job.description_path).exists() else ""
    res = c411_api.upload(
        cfg.c411_api_key,
        torrent_path=Path(tj.torrent_path),
        nfo_path=nfo_path,
        title=tj.title,
        category_id=tj.category_id,
        subcategory_id=tj.subcategory_id,
        description=desc_text,
        options=tj.options,
        tmdb_id=job.tmdb_id,
        tmdb_type=job.tmdb_type or "movie",
        year=job.tmdb_year or "",
    )
    url = f"https://c411.org/torrent/{res.info_hash}" if res.info_hash else ""
    return (True, res.message or "Envoyé.", res.info_hash, 0, url)


def _retry_torr9(
    tj: upload_queue.TrackerJob,
    job: upload_queue.UploadJob,
    cfg: Config,
) -> tuple[bool, str, str, int, str]:
    nfo_text = Path(job.nfo_path).read_text(encoding="utf-8") if job.nfo_path and Path(job.nfo_path).exists() else ""
    desc_text = Path(job.description_path).read_text(encoding="utf-8") if job.description_path and Path(job.description_path).exists() else ""
    jwt = ensure_torr9_jwt(cfg)
    res = torr9_api.upload(
        jwt,
        torrent_path=Path(tj.torrent_path),
        title=tj.title,
        description=desc_text,
        category=tj.category_name,
        subcategory=tj.subcategory_name,
        nfo_text=nfo_text,
        tags=tj.tags,
        tmdb_id=job.tmdb_id,
    )
    url = f"https://torr9.net/torrent/{res.torrent_id}" if res.torrent_id else ""
    return (True, res.message or "Envoyé.", res.info_hash, res.torrent_id, url)

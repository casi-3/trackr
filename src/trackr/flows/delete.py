"""Suppression d'un torrent existant (C411 ou Torr9).

Liste les uploads de l'user via les API de chaque tracker configuré, laisse
choisir un torrent, demande confirmation puis appelle le DELETE approprié.

Note tracker-specific :
- C411 : DELETE accepte info_hash ou id. Requiert la session web (Bearer
  refusé sur cet endpoint).
- Torr9 : DELETE prend un id numérique. Limité à 5 suppressions / jour.
"""

from __future__ import annotations

from dataclasses import dataclass

import questionary
from rich.text import Text

from trackr import ui
from trackr.config import Config, load_config
from trackr.session import ensure_torr9_jwt
from trackr.trackers import c411 as c411_api
from trackr.trackers import torr9 as torr9_api
from trackr.trackers.base import AuthError, TrackerError


@dataclass
class TorrentEntry:
    tracker: str       # "c411" | "torr9"
    title: str
    status: str
    info_hash: str
    torrent_id: int
    created_at: str

    @property
    def delete_identifier(self) -> str:
        if self.tracker == "torr9":
            return str(self.torrent_id) if self.torrent_id else ""
        return self.info_hash


def run() -> None:
    ui.clear()
    ui.console.print(ui.banner())

    cfg = load_config()
    entries: list[TorrentEntry] = []

    if cfg.is_c411_ready():
        try:
            with ui.console.status("[cyan]Liste C411…[/cyan]", spinner="dots"):
                items = c411_api.list_my_uploads(cfg.c411_api_key, cfg.c411_username, limit=30)
            for i in items:
                entries.append(TorrentEntry(
                    tracker="c411",
                    title=str(i.get("title") or i.get("name") or "?"),
                    status=str(i.get("status") or "").lower(),
                    info_hash=str(i.get("infoHash") or i.get("info_hash") or ""),
                    torrent_id=int(i.get("id") or 0),
                    created_at=str(i.get("createdAt") or ""),
                ))
        except (AuthError, TrackerError) as e:
            ui.console.print(ui.warn_panel("C411 indisponible", str(e)))

    if cfg.is_torr9_ready() and cfg.torr9_jwt_valid():
        try:
            with ui.console.status("[cyan]Liste Torr9…[/cyan]", spinner="dots"):
                items = torr9_api.list_my_uploads(cfg.torr9_jwt, limit=30)
            for i in items:
                entries.append(TorrentEntry(
                    tracker="torr9",
                    title=str(i.get("title") or "?"),
                    status=str(i.get("status") or "").lower(),
                    info_hash=str(i.get("info_hash") or ""),
                    torrent_id=int(i.get("id") or 0),
                    created_at=str(i.get("upload_date") or i.get("created_at") or ""),
                ))
        except (AuthError, TrackerError) as e:
            ui.console.print(ui.warn_panel("Torr9 indisponible", str(e)))

    if not entries:
        ui.console.print(ui.info_panel("Rien à afficher", "Aucun upload trouvé sur les trackers configurés."))
        ui.press_enter()
        return

    # Trier desc par date
    entries.sort(key=lambda e: e.created_at or "", reverse=True)

    while True:
        choices = [
            questionary.Choice(_label(e), value=e)
            for e in entries
        ]
        choices.append(questionary.Choice("← Retour", value=None))
        pick = questionary.select(
            "Quel torrent veux-tu supprimer ?",
            choices=choices,
        ).ask()
        if pick is None:
            return

        # Récap + confirmation
        ui.console.print()
        ui.console.print(ui.warn_panel(
            "Suppression",
            f"[bold]{pick.tracker.upper()}[/]  ·  {pick.title}\n"
            f"info_hash : {pick.info_hash or '—'}\n"
            f"status    : {pick.status or '—'}\n\n"
            "[italic]Action irréversible. Le torrent disparaîtra du tracker.[/italic]",
        ))
        if not questionary.confirm("Confirmer la suppression ?", default=False).ask():
            ui.console.print(f"[{ui.MUTED}]Annulé.[/]")
            continue

        if _delete(pick, cfg):
            entries.remove(pick)
            if not entries:
                ui.press_enter()
                return


def _label(e: TorrentEntry) -> str:
    status_marker = {
        "approved": "✓", "active": "✓",
        "pending": "⏳",
        "revision_requested": "🚨", "rejected": "✗",
    }.get(e.status, "·")
    title = e.title[:55] + ("…" if len(e.title) > 55 else "")
    return f"{status_marker} {e.tracker.upper():<6}  {title}"


def _delete(e: TorrentEntry, cfg: Config) -> bool:
    """Appelle l'API DELETE appropriée. True si la suppression est passée."""
    ident = e.delete_identifier
    if not ident:
        ui.console.print(ui.error_panel("Suppression impossible", "Identifiant manquant."))
        return False
    try:
        with ui.console.status(f"[cyan]Suppression {e.tracker.upper()}…[/cyan]", spinner="dots"):
            if e.tracker == "c411":
                if not cfg.c411_session_valid():
                    raise AuthError(
                        "C411 : session web expirée. Reconfigure en mode Guidé "
                        "(le Bearer seul ne permet pas DELETE)."
                    )
                msg = c411_api.delete_torrent(cfg.c411_session, ident)
            elif e.tracker == "torr9":
                jwt = ensure_torr9_jwt(cfg)
                msg = torr9_api.delete_torrent(jwt, int(ident))
            else:
                ui.console.print(ui.error_panel("Tracker inconnu", e.tracker))
                return False
    except AuthError as exc:
        ui.console.print(ui.error_panel(f"Auth refusée ({e.tracker.upper()})", str(exc)))
        return False
    except TrackerError as exc:
        body = str(exc)
        if e.tracker == "torr9" and "limite" in body.lower():
            body += (
                f"\n\n[{ui.MUTED}]Info : Torr9 limite les suppressions à 5/jour. "
                "Tu pourras réessayer demain, ou contacter un modérateur si urgent.[/]"
            )
        ui.console.print(ui.error_panel(f"Suppression {e.tracker.upper()} échouée", body))
        return False

    ui.console.print(ui.success_panel(f"{e.tracker.upper()} supprimé", Text(msg)))
    return True

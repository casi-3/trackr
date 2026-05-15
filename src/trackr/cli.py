from __future__ import annotations

import questionary
import typer
from trackr import __version__, dashboard as dashboard_mod, healthcheck, ui, updater, upload_queue
from trackr.config import load_config
from trackr.flows import configure as configure_flow
from trackr.flows import delete as delete_flow
from trackr.flows import drafts as drafts_flow
from trackr.flows import inspect as inspect_flow
from trackr.flows import game as game_flow
from trackr.flows import movie as movie_flow
from trackr.flows import rejection as rejection_flow
from trackr.flows import retry as retry_flow

app = typer.Typer(
    add_completion=False,
    help="trackr — CLI guidé pour publier sur plusieurs trackers.",
    invoke_without_command=True,
)


# Cache des résultats du healthcheck pour la durée de la session.
# Invalidé après un retour de Configuration (les creds ont pu changer).
_health: list[healthcheck.Check] = []
_health_dirty: bool = True


def _refresh_health_if_needed() -> list[healthcheck.Check]:
    global _health, _health_dirty
    if _health_dirty or not _health:
        cfg = load_config()
        with ui.console.status("[cyan]Vérification des accès…[/cyan]", spinner="dots"):
            _health = healthcheck.run_all(cfg)
        _health_dirty = False
    return _health


def invalidate_health() -> None:
    """Force un re-check au prochain draw de la home (ex: après une reconfig)."""
    global _health_dirty
    _health_dirty = True
    dashboard_mod.invalidate()


def _draw_home() -> None:
    ui.clear()
    ui.console.print(ui.banner())
    # Healthcheck silencieux (refresh auto JWT/SID si besoin)
    _refresh_health_if_needed()
    ui.console.print()
    with ui.console.status("[cyan]Chargement du dashboard…[/cyan]", spinner="dots"):
        dash = dashboard_mod.get(load_config())
    ui.console.print(ui.render_dashboard(dash))
    ui.console.print()


def _main_menu() -> str | None:
    choices = [questionary.Choice("📤  Uploader un torrent", value="upload")]
    pending = upload_queue.pending_count()
    if pending:
        choices.append(
            questionary.Choice(
                f"🎯  Reprendre les uploads en attente ({pending})",
                value="retry",
            )
        )
    # Entrée rejets C411 — conditionnée à la présence de rejets dans le cache dashboard
    dash = dashboard_mod.get(load_config())
    n_rej = len(dash.c411.rejections)
    if n_rej:
        choices.append(
            questionary.Choice(
                f"🚨  Résoudre les rejets C411 ({n_rej})",
                value="rejection",
            )
        )
    # Entrée brouillons C411 — conditionnée à la présence d'au moins un brouillon
    n_drafts = drafts_flow.count()
    if n_drafts:
        choices.append(
            questionary.Choice(
                f"📝  Brouillons C411 ({n_drafts})",
                value="drafts",
            )
        )
    choices += [
        questionary.Choice("🔍  Inspecter un fichier (mediainfo)", value="inspect"),
        questionary.Choice("🗑   Supprimer un torrent", value="delete"),
        questionary.Choice("⚙️   Configuration", value="configure"),
        questionary.Choice("🔄  Re-vérifier les accès", value="recheck"),
        questionary.Choice("⬆️   Vérifier les mises à jour", value="update"),
        questionary.Choice("👋  Quitter", value="quit"),
    ]
    return questionary.select("Que veux-tu faire ?", choices=choices).ask()


def _upload_menu() -> None:
    while True:
        ui.clear()
        category = questionary.select(
            "Pour quelle catégorie ?",
            choices=[
                questionary.Choice("🎬  Films & Vidéos", value="movies"),
                questionary.Choice("🎮  Jeux Vidéo", value="games"),
                questionary.Choice("← Retour", value="back"),
            ],
        ).ask()
        if category in (None, "back"):
            return
        if category == "movies":
            if _movies_submenu():
                return
        elif category == "games":
            if _games_submenu():
                return


def _movies_submenu() -> bool:
    """Renvoie True si un flow a été lancé (→ remonter au menu principal)."""
    ui.clear()
    choice = questionary.select(
        "Type :",
        choices=[
            questionary.Choice("🎬  Un film", value="movie"),
            questionary.Choice("📺  Une série  [bientôt]", value="series", disabled="à venir"),
            questionary.Choice("← Retour", value="back"),
        ],
    ).ask()
    if choice in (None, "back"):
        return False
    if choice == "movie":
        movie_flow.run()
        return True
    return False


def _games_submenu() -> bool:
    """Renvoie True si un flow a été lancé (→ remonter au menu principal)."""
    ui.clear()
    choice = questionary.select(
        "Plateforme :",
        choices=[
            questionary.Choice("🎮  Microsoft (Xbox / 360 / One / SX)", value="microsoft"),
            questionary.Choice("🎮  Sony (PS3 / PS4 / PS5)  [bientôt]", value="sony", disabled="à venir"),
            questionary.Choice("🎮  Nintendo (Switch / Wii / 3DS)  [bientôt]", value="nintendo", disabled="à venir"),
            questionary.Choice("💻  PC (Windows / Linux / MacOS)  [bientôt]", value="pc", disabled="à venir"),
            questionary.Choice("← Retour", value="back"),
        ],
    ).ask()
    if choice in (None, "back"):
        return False
    if choice == "microsoft":
        game_flow.run()
        return True
    return False


def _check_update_blocking() -> None:
    """Check GitHub releases au démarrage. Non-bloquant en cas d'erreur réseau."""
    with ui.console.status("[cyan]Vérification des mises à jour…[/cyan]", spinner="dots"):
        info = updater.check()
    if info is None:
        return
    ui.console.print()
    ui.console.print(
        ui.info_panel(
            f"🚀 Trackr {info.latest_tag} disponible",
            f"Tu utilises [bold]{info.current_version}[/]. Nouvelle version : [bold]{info.latest_version}[/].\n"
            f"[{ui.MUTED}]{info.html_url}[/]",
        )
    )
    mode = updater.detect_install_mode()
    if mode in ("windows-binary", "pip"):
        ui.console.print(f"[{ui.MUTED}]{updater.manual_instructions(info)}[/]")
        questionary.text(
            "Appuie sur Entrée pour continuer sans mettre à jour…",
            default="",
        ).ask()
        return
    if not questionary.confirm("Mettre à jour maintenant ?", default=True).ask():
        return
    try:
        updater.apply_update(info)  # ne revient pas si succès (execv)
    except updater.UpdateError as e:
        ui.console.print(ui.error_panel("Mise à jour échouée", str(e)))


def _loop() -> None:
    _check_update_blocking()
    while True:
        _draw_home()
        action = _main_menu()
        if action in (None, "quit"):
            ui.console.print(f"[{ui.MUTED}]À bientôt.[/]")
            return
        if action == "upload":
            _upload_menu()
        elif action == "retry":
            retry_flow.run()
        elif action == "rejection":
            rejection_flow.run()
            dashboard_mod.invalidate()  # rafraîchir après actions
        elif action == "drafts":
            drafts_flow.run()
            dashboard_mod.invalidate()
        elif action == "inspect":
            inspect_flow.run()
        elif action == "delete":
            delete_flow.run()
            dashboard_mod.invalidate()
        elif action == "configure":
            configure_flow.run()
            invalidate_health()
        elif action == "recheck":
            invalidate_health()
        elif action == "update":
            _check_update_blocking()


@app.callback()
def root(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", "-V", help="Affiche la version et quitte."),
) -> None:
    if version:
        ui.console.print(f"trackr {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        try:
            _loop()
        except KeyboardInterrupt:
            ui.console.print(f"\n[{ui.MUTED}]Interrompu.[/]")


def main() -> None:
    app()


if __name__ == "__main__":
    main()

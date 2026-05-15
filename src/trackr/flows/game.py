"""Flow d'upload Jeux Vidéo, sous-catégorie Microsoft.

v1 : Xbox / Xbox 360 / Xbox One / Xbox Series X|S.
Recherche automatique de métadonnées (RAWG.io) pour pré-remplir
présentation, genres et éditeur/développeur.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import questionary
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from trackr import pending as pending_mod
from trackr import qbittorrent as qbt
from trackr import ui
from trackr.config import Config, load_config, save_config
from trackr.media import imagehost
from trackr.media import torrent as torrent_mod
from trackr.nfo.builder import (
    _size_with_bytes,
    build_game_description_bbcode,
    build_game_nfo_c411,
    build_game_title_c411,
    slugify,
)
from trackr.session import ensure_torr9_jwt
from trackr.trackers import c411 as c411_api
from trackr.trackers import torr9 as torr9_api
from trackr.trackers import torr9_cats
from trackr.trackers.c411_cats import (
    GAME_LANGUAGES,
    GAME_REGIONS,
    MICROSOFT_CONSOLES,
    Console as ConsoleSpec,
    games_category,
)
from trackr.trackers.base import AuthError, TrackerError
from trackr.trackers.c411 import QuotaError


# Mapping langue choisie → slugs C411 (option_id=1, cat=5).
LANGUAGE_OPTION_HINTS = {
    "FR":    ("vff", "vff-uniquement", "francais-uniquement", "francais"),
    "EN":    ("anglais", "vo-anglais", "vo"),
    "JP":    ("japonais", "vo-japonais"),
    "MULTI": ("multi-francais-inclus", "multi", "multi-langue"),
}


@dataclass
class GamePlan:
    """Plan d'upload pour un tracker donné."""
    tracker: str              # "c411" | "torr9"
    title: str
    category_id: int
    category_name: str
    subcategory_id: int
    subcategory_name: str
    announce_url: str
    source_tag: str
    description: str
    nfo_text: str = ""
    options: dict = field(default_factory=dict)   # C411
    tags: list[str] = field(default_factory=list)  # Torr9
    rawg_data: dict = field(default_factory=dict)  # C411 only
    torrent_path: Path | None = None
    info_hash: str = ""
    total_size: int = 0


@dataclass
class PostResult:
    tracker: str
    ok: bool
    message: str
    info_hash: str = ""
    status: str = ""
    url_hint: str = ""
    torrent_id: int = 0                   # Torr9 expose un id numérique pour DL
    tracker_torrent_path: Path | None = None  # .torrent re-signé par le tracker


# ─────────────────────────── entrypoint ───────────────────────────


def run() -> None:
    ui.clear()
    ui.console.print(
        Panel(
            "Flow guidé d'upload d'un jeu vidéo Microsoft.\n"
            f"[{ui.WARN}]POST réel après confirmation explicite.[/]\n"
            f"[{ui.MUTED}]v1 : Xbox / Xbox 360 / Xbox One / Xbox Series X|S — C411 et/ou Torr9.[/]",
            title=f"[bold {ui.ACCENT}]🎮 Uploader un jeu (Microsoft)[/]",
            border_style=ui.ACCENT,
        )
    )

    cfg = load_config()
    available: list[str] = []
    if cfg.is_c411_ready():
        available.append("c411")
    if cfg.is_torr9_ready():
        available.append("torr9")
    if not available:
        ui.console.print(
            ui.error_panel(
                "Aucun tracker configuré",
                "Configure C411 et/ou Torr9 dans Configuration avant d'uploader.",
            )
        )
        ui.press_enter()
        return

    # 1. Trackers cibles
    targets = _ask_targets(available)
    if not targets:
        return

    # Recherche automatique de métadonnées : disponible dès que le back est utilisable.
    # Indépendante des trackers ciblés — sert aussi à enrichir un upload Torr9-only.
    auto_lookup_enabled = cfg.is_c411_ready() and cfg.c411_session_valid()
    if not auto_lookup_enabled:
        ui.console.print(
            ui.warn_panel(
                "Recherche automatique non disponible",
                "La recherche et la présentation seront saisies manuellement.\n"
                f"[{ui.MUTED}]Tu pourras quand même uploader, l'enrichissement automatique est juste désactivé.[/]",
            )
        )

    # 2. Console / nom / conteneur / région / langue / version
    console = _ask_console()
    if console is None:
        return
    name = questionary.text(
        "Nom du jeu (sans tag console, sans région) :",
        validate=lambda v: True if v.strip() else "Nom requis.",
    ).ask()
    if not name:
        return
    container = _ask_container(console)
    if not container:
        return
    region = _ask_pick("Région :", GAME_REGIONS, default_key="PAL")
    if not region:
        return
    language = _ask_pick("Langue :", GAME_LANGUAGES, default_key="MULTI")
    if not language:
        return
    version = questionary.text(
        "Version (optionnel, ex: 'v1.0', vide pour rien) :",
        default="",
    ).ask()
    if version is None:
        return

    # 3. Source : fichier ou dossier
    source_path = _ask_source_path()
    if source_path is None:
        return

    # 4. Recherche automatique de métadonnées (présentation + genres)
    rawg_data: dict = {}
    presentation_html = ""
    presentation_bbcode = ""
    genre_option_ids: list[int] = []
    rawg_screens: list[str] = []
    if auto_lookup_enabled:
        results: list[c411_api.RawgResult] = []
        try:
            results = _rawg_search_step(cfg.c411_session, name)
        except (AuthError, TrackerError):
            ui.console.print(
                ui.warn_panel(
                    "Recherche automatique non disponible",
                    "La présentation sera saisie manuellement (laisse vide si tu n'en as pas).",
                )
            )
        rawg_choice = _ask_rawg_choice(results)
        if rawg_choice is None:
            return
        if rawg_choice != "_skip":
            time.sleep(0.3)
            rawg_screens = list(rawg_choice.screenshots)
            official_name = str(rawg_choice.title or "").strip()
            try:
                lookup = c411_api.rawg_lookup(cfg.c411_session, rawg_choice.rawg_id, presentation=True)
                rawg_data = lookup.game
                presentation_html = lookup.presentation_html
                presentation_bbcode = _html_to_bbcode(presentation_html)
                genre_option_ids = lookup.genre_option_ids
                official_name = (
                    str(rawg_data.get("title") or rawg_data.get("name") or "").strip()
                    or official_name
                )
            except (AuthError, TrackerError):
                ui.console.print(
                    ui.warn_panel(
                        "Détail non disponible",
                        "Continue sans présentation auto.",
                    )
                )
            if official_name and official_name != name:
                ui.console.print(
                    f"[{ui.MUTED}]Nom officiel retenu : [bold]{official_name}[/] "
                    f"(saisi : « {name} »)[/]"
                )
                name = official_name

    # 5. Screenshots (commun)
    screenshots = _ask_screenshots(rawg_screens, cfg)
    if screenshots is None:
        return

    # 6. Notes (commun) — pré-remplies selon console + conteneur, l'user édite si besoin
    default_config = _default_config_requirements(console, container)
    default_install = _default_install_notes(console, container)
    ui.console.print(
        f"[{ui.MUTED}]Les champs suivants sont pré-remplis selon ta console et ton conteneur. "
        f"Édite, complète, ou laisse tel quel.[/]"
    )
    config_min = questionary.text(
        "Configuration / compatibilité (optionnel, multi-ligne) :",
        default=default_config,
        multiline=True,
    ).ask()
    if config_min is None:
        return
    install_notes = questionary.text(
        "Notes d'installation (optionnel, multi-ligne) :",
        default=default_install,
        multiline=True,
    ).ask()
    if install_notes is None:
        return

    # 7. Titre commun
    title = build_game_title_c411(
        console_tag=console.title_tag,
        name=name,
        region=region,
        language=language,
        container=container,
        version=version,
    )

    # 8. NFO commun (texte brut, plate-forme agnostique)
    out_dir = _build_dir(name)
    file_count_provisional = 0  # rempli après mktorrent du premier plan

    # 9. Plans par tracker (sans torrent_path / info_hash / total_size encore)
    plans: list[GamePlan] = []
    if "c411" in targets:
        plans.append(_make_plan_c411(cfg, title, console, region, language, container, rawg_data))
    if "torr9" in targets:
        plans.append(_make_plan_torr9(cfg, title, console, region, language, container))

    # 10. mktorrent par plan (chacun son announce + son source tag)
    for plan in plans:
        ui.console.print()
        torrent_out = out_dir / f"{plan.tracker}.torrent"
        try:
            built = torrent_mod.create_torrent(
                source_path=source_path,
                announce_url=plan.announce_url,
                output_path=torrent_out,
                source_tag=plan.source_tag,
                private=True,
                label=f"{plan.tracker}: hashing",
            )
        except torrent_mod.TorrentBuildError as e:
            ui.console.print(ui.error_panel(f".torrent {plan.tracker} échoué", str(e)))
            ui.press_enter()
            return
        plan.torrent_path = built.path
        plan.info_hash = built.info_hash
        plan.total_size = built.total_size
        if file_count_provisional == 0:
            file_count_provisional = (
                1 if source_path.is_file()
                else sum(1 for p in source_path.rglob("*") if p.is_file())
            )

    total_size = plans[0].total_size
    file_count = file_count_provisional

    # 11. NFO (commun aux deux trackers, même contenu texte)
    nfo_text = build_game_nfo_c411(
        name=name.strip(),
        platform=console.label,
        publisher=_join_strs(rawg_data.get("publishers"), 3),
        developer=_join_strs(rawg_data.get("developers"), 3),
        genre=_join_strs(rawg_data.get("genres"), 3),
        release_date=str(rawg_data.get("releaseDate") or rawg_data.get("year") or ""),
        region=region,
        language=language,
        container=container,
        file_count=file_count,
        total_size=total_size,
        synopsis=_clean_synopsis(rawg_data.get("description") or ""),
        config_required=config_min,
        install=install_notes,
    )
    nfo_path = out_dir / "release.nfo"
    nfo_path.write_text(nfo_text, encoding="utf-8")

    # 12. Descriptions par tracker — BBCode reconstruit depuis rawg_data (pas
    # de conversion HTML → balises propres, imbrication stricte).
    pres_c411 = _build_presentation_bbcode(
        rawg_data, console=console, region=region, language=language,
        container=container, version=version, title=title, total_size=total_size,
        file_count=file_count, keep_internal_links=True,
    )
    pres_torr9 = _build_presentation_bbcode(
        rawg_data, console=console, region=region, language=language,
        container=container, version=version, title=title, total_size=total_size,
        file_count=file_count, keep_internal_links=False,
    )
    desc_c411 = build_game_description_bbcode(
        presentation=pres_c411,
        screenshots=screenshots,
        config_min=config_min,
        install_notes=install_notes,
    )
    desc_torr9 = build_game_description_bbcode(
        presentation=pres_torr9,
        screenshots=screenshots,
        config_min=config_min,
        install_notes=install_notes,
    )

    for plan in plans:
        plan.nfo_text = nfo_text
        plan.description = desc_c411 if plan.tracker == "c411" else desc_torr9

    # 13. Options C411 (cat 5 + sous-cat 31 fusionnées, langue requise + genres pré-cochés)
    c411_plan = next((p for p in plans if p.tracker == "c411"), None)
    if c411_plan is not None:
        try:
            c411_plan.options = _resolve_c411_options(
                cfg, c411_plan.subcategory_id, language, genre_option_ids,
                console_key=console.key,
            )
        except TrackerError as e:
            ui.console.print(
                ui.warn_panel(
                    "Options C411 non résolues",
                    f"{e}\nL'upload partira avec options vides — risque de revision_requested si la langue est manquante.",
                )
            )
            c411_plan.options = {}

    # 14. Tags Torr9 (libres)
    torr9_plan = next((p for p in plans if p.tracker == "torr9"), None)
    if torr9_plan is not None:
        tags = [console.label.split(" ")[0], region, language, container]
        torr9_plan.tags = [t for t in tags if t]

    # 15. Persistance manifest
    desc_path = out_dir / "release.description.bbcode"
    desc_path.write_text(desc_c411 or desc_torr9, encoding="utf-8")
    manifest_path = _write_manifest(
        out_dir, plans, source_path, name, console, region, language, container, version,
        rawg_data, nfo_path, desc_path,
    )

    # 16. Preview
    _show_preview(plans, manifest_path)

    # 17. Confirmation
    ui.console.print()
    labels = " + ".join(p.tracker.upper() for p in plans)
    if not questionary.confirm(f"Publier maintenant sur {labels} ?", default=False).ask():
        ui.console.print(f"[{ui.MUTED}]Pas de POST. Les builds restent dans {out_dir}.[/]")
        ui.press_enter()
        return

    # 18. POST séquentiel + DL du .torrent re-signé par chaque tracker
    # Note : un torrent qui bascule en draft (status="draft") n'a pas encore
    # été accepté par le tracker — pas de fetch du .torrent signé, pas de seed.
    results: list[PostResult] = []
    for plan in plans:
        if plan.tracker == "c411":
            r = _post_c411(plan, cfg)
        else:
            r = _post_torr9(plan, cfg)
        if r.ok and plan.torrent_path and r.status != "draft":
            _fetch_tracker_torrent_game(plan, r, cfg)
        results.append(r)
        _print_post_result(r)

    # 19. Tracking pending C411 (dashboard) — exclure les drafts (pas en pending serveur)
    for plan, res in zip(plans, results):
        if plan.tracker == "c411" and res.ok and res.info_hash and res.status != "draft":
            pending_mod.add("c411", res.info_hash, plan.title)

    # 20. Seed dans qBittorrent — exclure les drafts (seed proposé à la publication ultérieure)
    drafted = [p for p, r in zip(plans, results) if r.status == "draft"]
    if drafted:
        names = ", ".join(p.tracker.upper() for p in drafted)
        ui.console.print(
            ui.info_panel(
                "Seed différé pour les brouillons",
                f"{names} : le torrent est en brouillon, le seed sera proposé "
                f"automatiquement quand tu publieras le brouillon depuis le menu "
                f"[b]📝 Brouillons C411[/].",
            )
        )
    _offer_seed_game(plans, results, cfg, source_path)

    # 21. Récap
    ui.console.print()
    ui.console.print(
        f"[{ui.MUTED}]Artefacts : {out_dir}[/]\n"
        f"[{ui.MUTED}]Source : {source_path}[/]"
    )
    ui.press_enter()


# ─────────────────────────── steps ───────────────────────────


def _ask_targets(available: list[str]) -> list[str]:
    if len(available) == 1:
        ui.console.print(f"[{ui.MUTED}]Seul {available[0].upper()} est configuré — sélection automatique.[/]")
        return list(available)
    choices = []
    for t in available:
        choices.append(questionary.Choice(t.upper(), value=t, checked=True))
    pick = questionary.checkbox(
        "Trackers cibles (espace pour cocher/décocher, entrée pour valider) :",
        choices=choices,
    ).ask()
    return pick or []


def _ask_console() -> ConsoleSpec | None:
    choices = [questionary.Choice(c.label, value=c) for c in MICROSOFT_CONSOLES]
    choices.append(questionary.Choice("← Annuler", value=None))
    return questionary.select("Console :", choices=choices).ask()


def _ask_container(console: ConsoleSpec) -> str:
    if len(console.containers) == 1:
        c = console.containers[0]
        ui.console.print(f"[{ui.MUTED}]Conteneur imposé : [bold]{c}[/].[/]")
        return c
    choices = [questionary.Choice(c, value=c) for c in console.containers]
    return questionary.select(
        f"Conteneur pour {console.label} :",
        choices=choices,
        default=console.containers[0],
    ).ask() or ""


def _ask_pick(label: str, entries, *, default_key: str) -> str:
    choices = [questionary.Choice(text, value=key) for (key, text) in entries]
    return questionary.select(label, choices=choices, default=default_key).ask() or ""


def _ask_source_path() -> Path | None:
    raw = questionary.path("Fichier ou dossier source (ISO/XVC/dossier) :", only_directories=False).ask()
    if not raw:
        return None
    p = Path(raw.strip().strip("'\"")).expanduser()
    if not p.exists():
        ui.console.print(ui.error_panel("Source introuvable", str(p)))
        ui.press_enter()
        return None
    return p


def _rawg_search_step(session_cookie: str, name: str) -> list[c411_api.RawgResult]:
    with ui.console.status(f"[cyan]Recherche en cours…[/cyan]", spinner="dots"):
        return c411_api.rawg_search(session_cookie, name, limit=10)


def _ask_rawg_choice(results: list[c411_api.RawgResult]):
    if not results:
        ui.console.print(
            ui.warn_panel(
                "Aucun résultat",
                "Continue sans présentation auto et sans pré-cochage des genres.",
            )
        )
        if questionary.confirm("Continuer sans présentation auto ?", default=True).ask():
            return "_skip"
        return None
    table = Table(show_header=True, header_style=f"bold {ui.ACCENT}", expand=True)
    table.add_column("#", style="dim", width=3)
    table.add_column("Titre")
    table.add_column("Année", width=6)
    table.add_column("Plateformes", overflow="fold")
    for i, r in enumerate(results, 1):
        table.add_row(str(i), r.title, r.year or "?", ", ".join(r.platforms[:4]))
    ui.console.print(table)
    choices = [questionary.Choice(f"{i}. {r.title} ({r.year or '?'})", value=r) for i, r in enumerate(results, 1)]
    choices.append(questionary.Choice("Continuer sans présentation auto", value="_skip"))
    choices.append(questionary.Choice("← Annuler", value=None))
    return questionary.select("Sélection du jeu :", choices=choices).ask()


def _ask_screenshots(rawg_screens: list[str], cfg: Config) -> list[str] | None:
    if rawg_screens:
        picked = _grab_rawg_screenshots(rawg_screens, cfg)
        if picked is None:
            return None
        if picked:
            return picked
    return _ask_screenshots_manual()


def _grab_rawg_screenshots(rawg_screens: list[str], cfg: Config) -> list[str] | None:
    """Récupère des screenshots directement depuis RAWG.

    Renvoie la liste finale, [] si l'user préfère la saisie manuelle,
    None si annulation.
    """
    avail = len(rawg_screens)
    ui.console.print(
        ui.info_panel(
            "Screenshots RAWG",
            f"{avail} capture(s) disponible(s) directement depuis RAWG.",
        )
    )
    if not questionary.confirm(
        "Récupérer les screenshots automatiquement depuis RAWG ?", default=True
    ).ask():
        return []

    default_n = min(3, avail)
    raw_n = questionary.text(
        f"Combien de screenshots ? (1–{avail}, défaut {default_n})",
        default=str(default_n),
    ).ask()
    if raw_n is None:
        return None
    try:
        n = int(str(raw_n).strip() or default_n)
    except ValueError:
        n = default_n
    n = max(1, min(n, avail))
    picked = rawg_screens[:n]

    ui.console.print(f"\n[{ui.MUTED}]{n} screenshot(s) sélectionné(s) :[/]")
    for i, u in enumerate(picked, 1):
        ui.console.print(f"  [dim]{i}.[/] {u}")
    ui.console.print()

    # Mode d'hébergement : lien RAWG direct ou réupload vers un host.
    mode = cfg.default_screen_host
    if mode == "ask":
        mode = questionary.select(
            "Hébergement des screenshots :",
            choices=[
                questionary.Choice("Réuploader sur Catbox (recommandé)", value="catbox"),
                questionary.Choice("Garder les liens RAWG directs", value="direct"),
            ],
        ).ask()
        if mode is None:
            return None

    if mode in ("direct", "rawg"):
        ui.console.print(f"[{ui.MUTED}]{n} lien(s) RAWG direct(s) utilisé(s).[/]")
        return picked

    host = "catbox" if mode not in ("catbox",) else mode
    if mode not in ("catbox",):
        ui.console.print(
            f"[{ui.MUTED}]Réupload via Catbox (host « {mode} » non géré pour le réupload auto).[/]"
        )

    final: list[str] = []
    n_fail = 0
    with ui.console.status(
        f"[cyan]Réupload de {n} screenshot(s) sur {host}…[/cyan]", spinner="dots"
    ):
        results = imagehost.rehost_many(picked, host=host)
    for i, r in enumerate(results, 1):
        final.append(r.url)
        if r.rehosted:
            ui.console.print(f"[{ui.SUCCESS}]✓[/] screenshot {i} → {r.url}")
        else:
            n_fail += 1
            ui.console.print(
                f"[{ui.WARN}]⚠[/] screenshot {i} : réupload échoué ({r.error}) — lien RAWG conservé"
            )
    if n_fail:
        ui.console.print(
            f"[{ui.WARN}]{n_fail}/{n} non réuploadé(s) — les liens RAWG d'origine sont utilisés.[/]"
        )
    return final


def _ask_screenshots_manual() -> list[str] | None:
    ui.console.print(
        ui.info_panel(
            "Screenshots",
            "Colle les URLs directes (3 minimum). Une par ligne, vide pour terminer.\n"
            f"[{ui.MUTED}]Host stable conseillé (catbox, imgbb, postimg).[/]",
        )
    )
    urls: list[str] = []
    while True:
        u = questionary.text(f"URL screenshot #{len(urls) + 1} (vide = terminer) :", default="").ask()
        if u is None:
            return None
        u = u.strip()
        if not u:
            if len(urls) < 3:
                ui.console.print(f"[{ui.WARN}]Il en faut au moins 3 (tu as {len(urls)}).[/]")
                continue
            return urls
        if not u.startswith(("http://", "https://")):
            ui.console.print(f"[{ui.WARN}]URL invalide.[/]")
            continue
        urls.append(u)


# ─────────────────────────── plans ───────────────────────────


def _make_plan_c411(
    cfg: Config,
    title: str,
    console: ConsoleSpec,
    region: str,
    language: str,
    container: str,
    rawg_data: dict,
) -> GamePlan:
    cat = games_category()
    return GamePlan(
        tracker="c411",
        title=title,
        category_id=cat.id,
        category_name=cat.name,
        subcategory_id=31,
        subcategory_name="Microsoft",
        announce_url=f"https://c411.org/announce/{cfg.c411_passkey}",
        source_tag="c411",
        description="",
        rawg_data=rawg_data,
    )


def _make_plan_torr9(
    cfg: Config,
    title: str,
    console: ConsoleSpec,
    region: str,
    language: str,
    container: str,
) -> GamePlan:
    cat = torr9_cats.find_cat("game")
    if cat is None:
        raise RuntimeError("Torr9 : catégorie 'game' introuvable dans le catalogue.")
    sub = torr9_cats.find_subcat(cat, "g-microsoft")
    if sub is None:
        raise RuntimeError("Torr9 : sous-catégorie Microsoft introuvable.")
    return GamePlan(
        tracker="torr9",
        title=title,
        category_id=cat.id,
        category_name=cat.name,
        subcategory_id=sub.id,
        subcategory_name=sub.name,
        announce_url=f"https://tracker.torr9.net/announce/{cfg.torr9_passkey}",
        source_tag="Torr9",  # casse serveur — matche info_hash sans re-DL
        description="",
    )


# Mapping console interne → label exact attendu côté C411 (option "Console Microsoft").
# Le label doit matcher ce que renvoie l'API ; sinon le fallback prompt prendra le relais.
CONSOLE_OPTION_LABELS = {
    "xbox":    ("Xbox", "Xbox originale", "Xbox (originale)"),
    "xbox360": ("Xbox 360",),
    "xone":    ("Xbox One",),
    "xsx":     ("Xbox Series X/S", "Xbox Series X|S", "Xbox Series X", "XSX"),
}


def _resolve_c411_options(cfg: Config, subcategory_id: int, language: str, genre_option_ids: list[int], console_key: str = "") -> dict:
    """Résout les options C411 (cat=5 ET sous-cat=subcategory_id) pour un jeu.

    Les options peuvent être déclarées au niveau catégorie OU au niveau
    sous-catégorie. On récupère les deux et on fusionne par id.

    - id=1 (langue) : auto depuis le pick langue
    - id=5 (genre)  : pré-coché depuis RAWG si dispo
    - autres options `isRequired` : prompt user
    - options optionnelles : ignorées
    """
    opt_defs: list[dict] = []
    seen_ids: set = set()
    for cat_or_sub in (5, subcategory_id):
        try:
            defs = c411_api.get_subcategory_options(cfg.c411_api_key, cat_or_sub)
        except (AuthError, TrackerError):
            defs = []
        for opt in defs:
            oid = int(opt.get("id") or 0)
            if oid and oid not in seen_ids:
                seen_ids.add(oid)
                opt_defs.append(opt)

    if not opt_defs:
        raise TrackerError("Aucune option C411 récupérée (cat 5 ni sous-cat).")

    # Dump pour visibilité — utile pour diagnostiquer les options inconnues (ex: ID 24)
    summary_rows = []
    for o in opt_defs:
        oid = o.get("id")
        name = o.get("name") or o.get("slug") or "?"
        req = "obligatoire" if (o.get("isRequired") or o.get("required")) else "optionnel"
        n_vals = len(o.get("values") or [])
        summary_rows.append(f"  • id={oid:<3} {name:<20} [{req}, {n_vals} valeur(s)]")
    if summary_rows:
        ui.console.print(
            ui.info_panel(
                f"Options C411 récupérées ({len(opt_defs)})",
                "\n".join(summary_rows),
            )
        )

    options: dict = {}

    for opt in opt_defs:
        opt_id = int(opt.get("id") or 0)
        slug = str(opt.get("slug") or "").lower()
        is_required = bool(opt.get("isRequired") or opt.get("required"))
        multi = bool(opt.get("allowsMultiple") or opt.get("multi"))
        values = opt.get("values") or []

        # 1) Langue — résolution auto par mapping
        if opt_id == 1 or slug == "langue":
            vid = _match_language_value(values, language)
            if vid is not None:
                options[opt_id] = [vid] if multi else vid
                continue
            if is_required:
                vid = _prompt_option(opt, language_hint=language)
                if vid is None:
                    raise TrackerError(f"Option obligatoire '{opt.get('name') or slug}' non remplie.")
                options[opt_id] = [vid] if multi else vid
            continue

        # 2) Genre — pré-cochage RAWG
        if opt_id == 5 or slug == "genre":
            if genre_option_ids:
                options[opt_id] = list(genre_option_ids)
                continue
            if is_required:
                vid = _prompt_option(opt)
                if vid is None:
                    raise TrackerError(f"Option obligatoire '{opt.get('name') or slug}' non remplie.")
                options[opt_id] = [vid] if multi else vid
            continue

        # 3) Console (sous-cat Microsoft) — auto depuis le choix console
        name_lower = str(opt.get("name") or "").lower()
        if "console" in slug or "console" in name_lower:
            wanted_labels = CONSOLE_OPTION_LABELS.get(console_key.lower(), ())
            vid = _match_value_by_label(values, wanted_labels)
            if vid is not None:
                options[opt_id] = [vid] if multi else vid
                continue
            if is_required:
                vid = _prompt_option(opt)
                if vid is None:
                    raise TrackerError(f"Option obligatoire '{opt.get('name') or slug}' non remplie.")
                options[opt_id] = [vid] if multi else vid
            continue

        # 4) Autres options requises → prompt user
        if is_required:
            vid = _prompt_option(opt)
            if vid is None:
                raise TrackerError(f"Option obligatoire '{opt.get('name') or slug}' non remplie.")
            options[opt_id] = [vid] if multi else vid

    return options


def _match_value_by_label(values: list, wanted_labels: tuple) -> int | None:
    """Trouve l'ID de la valeur dont le label/name matche un des candidats (insensible casse)."""
    if not wanted_labels:
        return None
    wl = tuple(w.lower() for w in wanted_labels)
    for v in values:
        label = str(v.get("label") or v.get("name") or v.get("value") or "").lower()
        if label in wl:
            vid = v.get("id") or v.get("value")
            return int(vid) if str(vid).isdigit() else vid
    # 2nd passe : substring sur le label (ex: "Xbox 360" trouvé dans "Xbox 360 (PAL)")
    for v in values:
        label = str(v.get("label") or v.get("name") or v.get("value") or "").lower()
        for w in wl:
            if w in label:
                vid = v.get("id") or v.get("value")
                return int(vid) if str(vid).isdigit() else vid
    return None


def _match_language_value(values: list, language: str):
    """Trouve l'ID de valeur correspondant à la langue choisie."""
    wanted = LANGUAGE_OPTION_HINTS.get(language.upper(), ())
    # Match exact sur slug/value
    for v in values:
        slug = str(v.get("slug") or v.get("value") or "").lower()
        if slug in wanted:
            vid = v.get("id") or v.get("value")
            return int(vid) if str(vid).isdigit() else vid
    # Substring sur label
    if wanted:
        for v in values:
            label = str(v.get("label") or v.get("name") or "").lower()
            if any(w in label for w in wanted):
                vid = v.get("id") or v.get("value")
                return int(vid) if str(vid).isdigit() else vid
    return None


def _prompt_option(opt: dict, language_hint: str = ""):
    """Demande à l'user de choisir une valeur pour une option C411."""
    name = opt.get("name") or opt.get("slug") or "Option"
    values = opt.get("values") or []
    if not values:
        ui.console.print(
            ui.warn_panel(
                f"Option '{name}' sans valeurs",
                "L'API n'a fourni aucune valeur sélectionnable pour cette option.",
            )
        )
        return None
    hint = f" (suggestion : {language_hint})" if language_hint else ""
    ui.console.print(
        ui.info_panel(
            f"Option obligatoire : {name}{hint}",
            "Cette option est requise par C411 et n'a pas pu être déduite automatiquement.",
        )
    )
    choices = []
    for v in values:
        label = str(v.get("label") or v.get("name") or v.get("value") or v.get("slug") or "?")
        vid = v.get("id") or v.get("value")
        choices.append(questionary.Choice(label, value=vid))
    choices.append(questionary.Choice("← Annuler", value=None))
    pick = questionary.select(f"{name} :", choices=choices).ask()
    if pick is None or pick == "":
        return None
    return int(pick) if str(pick).isdigit() else pick


# ─────────────────────────── POST ───────────────────────────


def _post_c411(plan: GamePlan, cfg: Config) -> PostResult:
    nfo_path = plan.torrent_path.parent / "release.nfo" if plan.torrent_path else None
    if not plan.torrent_path or not nfo_path or not nfo_path.exists():
        return PostResult("c411", False, "Fichiers .torrent/.nfo manquants")
    try:
        with ui.console.status("[cyan]POST sur C411…[/cyan]", spinner="dots"):
            res = c411_api.upload(
                cfg.c411_api_key,
                torrent_path=plan.torrent_path,
                nfo_path=nfo_path,
                title=plan.title,
                category_id=plan.category_id,
                subcategory_id=plan.subcategory_id,
                description=plan.description,
                description_format="standard",
                options=plan.options,
                rawg_data=plan.rawg_data,
            )
    except AuthError as e:
        return PostResult("c411", False, f"Auth refusée : {e}")
    except QuotaError as e:
        # Quota d'uploads en attente atteint → fallback vers brouillon
        return _fallback_to_draft(plan, cfg, nfo_path, str(e))
    except TrackerError as e:
        return PostResult("c411", False, f"Échec : {e}")
    url_hint = f"https://c411.org/torrent/{res.info_hash}" if res.info_hash else ""
    return PostResult("c411", True, res.message or "Envoyé.",
                      info_hash=res.info_hash, status=res.status, url_hint=url_hint)


def _fallback_to_draft(plan: GamePlan, cfg: Config, nfo_path: Path, quota_msg: str) -> PostResult:
    """Quand le POST upload renvoie 429 (quota pending atteint), sauve en brouillon.

    L'user pourra reprendre via le menu Brouillons C411 quand un slot se libère.
    """
    ui.console.print()
    ui.console.print(
        ui.warn_panel(
            "Quota d'uploads en attente atteint",
            f"{quota_msg}\n\n"
            f"[bold]Trackr va sauver ce torrent en [b]brouillon[/b] côté C411.[/]\n"
            f"Tu pourras le publier depuis le menu [b]Brouillons C411[/] dès qu'un slot "
            f"d'upload se libère (validation d'un autre torrent par la Team Pending).",
        )
    )
    try:
        with ui.console.status("[cyan]Sauvegarde du brouillon C411…[/cyan]", spinner="dots"):
            draft = c411_api.create_draft(
                cfg.c411_api_key,
                torrent_path=plan.torrent_path,
                nfo_path=nfo_path,
                title=plan.title,
                category_id=plan.category_id,
                subcategory_id=plan.subcategory_id,
                description=plan.description,
                description_format="standard",
                options=plan.options,
                rawg_data=plan.rawg_data,
            )
    except QuotaError as e:
        return PostResult("c411", False,
                          f"Brouillon refusé : {e}\nLimite brouillons atteinte (15 max). "
                          f"Supprime un brouillon existant depuis le menu Brouillons C411.")
    except AuthError as e:
        return PostResult("c411", False, f"Brouillon refusé (auth) : {e}")
    except TrackerError as e:
        return PostResult("c411", False, f"Brouillon échoué : {e}")
    # Conserve le draft_id dans le manifest local pour permettre la reprise
    # depuis le menu Brouillons (lookup cache_dir ↔ draft).
    _attach_draft_id_to_manifest(plan, draft.id)
    return PostResult(
        "c411", True,
        f"Sauvé en brouillon #{draft.id} — à publier depuis le menu Brouillons C411 quand un slot se libère.",
        status="draft",
        url_hint=f"https://c411.org/user/drafts",
    )


def _attach_draft_id_to_manifest(plan: GamePlan, draft_id: int) -> None:
    """Met à jour le manifest.json du build pour y stocker le draft_id C411."""
    if not plan.torrent_path:
        return
    manifest_path = plan.torrent_path.parent / "manifest.json"
    if not manifest_path.exists():
        return
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        for p in data.get("plans", []):
            if p.get("tracker") == "c411":
                p["c411_draft_id"] = draft_id
        manifest_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, json.JSONDecodeError):
        pass


def _post_torr9(plan: GamePlan, cfg: Config) -> PostResult:
    if not plan.torrent_path:
        return PostResult("torr9", False, "Fichier .torrent manquant")
    try:
        jwt = ensure_torr9_jwt(cfg)
    except (AuthError, TrackerError) as e:
        return PostResult("torr9", False, f"JWT invalide : {e}")
    try:
        with ui.console.status("[cyan]POST sur Torr9…[/cyan]", spinner="dots"):
            res = torr9_api.upload(
                jwt,
                torrent_path=plan.torrent_path,
                title=plan.title,
                description=plan.description,
                category=plan.category_name,
                subcategory=plan.subcategory_name,
                nfo_text=plan.nfo_text,
                tags=plan.tags,
            )
    except AuthError as e:
        return PostResult("torr9", False, f"Auth refusée : {e}")
    except TrackerError as e:
        return PostResult("torr9", False, f"Échec : {e}")
    url_hint = f"https://torr9.net/torrent/{res.info_hash}" if res.info_hash else ""
    # L'id numérique sert à télécharger le .torrent re-signé après upload
    raw = res.raw if isinstance(res.raw, dict) else {}
    torrent_id = 0
    for k in ("id", "torrent_id", "torrentId"):
        v = raw.get(k)
        if isinstance(v, int):
            torrent_id = v
            break
        if isinstance(v, str) and v.isdigit():
            torrent_id = int(v)
            break
    return PostResult(
        "torr9", True, res.message or "Envoyé.",
        info_hash=res.info_hash, status=res.status, url_hint=url_hint,
        torrent_id=torrent_id,
    )


def _print_post_result(r: PostResult) -> None:
    ui.console.print()
    if r.ok:
        ui.console.print(
            ui.success_panel(
                f"✓ {r.tracker.upper()}",
                f"Status : [bold]{r.status or 'pending'}[/]\n"
                f"Hash   : [{ui.MUTED}]{r.info_hash or '?'}[/]\n"
                + (f"URL    : {r.url_hint}\n" if r.url_hint else "")
                + f"{r.message}",
            )
        )
    else:
        ui.console.print(ui.error_panel(f"✗ {r.tracker.upper()}", r.message))


# ─────────────────────────── helpers ───────────────────────────


_STRIP_DOMAINS = ("c411.org",)


def _neutralize_bbcode(text: str) -> str:
    """Nettoie une description BBCode des liens internes au site source."""
    if not text:
        return ""
    out = text
    for dom in _STRIP_DOMAINS:
        out = re.sub(
            rf"\[url=[^\]]*{re.escape(dom)}[^\]]*\]([^\[]*)\[/url\]",
            r"\1", out, flags=re.IGNORECASE,
        )
        out = re.sub(
            rf"https?://\S*{re.escape(dom)}\S*",
            "", out, flags=re.IGNORECASE,
        )
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


# ─────────────────────── Traductions EN → FR ───────────────────────


# Mapping des genres RAWG (anglais) vers leurs labels français usuels côté trackers FR.
# Le tag interne dans les URLs reste en anglais (côté C411 le tag est en EN dans la BDD).
GENRE_EN_TO_FR = {
    "Racing": "Course",
    "Arcade": "Arcade",
    "Action": "Action",
    "Adventure": "Aventure",
    "RPG": "RPG",
    "Strategy": "Stratégie",
    "Shooter": "Shooter",
    "Casual": "Casual",
    "Simulation": "Simulation",
    "Puzzle": "Puzzle",
    "Platformer": "Plateforme",
    "Massively Multiplayer": "MMO",
    "Sports": "Sport",
    "Indie": "Indé",
    "Family": "Famille",
    "Fighting": "Combat",
    "Educational": "Éducatif",
    "Board Games": "Jeux de société",
    "Card": "Cartes",
    "Casino": "Casino",
}

# Classifications ESRB / PEGI (anglais → français)
AGE_RATING_EN_TO_FR = {
    "Everyone": "Tout public",
    "Everyone 10+": "Tout public 10+",
    "Teen": "Adolescent (13+)",
    "Mature": "Adulte (17+)",
    "Adults Only": "Adultes uniquement (18+)",
    "Rating Pending": "Classification en attente",
    "EC": "Petite enfance",
}


def _tr_genre(g: str) -> str:
    return GENRE_EN_TO_FR.get(g, g)


def _tr_age_rating(r: str) -> str:
    return AGE_RATING_EN_TO_FR.get(r, r)


def _split_for_translation(text: str, *, max_chars: int = 480) -> list[str]:
    """Découpe par phrases pour respecter la limite anonyme (500 chars / requête)."""
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    chunks: list[str] = []
    cur = ""
    for s in sentences:
        if not s:
            continue
        if len(s) > max_chars:
            # Phrase trop longue : split brut au mot
            if cur:
                chunks.append(cur)
                cur = ""
            for i in range(0, len(s), max_chars):
                chunks.append(s[i:i + max_chars])
            continue
        if cur and len(cur) + 1 + len(s) > max_chars:
            chunks.append(cur)
            cur = s
        else:
            cur = (cur + " " + s).strip() if cur else s
    if cur:
        chunks.append(cur)
    return chunks


def _translate_chunk_en_fr(text: str, *, timeout: float = 5.0) -> str | None:
    """Traduit un segment EN → FR via l'API publique MyMemory. None si échec."""
    import httpx

    if not text.strip():
        return text
    try:
        from trackr.http import make_client

        with make_client() as client:
            client.timeout = httpx.Timeout(timeout, connect=min(timeout, 3.0))
            r = client.get(
                "https://api.mymemory.translated.net/get",
                params={"q": text, "langpair": "en|fr"},
            )
        if r.status_code != 200:
            return None
        data = r.json()
        translated = data.get("responseData", {}).get("translatedText")
        if not translated:
            return None
        # MyMemory renvoie parfois "MYMEMORY WARNING: ..." sur erreur quota
        if translated.startswith("MYMEMORY WARNING"):
            return None
        return str(translated)
    except (httpx.HTTPError, ValueError, KeyError):
        return None


def _looks_english(text: str) -> bool:
    """Heuristique simple : détecte si un texte est en anglais (pas en FR).

    Cherche des mots-stop anglais fréquents — fiable pour des descriptions de
    jeux qui font ≥ 200 chars. Évite de retraduire si RAWG renvoyait déjà du FR.
    """
    t = " " + text.lower() + " "
    en_markers = (" the ", " and ", " with ", " your ", " you ", " from ", " this ", " that ")
    fr_markers = (" le ", " la ", " les ", " des ", " avec ", " votre ", " vous ", " cette ", " dans ")
    en_score = sum(1 for m in en_markers if m in t)
    fr_score = sum(1 for m in fr_markers if m in t)
    return en_score > fr_score


def _translate_description_en_fr(text: str) -> str:
    """Traduit une description EN → FR. Retourne l'EN si la traduction échoue."""
    if not text.strip():
        return text
    chunks = _split_for_translation(text, max_chars=480)
    out_parts: list[str] = []
    any_translated = False
    for c in chunks:
        tr = _translate_chunk_en_fr(c)
        if tr:
            out_parts.append(tr)
            any_translated = True
        else:
            out_parts.append(c)
    if not any_translated:
        return text  # tous les chunks ont échoué → on garde l'original
    return " ".join(out_parts)


# ─────────────────────── Présentation BBCode (depuis rawg_data) ───────────────────────


def _clean_text(s: str) -> str:
    """Nettoie le HTML résiduel d'une chaîne (cas RAWG.description)."""
    import html as _html

    t = str(s or "")
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.IGNORECASE)
    t = re.sub(r"</p>", "\n\n", t, flags=re.IGNORECASE)
    t = re.sub(r"<[^>]+>", "", t)
    t = _html.unescape(t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _build_presentation_bbcode(
    rawg_data: dict,
    *,
    console: ConsoleSpec,
    region: str,
    language: str,
    container: str,
    version: str,
    title: str,
    total_size: int,
    file_count: int,
    keep_internal_links: bool = False,
) -> str:
    """Construit une présentation BBCode propre directement depuis les données RAWG.

    Balises strictement imbriquées (pas de regex sur du HTML imbriqué).
    keep_internal_links : si True, garde [url=/torrents?tags=X] (utile C411).
    """
    if not rawg_data:
        return ""

    g = rawg_data
    rawg_title = str(g.get("title") or g.get("name") or "?")
    year = str(g.get("year") or "")
    image = str(g.get("imageUrl") or g.get("backgroundUrl") or "")
    platforms = [str(p) for p in (g.get("platforms") or []) if isinstance(p, str)]
    genres = [str(x) for x in (g.get("genres") or []) if isinstance(x, str)]
    metacritic = g.get("metacritic")
    rating = g.get("rating")
    rating_count = g.get("ratingCount")
    age_rating = g.get("ageRating") or g.get("esrbRating")
    developers = [str(d) for d in (g.get("developers") or []) if isinstance(d, str)]
    publishers = [str(p) for p in (g.get("publishers") or []) if isinstance(p, str)]
    description = _clean_text(g.get("description") or "")
    # Traduction EN → FR pour rendre la description directement utilisable
    # (sans intervention manuelle côté TP). Fallback silencieux sur l'EN si l'API
    # publique est indisponible.
    if description and _looks_english(description):
        with ui.console.status("[cyan]Traduction de la description…[/cyan]", spinner="dots"):
            description = _translate_description_en_fr(description)

    # Header : cover centrée + titre
    header_lines = ["[center]"]
    title_block = f"[size=22][b][color=#10B981]🎮 {rawg_title}[/color][/b][/size]"
    if year:
        title_block += f" [size=16][color=#10B981]({year})[/color][/size]"
    header_lines.append(title_block)
    if image:
        header_lines.append(f"[img]{image}[/img]")
    header_lines.append("[/center]")

    parts: list[str] = ["\n".join(header_lines)]

    # Section Informations
    info_lines = [_section_header_bb("Informations")]
    if platforms:
        info_lines.append(f"[b][color=#10B981]Plateformes :[/color][/b] [i]{', '.join(platforms[:8])}[/i]")
    if genres:
        # Genres traduits FR pour l'affichage. Côté C411 le tag interne reste en EN
        # (les liens /torrents?tags=Racing pointent sur le tag stocké en anglais).
        if keep_internal_links:
            gh = ", ".join(f"[url=/torrents?tags={g}]{_tr_genre(g)}[/url]" for g in genres[:8])
        else:
            gh = ", ".join(_tr_genre(g) for g in genres[:8])
        info_lines.append(f"[b][color=#10B981]Genres :[/color][/b] [i]{gh}[/i]")
    if age_rating:
        info_lines.append(f"[b][color=#10B981]Classification :[/color][/b] [i]{_tr_age_rating(age_rating)}[/i]")
    if developers:
        info_lines.append(f"[b][color=#10B981]Développeur(s) :[/color][/b] [i]{', '.join(developers[:3])}[/i]")
    if publishers:
        info_lines.append(f"[b][color=#10B981]Éditeur(s) :[/color][/b] [i]{', '.join(publishers[:3])}[/i]")
    if metacritic:
        try:
            mc = int(metacritic)
            mc_color = "#22c55e" if mc >= 75 else "#facc15" if mc >= 50 else "#ef4444"
            info_lines.append(
                f"[b][color=#10B981]Metacritic :[/color][/b] "
                f"[bgcolor={mc_color}][color=white] {mc} [/color][/bgcolor]"
            )
        except (TypeError, ValueError):
            pass
    if rating:
        try:
            r = float(rating)
            count_str = f" ({int(rating_count)} votes)" if rating_count else ""
            info_lines.append(f"[b][color=#10B981]Note utilisateurs :[/color][/b] [i]⭐ {r:.1f}/5{count_str}[/i]")
        except (TypeError, ValueError):
            pass
    parts.append("\n".join(info_lines))

    # Section Description
    if description:
        parts.append(
            _section_header_bb("Description") + "\n"
            + description
        )

    # Section Informations techniques (le "qui" / "quoi" du release)
    tech_lines = [_section_header_bb("Informations techniques")]
    tech_lines.append(f"[b][color=#10B981]Console :[/color][/b] [i]{console.label}[/i]")
    tech_lines.append(f"[b][color=#10B981]Conteneur :[/color][/b] [i]{container}[/i]")
    tech_lines.append(f"[b][color=#10B981]Région :[/color][/b] [i]{region}[/i]")
    tech_lines.append(f"[b][color=#10B981]Langue(s) :[/color][/b] [i]{language}[/i]")
    if version:
        tech_lines.append(f"[b][color=#10B981]Version :[/color][/b] [i]{version}[/i]")
    parts.append("\n".join(tech_lines))

    # Section Release (taille / fichiers)
    rel_lines = [_section_header_bb("Release")]
    rel_lines.append(f"[b][color=#10B981]Nom :[/color][/b] [i]{title}[/i]")
    rel_lines.append(f"[b][color=#10B981]Taille totale :[/color][/b] [i]{_size_with_bytes(total_size)}[/i]")
    rel_lines.append(f"[b][color=#10B981]Nombre de fichier(s) :[/color][/b] [i]{file_count}[/i]")
    parts.append("\n".join(rel_lines))

    return "\n\n".join(parts)


def _section_header_bb(label: str) -> str:
    """Header de section avec séparateur visuel."""
    return f"[center][b][color=#10B981]━━━━━━ {label} ━━━━━━[/color][/b][/center]"


# ─────────────────────── Description HTML (pour C411) ───────────────────────


def _esc_html(s: str) -> str:
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;"))


def _build_description_html(
    *,
    presentation_html: str,
    screenshots: list[str],
    config_min: str,
    install_notes: str,
) -> str:
    """Assemble la description HTML : présentation RAWG + screenshots + config + install."""
    parts: list[str] = []
    if presentation_html.strip():
        parts.append(presentation_html.strip())

    if screenshots:
        shots_html = "".join(
            f'<div style="text-align: center; margin: 0.5rem 0;">'
            f'<img src="{_esc_html(u)}" alt="screenshot" style="max-width: 100%; border-radius: 6px;" loading="lazy">'
            f'</div>'
            for u in screenshots
        )
        parts.append(
            '<div style="text-align: center; margin-top: 1.5rem;">'
            '<strong>Aperçus</strong></div>' + shots_html
        )

    if config_min.strip():
        cfg_html = _esc_html(config_min.strip()).replace("\n", "<br>")
        parts.append(f"<br><strong>Configuration / Compatibilité</strong><br>{cfg_html}")

    if install_notes.strip():
        ins_html = _esc_html(install_notes.strip()).replace("\n", "<br>")
        parts.append(f"<br><strong>Installation</strong><br>{ins_html}")

    parts.append(
        '<br><div style="text-align: center; font-size: 85%; opacity: 0.8;">'
        '<em>Uploadé via <a href="https://github.com/casi-3/trackr">Trackr</a></em></div>'
    )
    return "\n".join(parts)


# ─────────────────────── HTML → BBCode (pour cross-poster sur Torr9) ───────────────────────


def _rem_to_pt(rem: str) -> int:
    """Convertit une taille `Xrem` en pt approximatif pour BBCode [size=...]."""
    try:
        v = float(rem)
    except ValueError:
        return 14
    # 1rem ≈ 14pt, 1.25rem ≈ 18pt, 2rem ≈ 28pt — calibré sur les valeurs C411
    return max(8, min(48, int(round(v * 14))))


def _html_to_bbcode(html: str, *, keep_internal_links: bool = False) -> str:
    """Convertit un HTML (style présentation RAWG côté C411) en BBCode.

    keep_internal_links=True : préserve les `<a href="/torrents?tags=X">` en
    [url=...] (utile pour C411 où ces liens résolvent en interne).
    keep_internal_links=False : strip ces liens (Torr9, autres trackers).
    """
    if not html:
        return ""
    t = html

    # 1. Liens
    if keep_internal_links:
        # Liens c411.org : on garde seulement le label (refs internes au tracker source)
        t = re.sub(r'<a\s+href="[^"]*c411\.org[^"]*"[^>]*>(.*?)</a>',
                   r'\1', t, flags=re.IGNORECASE | re.DOTALL)
        # Liens relatifs : on garde en [url=...]
        t = re.sub(r'<a\s+href="(/[^"]+)"[^>]*>(.*?)</a>',
                   r'[url=\1]\2[/url]', t, flags=re.IGNORECASE | re.DOTALL)
    else:
        # Tout lien interne → texte seul
        t = re.sub(r'<a\s+href="[^"]*c411\.org[^"]*"[^>]*>(.*?)</a>',
                   r'\1', t, flags=re.IGNORECASE | re.DOTALL)
        t = re.sub(r'<a\s+href="/[^"]*"[^>]*>(.*?)</a>',
                   r'\1', t, flags=re.IGNORECASE | re.DOTALL)
    # Liens externes → [url=...]
    t = re.sub(r'<a\s+href="(https?://[^"]+)"[^>]*>(.*?)</a>',
               r'[url=\1]\2[/url]', t, flags=re.IGNORECASE | re.DOTALL)

    # 2. Images
    t = re.sub(r'<img\s+[^>]*src="([^"]+)"[^>]*>',
               r'[img]\1[/img]', t, flags=re.IGNORECASE)

    # 3. Span avec background-color → [bgcolor=...]
    t = re.sub(
        r'<span\s+[^>]*background-color:\s*([#\w]+)[^>]*>(.*?)</span>',
        r'[bgcolor=\1]\2[/bgcolor]', t, flags=re.IGNORECASE | re.DOTALL,
    )

    # 4. Span avec font-size: Xrem → [size=Y]
    def _size_repl(m):
        val = _rem_to_pt(m.group(1))
        return f'[size={val}]{m.group(2)}[/size]'
    t = re.sub(
        r'<span\s+[^>]*font-size:\s*([\d.]+)rem[^>]*>(.*?)</span>',
        _size_repl, t, flags=re.IGNORECASE | re.DOTALL,
    )
    # font-size en px ou pt : on strip (pas de mapping fiable)
    t = re.sub(r'<span\s+[^>]*font-size:[^>]*>(.*?)</span>',
               r'\1', t, flags=re.IGNORECASE | re.DOTALL)

    # 5. Span avec font-family : cosmétique, on strip plutôt que de mapper en
    # [font=...] qui nécessiterait de tracker la balise fermante (parsing HTML).
    t = re.sub(r'<span\s+[^>]*font-family:[^>]*>(.*?)</span>',
               r'\1', t, flags=re.IGNORECASE | re.DOTALL)

    # 6. Span couleur inline
    t = re.sub(r'<span\s+[^>]*color:\s*([#\w]+)[^>]*>(.*?)</span>',
               r'[color=\1]\2[/color]', t, flags=re.IGNORECASE | re.DOTALL)

    # 7. Centrage (div text-align center ou class text-center)
    t = re.sub(r'<div\s+[^>]*text-align:\s*center[^>]*>(.*?)</div>',
               r'[center]\1[/center]', t, flags=re.IGNORECASE | re.DOTALL)
    t = re.sub(r'<div\s+[^>]*class="[^"]*text-center[^"]*"[^>]*>(.*?)</div>',
               r'[center]\1[/center]', t, flags=re.IGNORECASE | re.DOTALL)

    # 8. Gras / italique / souligné
    t = re.sub(r'<(strong|b)>(.*?)</\1>', r'[b]\2[/b]', t, flags=re.IGNORECASE | re.DOTALL)
    t = re.sub(r'<(em|i)>(.*?)</\1>', r'[i]\2[/i]', t, flags=re.IGNORECASE | re.DOTALL)
    t = re.sub(r'<u>(.*?)</u>', r'[u]\1[/u]', t, flags=re.IGNORECASE | re.DOTALL)

    # 9. Sauts de ligne et paragraphes
    t = re.sub(r'<br\s*/?>', '\n', t, flags=re.IGNORECASE)
    t = re.sub(r'</p>', '\n\n', t, flags=re.IGNORECASE)
    t = re.sub(r'<p[^>]*>', '', t, flags=re.IGNORECASE)

    # 10. Strip tous les autres conteneurs (div, span résiduels, section, etc.)
    t = re.sub(r'</?(?:div|span|section|article|header|footer)[^>]*>', '', t, flags=re.IGNORECASE)

    # 11. Strip tag HTML restant
    t = re.sub(r'<[^>]+>', '', t)

    # 12. Entités HTML usuelles
    t = (t
         .replace('&nbsp;', ' ')
         .replace('&amp;', '&')
         .replace('&lt;', '<')
         .replace('&gt;', '>')
         .replace('&quot;', '"')
         .replace('&#39;', "'")
         .replace('&apos;', "'"))

    # 13. Nettoyage espaces / lignes vides
    t = re.sub(r'[ \t]+\n', '\n', t)
    t = re.sub(r'\n{3,}', '\n\n', t)

    # 14. Refs c411 (Torr9 uniquement — sur C411 on les a déjà préservées)
    if not keep_internal_links:
        t = _neutralize_bbcode(t)
    return t.strip()


# ─────────────────────────── tracker .torrent + seed ───────────────────────────


def _fetch_tracker_torrent_game(plan: GamePlan, result: PostResult, cfg: Config) -> None:
    """Télécharge le .torrent re-signé par le tracker après upload réussi.

    Le .torrent local a été créé avant le POST avec une passkey "naïve" ;
    le tracker peut renvoyer une variante signée (champ `source` normalisé,
    announce mis à jour). Utiliser le re-signé garantit un info_hash valide
    sans bind à refaire au premier announce.
    """
    if not plan.torrent_path:
        return
    out_path = plan.torrent_path.with_suffix(".from_tracker.torrent")
    try:
        with ui.console.status(
            f"[cyan]Récupération du .torrent signé par {plan.tracker.upper()}…[/cyan]",
            spinner="dots",
        ):
            if plan.tracker == "c411":
                if not cfg.c411_session_valid():
                    raise AuthError("session web expirée")
                ident = result.info_hash
                if not ident:
                    raise TrackerError("info_hash absent du retour upload")
                c411_api.download_torrent(cfg.c411_session, ident, out_path)
            elif plan.tracker == "torr9":
                if not result.torrent_id:
                    raise TrackerError("id absent du retour upload")
                jwt = ensure_torr9_jwt(cfg)
                torr9_api.download_torrent(jwt, result.torrent_id, out_path)
            else:
                return
    except (AuthError, TrackerError) as e:
        ui.console.print(
            f"[{ui.WARN}]⚠ {plan.tracker.upper()} — .torrent signé non récupéré : {e}[/]\n"
            f"[{ui.MUTED}][italic]Fallback sur la version locale pour le seed.[/italic][/]"
        )
        return
    result.tracker_torrent_path = out_path


def _offer_seed_game(
    plans: list[GamePlan],
    results: list[PostResult],
    cfg: Config,
    source_path: Path,
) -> None:
    """Propose d'ajouter les .torrent publiés à qBittorrent pour seeder.

    Détecte un qBit en Docker (save_path divergent du host) et suggère
    le chemin tel que vu côté conteneur, sinon le recheck reste à 0%.
    """
    # Helpers partagés avec le flow movie (qBit prefs + heuristique Docker)
    from trackr.flows.movie import (
        _get_qbt_default_save_path,
        _guess_container_path,
        _normalize_path,
    )

    # Exclure les torrents passés en brouillon : pas encore accepté serveur,
    # le seed sera proposé à la publication ultérieure depuis le menu Brouillons.
    ok_pairs: list[tuple[PostResult, GamePlan]] = [
        (r, p) for r, p in zip(results, plans)
        if r.ok and p.torrent_path and r.status != "draft"
    ]
    if not ok_pairs:
        return

    ui.console.print()
    if not cfg.is_qbt_ready():
        ui.console.print(
            f"[{ui.MUTED}][italic]qBittorrent n'est pas configuré — passe par Configuration "
            f"pour activer le seed automatique après upload.[/italic][/]"
        )
        return

    ui.console.print(
        f"[bold {ui.ACCENT}]Seeder dans qBittorrent ?[/]\n"
        f"[{ui.MUTED}][italic]On ajoute le .torrent à qBit en pointant vers le fichier/dossier "
        f"déjà sur disque. qBit fait un recheck rapide puis seed — aucun téléchargement.[/italic][/]"
    )
    if not questionary.confirm("Ajouter les torrents à qBittorrent maintenant ?", default=True).ask():
        return

    # Le save_path attendu par qBit = dossier qui CONTIENT le source.
    source_parent = str(source_path.parent)

    # Détection Docker : si le save_path par défaut de qBit n'a pas la même
    # racine que le path host (ex: qBit voit `/downloads/...` alors que l'host
    # voit `/data/films/...`), c'est un volume mount Docker (ou autre namespace
    # de chemin). On suggère le chemin tel que le conteneur le voit.
    qbt_default_save = _get_qbt_default_save_path(cfg)
    docker_suspected = (
        qbt_default_save
        and source_parent.split("/")[1:2] != qbt_default_save.split("/")[1:2]
    )
    if docker_suspected:
        suggested = _guess_container_path(source_parent, qbt_default_save, cfg)
        ui.console.print(
            f"\n[{ui.WARN}]⚠ qBittorrent semble tourner en Docker (ou autre namespace de chemin).[/]\n"
            f"[{ui.MUTED}]  Ton fichier (côté host)     : {source_parent}/{source_path.name}[/]\n"
            f"[{ui.MUTED}]  save_path par défaut qBit   : {qbt_default_save}[/]\n"
            + (f"[{ui.MUTED}]  Path container suggéré      : {suggested}[/]\n" if suggested else "")
            + f"[{ui.MUTED}][italic]  Indique le chemin **tel que qBit le voit** dans le conteneur, "
            f"sinon le recheck restera à 0%.[/italic][/]"
        )
        default_save = suggested or qbt_default_save
    else:
        default_save = source_parent
        ui.console.print(
            f"[{ui.MUTED}][italic]Chemin = dossier qui contient le fichier/dossier source.[/italic][/]"
        )
    raw = questionary.path("Dossier de seed :", default=default_save).ask()
    if not raw:
        ui.console.print(f"[{ui.MUTED}]Seed annulé.[/]")
        return
    save_path_str = raw.strip().rstrip("/") or raw.strip()

    # Validation locale uniquement si pas Docker (sinon le path n'existe pas côté host).
    # qBit refusera de toute façon si invalide.
    if not docker_suspected:
        local_path = _normalize_path(save_path_str)
        if not local_path.exists() or not local_path.is_dir():
            ui.console.print(
                ui.error_panel(
                    "Dossier introuvable",
                    f"{local_path}\n[italic]qBit refusera si le dossier n'existe pas.[/italic]",
                )
            )
            return
        save_path_str = str(local_path)
        expected = local_path / source_path.name
        if not expected.exists():
            ui.console.print(
                f"[{ui.WARN}]⚠ '{source_path.name}' n'est pas présent dans ce dossier.[/]\n"
                f"[{ui.MUTED}][italic]qBit marquera le torrent 'Missing files' — à toi de "
                f"déplacer/copier ensuite.[/italic][/]"
            )
            if not questionary.confirm("Continuer quand même ?", default=False).ask():
                return

    # Tags : un par tracker où c'est publié (reflète la portée)
    publication_tags = [f"trackr-{p.tracker.upper()}" for _, p in ok_pairs]

    # Picker tracker(s) à seed si > 1 disponible
    if len(ok_pairs) > 1:
        choices = [
            questionary.Choice(
                f"{p.tracker.upper()}  ·  {p.info_hash[:12]}…",
                value=i, checked=True,
            )
            for i, (_, p) in enumerate(ok_pairs)
        ]
        picked = questionary.checkbox(
            "Trackers à seeder :",
            choices=choices,
            validate=lambda a: True if a else "Sélectionne au moins un tracker.",
        ).ask()
        if not picked:
            return
        to_seed = [ok_pairs[i] for i in picked]
    else:
        to_seed = ok_pairs

    for r, p in to_seed:
        _seed_one_game(p, r, cfg, save_path_str, publication_tags)


def _seed_one_game(
    plan: GamePlan,
    result: PostResult,
    cfg: Config,
    save_path: str,
    tags: list[str],
) -> None:
    """Ajoute un .torrent dans qBit (api_key OU login + refresh SID)."""
    torrent_path = result.tracker_torrent_path or plan.torrent_path
    if not torrent_path:
        return
    source_label = "signé par le tracker" if result.tracker_torrent_path else "local — fallback"

    try:
        with ui.console.status(
            f"[cyan]Ajout dans qBittorrent ({plan.tracker.upper()})…[/cyan]",
            spinner="dots",
        ):
            if cfg.qbt_auth_mode == "api_key":
                qbt.add_torrent(
                    cfg.qbt_url, torrent_path, save_path,
                    api_key=cfg.qbt_api_key, tags=tags,
                )
            elif cfg.qbt_auth_mode == "login":
                sid = cfg.qbt_sid_cookie
                try:
                    if not sid:
                        sid = qbt.login(cfg.qbt_url, cfg.qbt_username, cfg.qbt_password)
                        cfg.qbt_sid_cookie = sid
                        save_config(cfg)
                    qbt.add_torrent(cfg.qbt_url, torrent_path, save_path, sid=sid, tags=tags)
                except qbt.QbtAuthError:
                    sid = qbt.login(cfg.qbt_url, cfg.qbt_username, cfg.qbt_password)
                    cfg.qbt_sid_cookie = sid
                    save_config(cfg)
                    qbt.add_torrent(cfg.qbt_url, torrent_path, save_path, sid=sid, tags=tags)
            else:
                ui.console.print(ui.error_panel("qBittorrent", "mode d'auth invalide"))
                return
    except qbt.QbtAuthError as e:
        ui.console.print(
            ui.error_panel(
                f"qBittorrent — auth refusée ({plan.tracker.upper()})",
                f"{e}\n[italic]Reconfigure le client dans Configuration.[/italic]",
            )
        )
        return
    except qbt.QbtError as e:
        ui.console.print(ui.error_panel(f"qBittorrent — ajout échoué ({plan.tracker.upper()})", str(e)))
        return

    ui.console.print(
        ui.success_panel(
            f"✓ {plan.tracker.upper()} ajouté dans qBittorrent",
            f"save_path : {save_path}\n"
            f"info hash : {plan.info_hash}\n"
            f".torrent : {source_label}\n"
            f"[italic]Recheck en cours côté qBit ; le seed démarre dès qu'il a validé les pièces.[/italic]",
        )
    )


# ─────────────────────────── defaults config/install ───────────────────────────


def _default_config_requirements(console: ConsoleSpec, container: str) -> str:
    """Renvoie un template de config requise selon la console et le conteneur.

    L'user peut éditer ce texte avant qu'il soit injecté dans le NFO et la
    description BBCode. Couvre les cas pratiques (console modée, retail,
    émulateur) sans présumer du setup spécifique de l'utilisateur final.
    """
    key = console.key
    cont = container.upper()

    if key == "xbox":
        if cont == "ISO":
            return (
                "Émulateur Xemu 0.7+ (https://xemu.app)\n"
                "  • Windows / Linux / macOS\n"
                "  • GPU compatible Vulkan ou OpenGL 4.6\n"
                "  • 4 Go RAM minimum\n"
                "  • BIOS + EEPROM + image HDD requis (non fournis avec ce torrent)\n"
            )
        return (
            "Xbox originale modée (Softmod ou TSOP / Hard mod)\n"
            "Disque dur formaté avec FATX / dashboard custom (Evolution X, UnleashX)\n"
        )

    if key == "xbox360":
        if cont == "GOD":
            return (
                "Xbox 360 modée (RGH ou JTAG), kernel ≥ 17559 recommandé\n"
                "Dashboard custom (freeBOOT / Aurora / FreeStyle Dashboard)\n"
                "Disque dur interne ou USB en FATX, ~8 Go libres minimum\n"
                "Manette officielle ou compatible\n"
                "\n"
                "Alternative émulateur :\n"
                "  • Xenia canary 2024+ (https://xenia.jp)\n"
                "  • Windows 10/11 64-bit, CPU AVX, GPU compatible DX12 / Vulkan\n"
                "  • 8 Go RAM minimum, 16 Go recommandés\n"
            )
        if cont == "ISO":
            return (
                "Émulateur Xenia canary 2024+ (https://xenia.jp)\n"
                "  • Windows 10/11 64-bit\n"
                "  • CPU avec AVX (Intel Haswell+ / AMD Excavator+)\n"
                "  • GPU compatible DX12 ou Vulkan\n"
                "  • 8 Go RAM minimum, 16 Go recommandés\n"
                "\n"
                "ISO chargeable directement dans Xenia, aucune conversion nécessaire.\n"
            )
        if cont == "JTAG":
            return (
                "Xbox 360 JTAG uniquement (kernel ≤ 7371)\n"
                "Dashboard custom (freeBOOT, XeXMenu, Aurora)\n"
                "Disque dur interne ou USB en FATX, ~10 Go libres\n"
            )
        if cont == "RGH":
            return (
                "Xbox 360 RGH (Reset Glitch Hack — tous modèles compatibles)\n"
                "Dashboard custom (freeBOOT / Aurora / FreeStyle Dashboard)\n"
                "Disque dur interne ou USB en FATX, ~10 Go libres\n"
            )
        return ""

    if key == "xone":
        return (
            "Xbox One retail ou dev avec firmware récent\n"
            "Dev Mode activé OU licence du jeu liée au compte Microsoft\n"
            "~50 Go libres sur le stockage interne ou disque USB 3.0\n"
            "Connexion Xbox Live recommandée pour l'activation initiale\n"
            "\n"
            "Note : pas d'émulateur Xbox One fonctionnel à ce jour.\n"
        )

    if key == "xsx":
        return (
            "Xbox Series X|S retail ou dev\n"
            "Dev Mode activé OU licence du jeu liée au compte Microsoft\n"
            "~80 Go libres sur SSD interne ou Seagate Storage Expansion Card\n"
            "Connexion Xbox Live recommandée pour l'activation initiale\n"
            "\n"
            "Note : pas d'émulateur Xbox Series fonctionnel à ce jour.\n"
        )

    return ""


def _default_install_notes(console: ConsoleSpec, container: str) -> str:
    """Renvoie un template de procédure d'installation selon console + conteneur."""
    key = console.key
    cont = container.upper()

    if key == "xbox" and cont == "ISO":
        return (
            "1. Ouvrir Xemu\n"
            "2. Configurer BIOS + MCPX + EEPROM + HDD image dans Settings\n"
            "3. Load Disc → sélectionner l'ISO\n"
        )

    if key == "xbox360":
        if cont == "GOD":
            return (
                "1. Extraire le contenu du torrent\n"
                "2. Copier le dossier dans Hdd1/Content/0000000000000000/<TitleID>/00007000/\n"
                "   (ou via FTP vers la console)\n"
                "3. Lancer depuis le dashboard Jeux ou via Aurora / FSD\n"
                "\n"
                "Alternative Xenia :\n"
                "  1. Ouvrir Xenia\n"
                "  2. File → Open → pointer vers le fichier GOD principal (sans extension)\n"
            )
        if cont == "ISO":
            return (
                "1. Ouvrir Xenia\n"
                "2. File → Open → sélectionner l'ISO\n"
            )
        if cont in ("JTAG", "RGH"):
            return (
                "1. Extraire le contenu du torrent\n"
                "2. Copier le dossier du jeu sur la console (FTP, USB, HDD direct)\n"
                "   chemin habituel : Hdd1/Games/<NomDuJeu>/\n"
                "3. Lancer via le dashboard custom (freeBOOT / Aurora / FSD)\n"
            )
        return ""

    if key in ("xone", "xsx"):
        return (
            "Dev Mode :\n"
            "  1. Connecter la console au Device Portal\n"
            "  2. Installer le .xvc via l'interface ou xbapp deploy\n"
            "\n"
            "Retail (licence liée au compte) :\n"
            "  1. Se connecter avec le compte Microsoft autorisé\n"
            "  2. Installer depuis « Mes jeux et applications » → Prêt à installer\n"
        )

    return ""


def _join_strs(seq, limit: int) -> str:
    if not isinstance(seq, list):
        return ""
    return ", ".join(str(s) for s in seq[:limit] if isinstance(s, str))


def _clean_synopsis(raw: str) -> str:
    """RAWG renvoie souvent du HTML — on neutralise."""
    text = raw or ""
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > 2000:
        text = text[:1997].rstrip() + "…"
    return text


def _build_dir(name: str) -> Path:
    from platformdirs import user_cache_dir

    root = Path(user_cache_dir("trackr")) / "builds"
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = slugify(name, max_len=40)
    out = root / f"{ts}-game-{slug}"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _write_manifest(
    out_dir: Path,
    plans: list[GamePlan],
    source_path: Path,
    name: str,
    console: ConsoleSpec,
    region: str,
    language: str,
    container: str,
    version: str,
    rawg_data: dict,
    nfo_path: Path,
    desc_path: Path,
) -> Path:
    manifest = {
        "type": "game",
        "platform": "microsoft",
        "console": {"key": console.key, "label": console.label, "tag": console.title_tag},
        "name": name,
        "version": version,
        "region": region,
        "language": language,
        "container": container,
        "rawg_id": int(rawg_data.get("id") or 0) if isinstance(rawg_data, dict) else 0,
        "rawg_data": rawg_data if isinstance(rawg_data, dict) else {},
        "source_path": str(source_path),
        "nfo_path": str(nfo_path),
        "desc_path": str(desc_path),
        "plans": [
            {
                "tracker": p.tracker,
                "title": p.title,
                "category_id": p.category_id,
                "category_name": p.category_name,
                "subcategory_id": p.subcategory_id,
                "subcategory_name": p.subcategory_name,
                "options": p.options,
                "tags": p.tags,
                "torrent_path": str(p.torrent_path) if p.torrent_path else "",
                "info_hash": p.info_hash,
                "total_size": p.total_size,
            }
            for p in plans
        ],
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    p = out_dir / "manifest.json"
    p.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _show_preview(plans: list[GamePlan], manifest_path: Path) -> None:
    for plan in plans:
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="dim", width=18)
        grid.add_column()
        grid.add_row("Tracker", f"[bold {ui.ACCENT}]{plan.tracker.upper()}[/]")
        grid.add_row("Catégorie", f"{plan.category_name} ({plan.category_id}) / {plan.subcategory_name} ({plan.subcategory_id})")
        grid.add_row("Titre", plan.title)
        grid.add_row("Info hash", plan.info_hash or "?")
        grid.add_row("Taille", f"{plan.total_size:,} octets".replace(",", " "))
        if plan.tracker == "c411":
            grid.add_row("Options", json.dumps(plan.options, ensure_ascii=False) or "{}")
            rid = plan.rawg_data.get("id") if isinstance(plan.rawg_data, dict) else None
            grid.add_row("RAWG id", str(rid or "—"))
        else:
            grid.add_row("Tags", ", ".join(plan.tags) or "—")
        ui.console.print(Panel(grid, title=f"[bold {ui.ACCENT}]Preview {plan.tracker.upper()}[/]", border_style=ui.ACCENT))

    ui.console.print()
    ui.console.print(f"[bold]NFO :[/]")
    ui.console.print(Panel(Syntax(plans[0].nfo_text, "ini", theme="ansi_dark", word_wrap=True), border_style=ui.MUTED))

    # Descriptions : si différentes (C411 vs Torr9 nettoyée), on les montre toutes les deux
    seen = set()
    for plan in plans:
        if plan.description in seen:
            continue
        seen.add(plan.description)
        label = f"Description BBCode ({plan.tracker.upper()})" if len(plans) > 1 else "Description BBCode"
        ui.console.print()
        ui.console.print(f"[bold]{label} :[/]")
        ui.console.print(Panel(Syntax(plan.description, "bbcode", theme="ansi_dark", word_wrap=True), border_style=ui.MUTED))

    ui.console.print(f"[{ui.MUTED}]Manifest : {manifest_path}[/]")

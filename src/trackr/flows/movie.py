"""Flow guidé d'upload d'un film vers C411 et/ou Torr9 (POST réel)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import questionary
from platformdirs import user_cache_dir
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from trackr import qbittorrent as qbt
from trackr import ui, upload_queue
from trackr.config import Config, load_config, save_config
from trackr.media import torrent as torrent_mod
from trackr.media.lookup import MediaHit, lookup_by_id, search as tmdb_search
from trackr.media.mediainfo import MediaInfo, MediainfoError, probe
from trackr.nfo.builder import (
    build_description_bbcode,
    build_nfo,
    detect_language_tag,
    detect_source_tag,
    detect_team_tag,
    has_encoding_settings,
    has_fr_audio,
    has_fr_subs,
    slugify,
    suggest_title_c411,
)
from trackr.session import ensure_torr9_jwt


# Mapping TMDB genre IDs (standards) → slug de la valeur C411 (option Genre).
# Référence TMDB : https://api.themoviedb.org/3/genre/movie/list
TMDB_TO_C411_GENRE_SLUG: dict[int, str] = {
    28: "action",
    12: "aventure",
    16: "animation",
    35: "comedie",
    80: "crime",
    99: "documentaire",
    18: "drame",
    10751: "famille",
    14: "fantastique",
    36: "historique",
    27: "epouvante-horreur",
    10402: "musical",
    9648: "enquete",
    10749: "romance",
    878: "science-fiction",
    53: "thriller",
    10752: "guerre",
    37: "western",
    # 10770 (TV Movie) : pas d'équivalent direct
}
from trackr.trackers import c411 as c411_api
from trackr.trackers import c411_cats
from trackr.trackers import torr9 as torr9_api
from trackr.trackers import torr9_cats
from trackr.trackers.base import AuthError, TrackerError


# ─────────────────────────── dataclasses ───────────────────────────


@dataclass
class TrackerPlan:
    name: str  # "c411" | "torr9"
    announce_url: str
    source_tag: str  # tag pour le champ "source" du .torrent
    category_id: int
    category_name: str
    subcategory_id: int
    subcategory_name: str
    title: str
    options: dict = field(default_factory=dict)  # C411 : {opt_id: val_id|[val_id]}
    tags: list[str] = field(default_factory=list)  # Torr9 : tags CSV
    # Remplis après création torrent + NFO + desc
    torrent_path: Path | None = None
    info_hash: str = ""
    nfo_text: str = ""
    description: str = ""
    piece_size: int = 0
    piece_count: int = 0
    total_size: int = 0


@dataclass
class PostResult:
    tracker: str
    ok: bool
    message: str
    info_hash: str = ""
    status: str = ""
    url_hint: str = ""
    torrent_id: int = 0  # ID numérique (Torr9 le fournit, C411 utilise info_hash)
    tracker_torrent_path: Path | None = None  # .torrent re-téléchargé depuis le tracker

    @property
    def delete_identifier(self) -> str:
        """Identifiant à passer à DELETE — Torr9 veut id numérique, C411 accepte info_hash."""
        if self.tracker == "torr9":
            return str(self.torrent_id) if self.torrent_id else ""
        return self.info_hash


# ─────────────────────────── entrypoint ───────────────────────────


def run() -> None:
    ui.clear()
    ui.console.print(ui.banner())
    ui.console.print(
        Panel(
            "Flow guidé d'upload d'un film vers C411 et/ou Torr9.\n"
            f"[{ui.WARN}]POST réel après confirmation explicite.[/]",
            title=f"[bold {ui.ACCENT}]Uploader un film[/]",
            border_style=ui.ACCENT,
        )
    )

    cfg = load_config()
    available = []
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

    # 1. Sélection trackers
    targets = _ask_targets(available)
    if not targets:
        return

    # 2. Fichier source
    file_path = _ask_file_path()
    if not file_path:
        return

    # 3. Mediainfo
    info = _run_mediainfo(file_path)
    if not info:
        return

    # 4. Source / VOD / Langue / Team
    source_hint = _ask_source_hint(file_path)
    vod_platform = ""
    if source_hint in ("WEB", "WEB-DL", "WEBRip"):
        vod_platform = _ask_vod_platform()
    language_tag = _ask_language_tag(file_path, info)
    detected_team = detect_team_tag(file_path)
    team_label = (
        f"Tag TEAM (détecté : {detected_team}, sans le tiret) :"
        if detected_team != "NOTAG"
        else "Tag TEAM (sans le tiret, ex: NOTAG, FW, ZEKEY) :"
    )
    if detected_team != "NOTAG":
        ui.console.print(
            f"[{ui.WARN}]⚠ Filename porte le tag '-{detected_team}'. C411 sanctionne le DETAG par omission "
            f"(NOTAG sur un fichier qui a un tag identifiable).[/]"
        )
    team_tag = (
        questionary.text(team_label, default=detected_team).ask()
        or detected_team
    )

    # 5. JWT Torr9 (requis pour TMDB search même si on ne POST pas Torr9 — l'endpoint search est sur Torr9)
    try:
        with ui.console.status("[cyan]Vérification du JWT Torr9 (pour recherche TMDB)…[/cyan]", spinner="dots"):
            jwt = ensure_torr9_jwt(cfg)
    except (AuthError, TrackerError) as e:
        ui.console.print(ui.error_panel("Recherche TMDB indisponible", str(e)))
        ui.press_enter()
        return

    # 6. Recherche TMDB
    hit = _search_tmdb(cfg, jwt)
    if not hit:
        return

    # 7. NFO en avance (on a besoin de savoir si 'Encoding settings' est présent
    # pour choisir le tag codec : x265 (re-encode) vs H265 (stream direct)).
    try:
        nfo_text = build_nfo(file_path)
    except MediainfoError as e:
        ui.console.print(ui.error_panel("Construction NFO échouée", str(e)))
        ui.press_enter()
        return
    is_reencode = has_encoding_settings(nfo_text)
    if not is_reencode:
        ui.console.print(
            f"[{ui.MUTED}]NFO sans « Encoding settings » → release directe : "
            f"codec en H264/H265 (pas x264/x265).[/]"
        )

    # 8. Titre release (format C411 strict, appliqué partout)
    title_default = suggest_title_c411(
        hit, info, source=source_hint, language_tag=language_tag, team=team_tag,
        is_reencode=is_reencode,
    )
    ui.console.print(f"[{ui.MUTED}]Format : Nom.Année.Lang.Res.Source.Audio.Vidéo-TEAM (sans accents).[/]")
    release_title = questionary.text("Titre release :", default=title_default).ask()
    if not release_title:
        return

    # 8. Préparer un TrackerPlan par cible
    plans: list[TrackerPlan] = []
    for tracker in targets:
        ui.console.print()
        ui.console.print(f"[bold {ui.ACCENT}]── Configuration {tracker.upper()} ──[/]")
        try:
            if tracker == "c411":
                plan = _plan_c411(cfg, release_title, hit, language_tag, source_hint, vod_platform)
            else:
                plan = _plan_torr9(cfg, release_title, hit, language_tag, source_hint)
        except (AuthError, TrackerError) as e:
            ui.console.print(ui.error_panel(f"Préparation {tracker} échouée", str(e)))
            ui.press_enter()
            return
        if plan is None:
            return
        plans.append(plan)

    # 9. Build dir, NFO, description, torrents
    out_dir = _build_dir(hit, info)
    ui.console.print(f"[{ui.MUTED}]Dossier de sortie : {out_dir}[/]")

    # Taille payload = exactement ce que le tracker calcule à partir du .torrent.
    # On stat le fichier (flow single-file) pour matcher au byte près.
    try:
        payload_size = file_path.stat().st_size
    except OSError:
        payload_size = info.file_size
    description = build_description_bbcode(
        hit, info,
        release_title=release_title,
        source=source_hint,
        vod_platform=vod_platform,
        team_tag=team_tag,
        total_size=payload_size,
    )
    nfo_path = out_dir / "release.nfo"
    nfo_path.write_text(nfo_text, encoding="utf-8")
    desc_path = out_dir / "release.description.bbcode"
    desc_path.write_text(description, encoding="utf-8")

    for plan in plans:
        ui.console.print()
        torrent_out = out_dir / f"{plan.name}.torrent"
        try:
            built = torrent_mod.create_torrent(
                source_path=file_path,
                announce_url=plan.announce_url,
                output_path=torrent_out,
                source_tag=plan.source_tag,
                private=True,
                label=f"{plan.name}: hashing",
            )
        except torrent_mod.TorrentBuildError as e:
            ui.console.print(ui.error_panel(f"Création .torrent {plan.name} échouée", str(e)))
            ui.press_enter()
            return
        plan.torrent_path = built.path
        plan.info_hash = built.info_hash
        plan.piece_size = built.piece_size
        plan.piece_count = built.piece_count
        plan.total_size = built.total_size
        plan.nfo_text = nfo_text
        plan.description = description

    # 10. Manifest JSON
    manifest_path = _write_manifest(out_dir, plans, file_path, hit, info, release_title, source_hint, vod_platform, language_tag, team_tag, nfo_path, desc_path)

    # 11. Preview
    _show_preview(plans, manifest_path, nfo_text, description)

    # 12. Confirmation POST
    ui.console.print()
    confirm = questionary.confirm(
        f"Publier maintenant sur {' et '.join(p.name.upper() for p in plans)} ?",
        default=False,
    ).ask()
    if not confirm:
        ui.console.print(f"[{ui.MUTED}]Pas de POST. Les builds restent dans {out_dir}.[/]")
        ui.press_enter()
        return

    # 13. POST séquentiel + download du .torrent re-signé par le tracker
    from trackr import pending as pending_mod

    results: list[PostResult] = []
    for plan in plans:
        result = _post_plan(plan, cfg, hit)
        if result.ok and plan.torrent_path:
            _fetch_tracker_torrent(plan, result, cfg)
            # Tracking C411 : list_my_uploads ne retourne pas les pending,
            # on les garde localement pour les afficher dans le dashboard.
            if plan.name == "c411" and result.info_hash:
                pending_mod.add("c411", result.info_hash, plan.title)
        results.append(result)
        _print_post_result(result)

    # 14. Résumé final
    _show_final_summary(results, plans)

    # 15. Sauvegarde en queue si au moins un tracker a échoué (retry plus tard)
    job_id = ""
    if any(not r.ok for r in results):
        job_id = _save_to_queue(
            results, plans, file_path, hit,
            release_title=release_title,
            nfo_path=nfo_path, desc_path=desc_path, manifest_path=manifest_path,
        )
        if job_id:
            ui.console.print(
                f"\n[{ui.WARN}]💾 Upload partiellement échoué — sauvegardé en queue.[/]\n"
                f"[{ui.MUTED}]   Job id : {job_id}[/]\n"
                f"[{ui.MUTED}][italic]Tu pourras retenter les trackers en erreur depuis "
                f"le menu principal (« Reprendre les uploads en attente »).[/italic][/]"
            )

    # 16. Seed dans qBittorrent (si configuré, et au moins un upload OK)
    _offer_seed(results, plans, cfg, file_path)

    # 17. Récap fichiers générés (pour seed manuel si besoin)
    _show_artifacts_recap(out_dir, plans, results)

    # 18. Rollback rapide (si au moins un upload a réussi)
    _offer_rollback(results, cfg)

    # 19. Mode batch : enchaîner un autre upload sans repasser par le menu ?
    if questionary.confirm("\nUploader un autre film maintenant ?", default=False).ask():
        return run()

    ui.press_enter("Entrée pour revenir au menu")


# ─────────────────────────── steps ───────────────────────────


def _ask_targets(available: list[str]) -> list[str]:
    labels = {"c411": "C411", "torr9": "Torr9"}
    choices = [questionary.Choice(labels[t], value=t, checked=True) for t in available]
    answer = questionary.checkbox(
        "Sur quel(s) tracker(s) veux-tu publier ?",
        choices=choices,
        validate=lambda a: True if a else "Sélectionne au moins un tracker.",
    ).ask()
    return answer or []


def _ask_file_path() -> Path | None:
    raw = questionary.path("Chemin du fichier vidéo (drag&drop OK) :").ask()
    if not raw:
        return None
    path = _normalize_path(raw)
    if not path.exists():
        ui.console.print(ui.error_panel("Fichier introuvable", str(path)))
        ui.press_enter()
        return None
    if path.is_dir():
        ui.console.print(ui.error_panel("Dossier, pas fichier", str(path)))
        ui.press_enter()
        return None
    return path


def _normalize_path(raw: str) -> Path:
    s = raw.strip().strip("'\"")
    if s.startswith("file://"):
        from urllib.parse import unquote, urlparse

        s = unquote(urlparse(s).path)
    elif "%" in s:
        from urllib.parse import unquote

        s = unquote(s)
    s = s.replace("\\ ", " ")
    return Path(s).expanduser()


def _run_mediainfo(path: Path) -> MediaInfo | None:
    try:
        with ui.console.status("[cyan]Analyse mediainfo…[/cyan]", spinner="dots"):
            info = probe(path)
    except MediainfoError as e:
        ui.console.print(ui.error_panel("MediaInfo a échoué", str(e)))
        ui.press_enter()
        return None
    from trackr.flows.inspect import _render_panel

    ui.console.print(_render_panel(info))
    if not questionary.confirm("Continuer avec ce fichier ?", default=True).ask():
        return None
    return info


def _ask_source_hint(path: Path) -> str:
    detected = detect_source_tag(path) or "WEB"
    answer = questionary.select(
        "Source du fichier ?",
        choices=[
            questionary.Choice("WEB", value="WEB"),
            questionary.Choice("WEB-DL", value="WEB-DL"),
            questionary.Choice("WEBRip", value="WEBRip"),
            questionary.Choice("BluRay", value="BluRay"),
            questionary.Choice("BDRip", value="BDRip"),
            questionary.Choice("HDTV", value="HDTV"),
            questionary.Choice("DVDRip", value="DVDRip"),
            questionary.Choice("DVD", value="DVD"),
            questionary.Choice("Autre", value=""),
        ],
        default=detected if detected in ("WEB", "WEB-DL", "WEBRip", "BluRay", "BDRip", "HDTV", "DVDRip", "DVD") else "WEB",
    ).ask()
    return answer or ""


def _ask_vod_platform() -> str:
    choice = questionary.select(
        "Plateforme VOD source (obligatoire C411 pour WEB) :",
        choices=[
            questionary.Choice("Netflix", value="Netflix"),
            questionary.Choice("Amazon Prime", value="Amazon"),
            questionary.Choice("Disney+", value="Disney+"),
            questionary.Choice("Apple TV+", value="AppleTV+"),
            questionary.Choice("Canal+", value="Canal+"),
            questionary.Choice("Paramount+", value="Paramount+"),
            questionary.Choice("Max / HBO Max", value="Max"),
            questionary.Choice("Crunchyroll", value="Crunchyroll"),
            questionary.Choice("YouTube", value="YouTube"),
            questionary.Choice("Autre (saisie libre)", value="__custom__"),
            questionary.Choice("Inconnu", value=""),
        ],
        default="Netflix",
    ).ask()
    if choice == "__custom__":
        return questionary.text("Nom de la plateforme :").ask() or ""
    return choice or ""


def _ask_language_tag(path: Path, info: MediaInfo) -> str:
    detected = detect_language_tag(path, info)
    # Garde-fou C411 : pas de piste FR + pas de sous-titres FR = upload interdit.
    if not has_fr_audio(info) and not has_fr_subs(info):
        ui.console.print(
            ui.warn_panel(
                "Upload C411 problématique",
                "Aucune piste audio FR ni sous-titres FR détectés. C411 exige "
                "des sous-titres FR complets pour les fichiers sans piste FR — "
                "sinon l'upload sera rejeté.\n\n"
                "[italic]Tu peux continuer si tu sais que des sous-titres FR "
                "complets sont présents (parfois non taggés dans mediainfo).[/italic]",
            )
        )
    choices = [
        questionary.Choice("VFF (vraie French, France)", value="VFF"),
        questionary.Choice("VFQ (Québec)", value="VFQ"),
        questionary.Choice("VF2 (French alternatif)", value="VF2"),
        questionary.Choice("VFI (international)", value="VFI"),
        questionary.Choice("VOF (Version Originale Française)", value="VOF"),
        questionary.Choice("VOSTFR (subs officiels)", value="VOSTFR"),
        questionary.Choice("VOSTFR.FANSUB (subs de fans)", value="VOSTFR.FANSUB"),
        questionary.Choice("VOSTFR.FASTSUB (subs rapides, qualité moindre)", value="VOSTFR.FASTSUB"),
        questionary.Choice("MULTi.VFF (multi-pistes avec FR)", value="MULTi.VFF"),
        questionary.Choice("MULTi.VOF", value="MULTi.VOF"),
        questionary.Choice("MULTi.VFQ", value="MULTi.VFQ"),
        questionary.Choice("TRUEFRENCH", value="TRUEFRENCH"),
        questionary.Choice("FRENCH", value="FRENCH"),
    ]
    return (
        questionary.select(
            f"Tag de langue (détecté : {detected}) :",
            choices=choices,
            default=detected,
        ).ask()
        or detected
    )


def _search_tmdb(cfg: Config, jwt: str) -> MediaHit | None:
    raw = questionary.text("Recherche TMDB — titre OU id numérique :").ask()
    if not raw:
        return None
    raw = raw.strip()
    try:
        with ui.console.status("[cyan]Interrogation TMDB…[/cyan]", spinner="dots"):
            session = cfg.c411_session if cfg.c411_session_valid() else ""
            if raw.isdigit():
                hit = lookup_by_id(int(raw), c411_session=session, torr9_jwt=jwt, category="film")
                hits = [hit] if hit else []
            else:
                hits = tmdb_search(raw, c411_session=session, torr9_jwt=jwt, category="film", limit=10)
    except (AuthError, TrackerError) as e:
        ui.console.print(ui.error_panel("Recherche échouée", str(e)))
        ui.press_enter()
        return None

    if not hits:
        ui.console.print(ui.warn_panel("Aucun résultat", "Réessaie avec un autre titre ou id."))
        return _search_tmdb(cfg, jwt)

    if len(hits) == 1:
        ui.console.print(_render_hit(hits[0]))
        if questionary.confirm("C'est bien ce film ?", default=True).ask():
            return hits[0]
        return _search_tmdb(cfg, jwt)

    choices = []
    for h in hits:
        label = h.title
        if h.year:
            label += f" ({h.year})"
        if h.rating:
            label += f"  ★ {h.rating}"
        if h.tmdb_id:
            label += f"  · tmdb {h.tmdb_id}"
        choices.append(questionary.Choice(label, value=h.tmdb_id))
    choices.append(questionary.Choice("← Nouvelle recherche", value=-1))
    picked_id = questionary.select("Choisis le bon film :", choices=choices).ask()
    if picked_id == -1 or picked_id is None:
        return _search_tmdb(cfg, jwt)
    for h in hits:
        if h.tmdb_id == picked_id:
            ui.console.print(_render_hit(h))
            return h
    return None


def _render_hit(h: MediaHit) -> Panel:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim", width=12)
    grid.add_column()
    grid.add_row("Titre", h.title)
    if h.year:
        grid.add_row("Année", h.year)
    if h.rating:
        grid.add_row("Note", f"★ {h.rating}/10")
    grid.add_row("TMDB id", str(h.tmdb_id))
    if h.poster_url:
        grid.add_row("Poster", f"[{ui.MUTED}]{h.poster_url}[/]")
    if h.description:
        synopsis = h.description.strip()
        if len(synopsis) > 400:
            synopsis = synopsis[:400].rstrip() + "…"
        grid.add_row("Synopsis", synopsis)
    return Panel(grid, title=f"[bold {ui.ACCENT}]Sélection TMDB[/]", border_style=ui.ACCENT)


# ─────────────────────────── plans per tracker ───────────────────────────


def _plan_c411(
    cfg: Config,
    release_title: str,
    hit: MediaHit,
    language_tag: str,
    source_hint: str,
    vod_platform: str,
) -> TrackerPlan | None:
    cat = c411_cats.movies_category()
    sub_choices = [questionary.Choice(s.name, value=s.id) for s in cat.subs]
    sub_id = questionary.select(
        "Sous-catégorie C411 :",
        choices=sub_choices,
        default=6,  # Film
    ).ask()
    if sub_id is None:
        return None
    sub = next(s for s in cat.subs if s.id == sub_id)

    # Options dynamiques (Langue/Genre/Type)
    try:
        with ui.console.status("[cyan]Récupération des options C411…[/cyan]", spinner="dots"):
            opt_defs = c411_api.get_subcategory_options(cfg.c411_api_key, sub_id)
    except (AuthError, TrackerError) as e:
        ui.console.print(ui.error_panel("Options C411 indisponibles", str(e)))
        return None

    options = _ask_c411_options(opt_defs, language_tag=language_tag, hit=hit)
    if options is None:
        return None

    announce = f"https://c411.org/announce/{cfg.c411_passkey}"
    return TrackerPlan(
        name="c411",
        announce_url=announce,
        source_tag="C411",  # casse normalisée serveur — matche l'info_hash sans re-DL
        category_id=cat.id,
        category_name=cat.name,
        subcategory_id=sub.id,
        subcategory_name=sub.name,
        title=release_title,
        options=options,
    )


def _ask_c411_options(
    opt_defs: list[dict],
    *,
    language_tag: str,
    hit: MediaHit | None = None,
) -> dict | None:
    """Présente les options (Langue/Genre/Type) à l'user. Renvoie {opt_id: val_id|[val_id]}.

    Pré-coche :
    - Langue selon le tag scene-style choisi plus tôt.
    - Genre selon les genreIds TMDB du film sélectionné.
    """
    out: dict = {}

    lang_default_map = {
        "VFF": "francais-vff-truefrench",
        "TRUEFRENCH": "francais-vff-truefrench",
        "FRENCH": "francais-vff-truefrench",
        "VFQ": "quebecois-vfq-french",
        "VOF": "francais-vff-truefrench",
        "MULTi.VFF": "multi-francais-inclus",
        "MULTi.VOF": "multi-francais-inclus",
        "MULTi.VFQ": "multi-quebecois-inclus",
        "VF2": "multi-vf2",
        "VOSTFR": "vostfr",
        "VO": "anglais",
    }

    tmdb_genre_slugs: set[str] = set()
    if hit and hit.genre_ids:
        for gid in hit.genre_ids:
            slug = TMDB_TO_C411_GENRE_SLUG.get(gid)
            if slug:
                tmdb_genre_slugs.add(slug)

    for opt in opt_defs:
        opt_id = opt["id"]
        name = opt["name"]
        required = opt.get("isRequired", False)
        multi = opt.get("allowsMultiple", False)
        values = opt.get("values", [])

        if not values:
            continue

        if multi:
            default_slugs: set[str] = set()
            if opt["slug"] == "langue":
                target = lang_default_map.get(language_tag)
                if target:
                    default_slugs.add(target)
            elif opt["slug"] == "genre" and tmdb_genre_slugs:
                default_slugs |= tmdb_genre_slugs
            choices = [
                questionary.Choice(v["value"], value=v["id"], checked=(v["slug"] in default_slugs))
                for v in values
            ]
            label = f"{name} (multi) :"
            if required:
                label = f"{name} (multi, requis) :"
            kwargs = {"choices": choices}
            if required:
                err_msg = f"{name} est requis."
                kwargs["validate"] = lambda a, _err=err_msg: bool(a) or _err
            picked = questionary.checkbox(label, **kwargs).ask()
            if picked is None and required:
                return None
            if picked:
                out[opt_id] = list(picked)
        else:
            default_id = None
            if values:
                default_id = values[0]["id"]
            choices = [questionary.Choice(v["value"], value=v["id"]) for v in values]
            if not required:
                choices.insert(0, questionary.Choice("(ne pas définir)", value=0))
            label = f"{name} :"
            if required:
                label = f"{name} (requis) :"
            picked = questionary.select(
                label,
                choices=choices,
                default=default_id if not required else default_id,
            ).ask()
            if picked is None and required:
                return None
            if picked:
                out[opt_id] = picked

    return out


def _plan_torr9(
    cfg: Config,
    release_title: str,
    hit: MediaHit,
    language_tag: str,
    source_hint: str,
) -> TrackerPlan | None:
    cat = torr9_cats.movies_category()
    sub_choices = [questionary.Choice(s.name, value=s.id) for s in cat.subs]
    sub_id = questionary.select(
        "Sous-catégorie Torr9 :",
        choices=sub_choices,
        default=51,  # Films
    ).ask()
    if sub_id is None:
        return None
    sub = next(s for s in cat.subs if s.id == sub_id)

    # Tags simples auto-générés
    tags = [t for t in [source_hint, language_tag, "FR"] if t]
    if hit.year:
        tags.append(str(hit.year))

    announce = f"https://tracker.torr9.net/announce/{cfg.torr9_passkey}"
    return TrackerPlan(
        name="torr9",
        announce_url=announce,
        source_tag="Torr9",  # casse normalisée serveur — matche l'info_hash sans re-DL
        category_id=cat.id,
        category_name=cat.name,
        subcategory_id=sub.id,
        subcategory_name=sub.name,
        title=release_title,
        tags=tags,
    )


# ─────────────────────────── persistence ───────────────────────────


def _build_dir(hit: MediaHit, info: MediaInfo) -> Path:
    base = Path(user_cache_dir("trackr")) / "builds"
    slug = slugify(f"{hit.title} {hit.year}" if hit.year else hit.title)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = base / f"{slug}-{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _write_manifest(out_dir, plans, file_path, hit, info, title, source, vod, lang, team, nfo_path, desc_path) -> Path:
    manifest_path = out_dir / "manifest.json"
    manifest = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "source_file": str(file_path),
        "tmdb": {
            "id": hit.tmdb_id,
            "title": hit.title,
            "year": hit.year,
            "rating": hit.rating,
            "poster_url": hit.poster_url,
            "media_type": hit.media_type,
        },
        "source": source,
        "vod_platform": vod,
        "language_tag": lang,
        "team_tag": team,
        "release_title": title,
        "nfo_path": str(nfo_path),
        "description_path": str(desc_path),
        "plans": [
            {
                "tracker": p.name,
                "title": p.title,
                "torrent": str(p.torrent_path) if p.torrent_path else "",
                "info_hash": p.info_hash,
                "announce": p.announce_url,
                "category_id": p.category_id,
                "category_name": p.category_name,
                "subcategory_id": p.subcategory_id,
                "subcategory_name": p.subcategory_name,
                "options": p.options,
                "tags": p.tags,
                "piece_size": p.piece_size,
                "piece_count": p.piece_count,
                "total_size": p.total_size,
            }
            for p in plans
        ],
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_path


# ─────────────────────────── preview ───────────────────────────


def _show_preview(plans, manifest_path, nfo_text, description) -> None:
    ui.console.print()
    ui.console.print(ui.success_panel("Builds prêts", f"{len(plans)} tracker(s) configuré(s)."))
    for p in plans:
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="dim", width=18)
        grid.add_column()
        grid.add_row("Tracker", f"[bold]{p.name.upper()}[/]")
        grid.add_row("Titre", p.title)
        grid.add_row("Catégorie", f"{p.category_name} → {p.subcategory_name}  ([{ui.MUTED}]subcat id {p.subcategory_id}[/])")
        grid.add_row("Announce", f"[{ui.MUTED}]{p.announce_url}[/]")
        grid.add_row(".torrent", str(p.torrent_path))
        grid.add_row("Info hash", p.info_hash)
        grid.add_row("Pièces", f"{p.piece_count} × {p.piece_size // (1<<20)} MiB")
        if p.options:
            grid.add_row("Options C411", json.dumps(p.options, ensure_ascii=False))
        if p.tags:
            grid.add_row("Tags Torr9", ", ".join(p.tags))
        ui.console.print(Panel(grid, title=f"[bold {ui.ACCENT}]Plan · {p.name}[/]", border_style=ui.ACCENT))

    ui.console.print()
    ui.console.print(f"[bold {ui.ACCENT}]Aperçu NFO[/]")
    ui.console.print(Panel(nfo_text[:2000] + ("\n…" if len(nfo_text) > 2000 else ""), border_style=ui.MUTED, padding=(0, 2)))
    ui.console.print()
    ui.console.print(f"[bold {ui.ACCENT}]Aperçu description BBCode[/]")
    syntax = Syntax(description, "bbcode", theme="ansi_dark", word_wrap=True)
    ui.console.print(Panel(syntax, border_style=ui.MUTED))
    ui.console.print()
    ui.console.print(f"[{ui.MUTED}]Manifest : {manifest_path}[/]")


# ─────────────────────────── POST ───────────────────────────


def _post_plan(plan: TrackerPlan, cfg: Config, hit: MediaHit) -> PostResult:
    if plan.name == "c411":
        return _post_c411(plan, cfg, hit)
    if plan.name == "torr9":
        return _post_torr9(plan, cfg, hit)
    return PostResult(plan.name, False, "tracker inconnu")


def _post_c411(plan: TrackerPlan, cfg: Config, hit: MediaHit) -> PostResult:
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
                options=plan.options,
                tmdb_id=hit.tmdb_id,
                tmdb_type=hit.media_type or "movie",
                year=hit.year or "",
            )
    except AuthError as e:
        return PostResult("c411", False, f"Auth refusée : {e}")
    except TrackerError as e:
        return PostResult("c411", False, f"Échec : {e}")
    url_hint = f"https://c411.org/torrent/{res.info_hash}" if res.info_hash else ""
    return PostResult(
        "c411", True, res.message or "Envoyé.",
        info_hash=res.info_hash, status=res.status, url_hint=url_hint,
    )


def _post_torr9(plan: TrackerPlan, cfg: Config, hit: MediaHit) -> PostResult:
    if not plan.torrent_path:
        return PostResult("torr9", False, "Fichier .torrent manquant")
    # JWT validé (et refresh si besoin) au début du flow ; revérif au cas où
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
                tmdb_id=hit.tmdb_id,
            )
    except AuthError as e:
        return PostResult("torr9", False, f"Auth refusée : {e}")
    except TrackerError as e:
        return PostResult("torr9", False, f"Échec : {e}")
    return PostResult(
        "torr9", True, res.message or "Envoyé.",
        info_hash=res.info_hash, status=res.status,
        torrent_id=res.torrent_id,
        url_hint=f"https://torr9.net/torrent/{res.torrent_id}" if res.torrent_id else "",
    )


def _show_artifacts_recap(
    out_dir: Path,
    plans: list[TrackerPlan],
    results: list[PostResult],
) -> None:
    """Affiche un récap des fichiers générés, pour seed manuel si nécessaire."""
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style=ui.MUTED, no_wrap=True)
    grid.add_column()
    grid.add_row("Dossier", str(out_dir))
    result_by_name = {r.tracker: r for r in results}
    for p in plans:
        r = result_by_name.get(p.name)
        tracker_path = (
            Path(r.tracker_torrent_path) if (r and r.tracker_torrent_path) else None
        )
        path_to_show = tracker_path or p.torrent_path
        source_note = " (signé tracker)" if tracker_path else " (local)"
        if path_to_show:
            grid.add_row(
                f"{p.name.upper()} .torrent",
                f"{path_to_show.name}{source_note}",
            )
    ui.console.print()
    ui.console.print(
        Panel(
            grid,
            title=f"[bold {ui.ACCENT}]Fichiers générés[/]",
            border_style=ui.MUTED,
            padding=(0, 1),
        )
    )
    ui.console.print(
        f"[{ui.MUTED}][italic]Si le seed automatique a échoué, tu peux ajouter "
        f"ces .torrent manuellement à qBittorrent en pointant vers le bon dossier.[/italic][/]"
    )


def _save_to_queue(
    results: list[PostResult],
    plans: list[TrackerPlan],
    file_path: Path,
    hit: MediaHit,
    *,
    release_title: str,
    nfo_path: Path,
    desc_path: Path,
    manifest_path: Path,
) -> str:
    """Crée et persiste un UploadJob pour permettre un retry plus tard.

    Le tracker `ok` reste publié ; on garde tout en queue pour pouvoir retenter
    uniquement les `failed`. Renvoie l'id du job ou "" si rien à sauver.
    """
    result_by_name = {r.tracker: r for r in results}
    tracker_jobs: list[upload_queue.TrackerJob] = []
    for plan in plans:
        r = result_by_name.get(plan.name)
        if r is None:
            continue
        status = "ok" if r.ok else "failed"
        tj = upload_queue.TrackerJob(
            name=plan.name,
            title=plan.title,
            announce_url=plan.announce_url,
            source_tag=plan.source_tag,
            category_id=plan.category_id,
            category_name=plan.category_name,
            subcategory_id=plan.subcategory_id,
            subcategory_name=plan.subcategory_name,
            options=plan.options,
            tags=plan.tags,
            torrent_path=str(plan.torrent_path) if plan.torrent_path else "",
            tracker_torrent_path=str(r.tracker_torrent_path) if r.tracker_torrent_path else "",
            info_hash=r.info_hash or plan.info_hash,
            piece_size=plan.piece_size,
            piece_count=plan.piece_count,
            total_size=plan.total_size,
            status=status,
            last_error=r.message if not r.ok else "",
            last_attempt_at=upload_queue.now_iso(),
            url_hint=r.url_hint,
            torrent_id=r.torrent_id,
        )
        tracker_jobs.append(tj)

    job = upload_queue.UploadJob(
        id=upload_queue.new_id(),
        created_at=upload_queue.now_iso(),
        updated_at=upload_queue.now_iso(),
        source_file=str(file_path),
        nfo_path=str(nfo_path),
        description_path=str(desc_path),
        manifest_path=str(manifest_path),
        release_title=release_title,
        tmdb_id=hit.tmdb_id,
        tmdb_type=hit.media_type or "movie",
        tmdb_title=hit.title,
        tmdb_year=hit.year or "",
        media_type=hit.media_type or "movie",
        trackers=tracker_jobs,
    )
    upload_queue.save(job)
    return job.id


def _guess_container_path(host_dir: str, qbt_default_save: str, cfg: Config) -> str:
    """Propose un path container plausible à partir du nom de dossier source.

    Heuristique simple : si le default_save_path de qBit est `/downloads` et
    le fichier source est dans un dossier nommé `<leaf>`, on suggère
    `/downloads/<leaf>` — c'est le mapping Docker le plus courant.
    """
    if not host_dir or not qbt_default_save:
        return ""
    leaf = host_dir.rstrip("/").split("/")[-1]
    if not leaf:
        return qbt_default_save
    return f"{qbt_default_save.rstrip('/')}/{leaf}"


def _get_qbt_default_save_path(cfg: Config) -> str:
    """Best-effort : récupère le `save_path` par défaut de qBit pour détecter un Docker."""
    if not cfg.is_qbt_ready():
        return ""
    try:
        if cfg.qbt_auth_mode == "api_key":
            prefs = qbt.get_preferences(cfg.qbt_url, api_key=cfg.qbt_api_key)
        else:
            sid = cfg.qbt_sid_cookie
            if not sid and cfg.qbt_username and cfg.qbt_password:
                sid = qbt.login(cfg.qbt_url, cfg.qbt_username, cfg.qbt_password)
            prefs = qbt.get_preferences(cfg.qbt_url, sid=sid)
    except (qbt.QbtError, qbt.QbtAuthError):
        return ""
    return (prefs.get("save_path") or "").rstrip("/")


def _fetch_tracker_torrent(plan: TrackerPlan, result: PostResult, cfg: Config) -> None:
    """Télécharge le .torrent re-signé par le tracker juste après upload.

    On stocke le résultat dans `result.tracker_torrent_path`. En cas d'échec on
    log un warning et on tombera back sur le .torrent local pour le seed.
    """
    if not plan.torrent_path:
        return
    out_path = plan.torrent_path.with_suffix(".from_tracker.torrent")
    try:
        with ui.console.status(
            f"[cyan]Récupération du .torrent signé par {plan.name.upper()}…[/cyan]",
            spinner="dots",
        ):
            if plan.name == "c411":
                if not cfg.c411_session_valid():
                    raise AuthError(
                        "C411 download : session web expirée. Reconfigure en Guidé."
                    )
                ident = result.info_hash or str(result.torrent_id)
                c411_api.download_torrent(cfg.c411_session, ident, out_path)
            elif plan.name == "torr9":
                if not result.torrent_id:
                    raise TrackerError("Torr9 : id absent du retour upload")
                jwt = ensure_torr9_jwt(cfg)
                torr9_api.download_torrent(jwt, result.torrent_id, out_path)
            else:
                return
    except (AuthError, TrackerError) as e:
        ui.console.print(
            f"[{ui.WARN}]⚠ {plan.name.upper()} — .torrent signé non récupéré : {e}[/]\n"
            f"[{ui.MUTED}][italic]On utilisera la version locale pour le seed (peut "
            f"déclencher un re-bind tracker au premier announce).[/italic][/]"
        )
        return
    result.tracker_torrent_path = out_path


def _print_post_result(r: PostResult) -> None:
    if r.ok:
        title = f"✓ {r.tracker.upper()} OK"
        body = Text()
        body.append(r.message + "\n", style=ui.SUCCESS)
        if r.status:
            body.append(f"Statut : {r.status}\n")
        if r.info_hash:
            body.append(f"Info hash : {r.info_hash}\n")
        if r.url_hint:
            body.append(f"URL : {r.url_hint}", style=ui.MUTED)
        ui.console.print(Panel(body, border_style=ui.SUCCESS, title=title))
    else:
        ui.console.print(ui.error_panel(f"{r.tracker.upper()} a échoué", r.message))


def _offer_rollback(results: list[PostResult], cfg: Config) -> None:
    """Propose de supprimer un ou plusieurs uploads qui viennent d'être créés."""
    deletable = [r for r in results if r.ok and r.delete_identifier]
    if not deletable:
        return

    ui.console.print()
    want = questionary.confirm(
        "Supprimer un ou plusieurs uploads qui viennent d'être publiés ?",
        default=False,
    ).ask()
    if not want:
        return

    choices = [
        questionary.Choice(
            f"{r.tracker.upper()}  ·  {r.info_hash[:12]}…  ·  {r.message[:40]}",
            value=r,
        )
        for r in deletable
    ]
    picked = questionary.checkbox(
        "Sélectionne ceux à supprimer :",
        choices=choices,
        validate=lambda a: True if a else "Sélectionne au moins un upload (ou Ctrl+C pour annuler).",
    ).ask()
    if not picked:
        return

    confirm = questionary.confirm(
        f"Confirmer la suppression de {len(picked)} upload(s) ? Action irréversible.",
        default=False,
    ).ask()
    if not confirm:
        ui.console.print(f"[{ui.MUTED}]Suppression annulée.[/]")
        return

    for r in picked:
        _delete_one(r, cfg)


def _delete_one(r: PostResult, cfg: Config) -> None:
    ident = r.delete_identifier
    try:
        with ui.console.status(f"[cyan]Suppression {r.tracker.upper()}…[/cyan]", spinner="dots"):
            if r.tracker == "c411":
                if not cfg.c411_session_valid():
                    raise AuthError(
                        "C411 : session web expirée. Reconfigure en mode Guidé "
                        "(le Bearer seul ne permet pas DELETE)."
                    )
                msg = c411_api.delete_torrent(cfg.c411_session, ident)
            elif r.tracker == "torr9":
                jwt = ensure_torr9_jwt(cfg)
                msg = torr9_api.delete_torrent(jwt, int(ident))
            else:
                ui.console.print(ui.error_panel("Tracker inconnu", r.tracker))
                return
    except AuthError as e:
        ui.console.print(ui.error_panel(f"Auth refusée ({r.tracker.upper()})", str(e)))
        return
    except TrackerError as e:
        body = str(e)
        if r.tracker == "torr9" and "limite" in body.lower():
            body += (
                f"\n\n[{ui.MUTED}]Info : Torr9 limite les suppressions à 5/jour. "
                f"Tu pourras réessayer demain, ou contacter un modérateur si urgent.[/]"
            )
        ui.console.print(ui.error_panel(f"Suppression {r.tracker.upper()} échouée", body))
        return

    ui.console.print(
        Panel(
            Text.from_markup(
                f"[bold {ui.SUCCESS}]✓ {r.tracker.upper()} supprimé[/]\n"
                f"[{ui.MUTED}]{msg}[/]\n"
                f"[{ui.MUTED}]info hash : {r.info_hash}[/]"
            ),
            border_style=ui.SUCCESS,
        )
    )


def _offer_seed(
    results: list[PostResult],
    plans: list[TrackerPlan],
    cfg: Config,
    source_file: Path,
) -> None:
    """Propose d'ajouter les .torrent publiés à qBittorrent pour seeder.

    qBit fait un recheck depuis `save_path` et passe en seeding si les fichiers
    correspondent — pas de re-download.
    """
    ok_pairs: list[tuple[PostResult, TrackerPlan]] = []
    for r, p in zip(results, plans, strict=False):
        if r.ok and p.torrent_path:
            ok_pairs.append((r, p))
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
        f"[{ui.MUTED}][italic]On ajoute le .torrent à qBit en pointant vers les fichiers "
        f"déjà sur ton disque. Un recheck rapide vérifie qu'ils correspondent, puis le "
        f"seed démarre — aucun téléchargement.[/italic][/]"
    )
    if not questionary.confirm("Ajouter les torrents à qBittorrent maintenant ?", default=True).ask():
        return

    # Détection du mapping qBit (cas Docker fréquent : le path host n'est pas visible
    # par le conteneur, qBit ne trouve pas les fichiers, recheck à 0%).
    source_parent = str(source_file.parent)
    qbt_default_save = _get_qbt_default_save_path(cfg)
    docker_suspected = (
        qbt_default_save
        and source_parent.split("/")[1:2] != qbt_default_save.split("/")[1:2]
    )
    if docker_suspected:
        suggested = _guess_container_path(source_parent, qbt_default_save, cfg)
        ui.console.print(
            f"\n[{ui.WARN}]⚠ qBittorrent semble tourner en Docker (ou autre namespace de chemin).[/]\n"
            f"[{ui.MUTED}]  Ton fichier (côté host)     : {source_parent}/{source_file.name}[/]\n"
            f"[{ui.MUTED}]  save_path par défaut qBit   : {qbt_default_save}[/]\n"
            + (f"[{ui.MUTED}]  Path container suggéré      : {suggested}[/]\n" if suggested else "")
            + f"[{ui.MUTED}][italic]  Indique le chemin **tel que qBit le voit** dans le conteneur, "
            f"sinon le recheck restera à 0%.[/italic][/]"
        )
        default_save = suggested or qbt_default_save
    else:
        default_save = source_parent
    ui.console.print(
        f"[{ui.MUTED}][italic]Chemin = dossier qui contient le fichier vidéo. "
        f"Par défaut on prend le dossier du fichier (ou celui de qBit si Docker détecté).[/italic][/]"
    )
    raw = questionary.path(
        "Dossier de seed :",
        default=default_save,
    ).ask()
    if not raw:
        ui.console.print(f"[{ui.MUTED}]Seed annulé.[/]")
        return
    save_path_str = raw.strip().rstrip("/") or raw.strip()

    # Validation locale uniquement si le path semble local (cohérent côté host).
    # En mode Docker, on fait confiance à l'user — qBit refusera si invalide.
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
        expected = local_path / source_file.name
        if not expected.exists():
            ui.console.print(
                f"[{ui.WARN}]⚠ Le fichier '{source_file.name}' n'est pas présent dans ce dossier.[/]\n"
                f"[{ui.MUTED}][italic]qBit fera quand même le recheck et marquera le torrent "
                f"comme 'Missing files' — à toi de déplacer/copier le fichier ensuite.[/italic][/]"
            )
            if not questionary.confirm("Continuer quand même ?", default=False).ask():
                return

    if len(ok_pairs) > 1:
        choices = [
            questionary.Choice(
                f"{p.name.upper()}  ·  {p.info_hash[:12]}…",
                value=i,
                checked=True,
            )
            for i, (_, p) in enumerate(ok_pairs)
        ]
        picked_idx = questionary.checkbox(
            "Trackers à seeder :",
            choices=choices,
            validate=lambda a: True if a else "Sélectionne au moins un tracker.",
        ).ask()
        if not picked_idx:
            return
        to_seed = [ok_pairs[i] for i in picked_idx]
    else:
        to_seed = ok_pairs

    # Tags : un par tracker où c'est publié (reflète la portée de l'upload,
    # pas seulement les trackers seedés cette fois).
    publication_tags = [f"trackr-{p.name.upper()}" for _, p in ok_pairs]

    for r, p in to_seed:
        _seed_one(p, r, cfg, save_path_str, publication_tags)


def _seed_one(
    plan: TrackerPlan,
    result: PostResult,
    cfg: Config,
    save_path: str,
    tags: list[str],
) -> None:
    """Ajoute un .torrent unique dans qBit (gère api_key OU login + refresh SID).

    Utilise le .torrent **re-signé par le tracker** s'il est disponible (binding
    correct passkey/announce), sinon fallback sur la version locale.

    `tags` doit refléter la portée de publication (ex: `["trackr-C411", "trackr-Torr9"]`
    si l'upload a été simultané sur les deux trackers).
    """
    torrent_path = result.tracker_torrent_path or plan.torrent_path
    if not torrent_path:
        return
    source_label = "tracker" if result.tracker_torrent_path else "local"

    try:
        with ui.console.status(
            f"[cyan]Ajout dans qBittorrent ({plan.name.upper()})…[/cyan]",
            spinner="dots",
        ):
            if cfg.qbt_auth_mode == "api_key":
                qbt.add_torrent(
                    cfg.qbt_url,
                    torrent_path,
                    save_path,
                    api_key=cfg.qbt_api_key,
                    tags=tags,
                )
            elif cfg.qbt_auth_mode == "login":
                sid = cfg.qbt_sid_cookie
                try:
                    if not sid:
                        sid = qbt.login(cfg.qbt_url, cfg.qbt_username, cfg.qbt_password)
                        cfg.qbt_sid_cookie = sid
                        save_config(cfg)
                    qbt.add_torrent(
                        cfg.qbt_url, torrent_path, save_path,
                        sid=sid, tags=tags,
                    )
                except qbt.QbtAuthError:
                    sid = qbt.login(cfg.qbt_url, cfg.qbt_username, cfg.qbt_password)
                    cfg.qbt_sid_cookie = sid
                    save_config(cfg)
                    qbt.add_torrent(
                        cfg.qbt_url, torrent_path, save_path,
                        sid=sid, tags=tags,
                    )
            else:
                ui.console.print(ui.error_panel("qBittorrent", "mode d'auth invalide"))
                return
    except qbt.QbtAuthError as e:
        ui.console.print(
            ui.error_panel(
                f"qBittorrent — auth refusée ({plan.name.upper()})",
                f"{e}\n[italic]Reconfigure le client dans Configuration.[/italic]",
            )
        )
        return
    except qbt.QbtError as e:
        ui.console.print(
            ui.error_panel(f"qBittorrent — ajout échoué ({plan.name.upper()})", str(e))
        )
        return

    ui.console.print(
        Panel(
            Text.from_markup(
                f"[bold {ui.SUCCESS}]✓ {plan.name.upper()} ajouté dans qBittorrent[/]\n"
                f"[{ui.MUTED}]save_path : {save_path}[/]\n"
                f"[{ui.MUTED}]info hash : {plan.info_hash}[/]\n"
                f"[{ui.MUTED}].torrent source : {source_label} "
                f"({'signé par le tracker' if source_label == 'tracker' else 'local — fallback'})[/]\n"
                f"[{ui.MUTED}][italic]Recheck en cours côté qBit ; le seed démarre dès "
                f"qu'il a validé les pièces.[/italic][/]"
            ),
            border_style=ui.SUCCESS,
        )
    )


def _show_final_summary(results, plans) -> None:
    ok_count = sum(1 for r in results if r.ok)
    style = ui.SUCCESS if ok_count == len(results) else ui.WARN if ok_count else ui.ERROR
    lines = [f"[bold {style}]{ok_count} / {len(results)} tracker(s) ont accepté l'upload.[/]", ""]
    for r in results:
        icon = "✓" if r.ok else "✗"
        col = ui.SUCCESS if r.ok else ui.ERROR
        lines.append(f"  [{col}]{icon}[/] [bold]{r.tracker.upper()}[/] — {r.message}")
        if r.info_hash:
            lines.append(f"      [{ui.MUTED}]info hash : {r.info_hash}[/]")
        if r.url_hint:
            lines.append(f"      [{ui.MUTED}]URL : {r.url_hint}[/]")
    ui.console.print(
        Panel(
            Text.from_markup("\n".join(lines)),
            title=f"[bold {style}]Résumé final[/]",
            border_style=style,
        )
    )

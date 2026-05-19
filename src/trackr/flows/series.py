"""Flow guidé d'upload d'une série vers C411 et/ou Torr9 (POST réel).

Glisser un fichier unique → épisode (S01E01). Glisser un dossier contenant
plusieurs épisodes d'une même saison → pack saison complète (S01).

La machinerie générique (POST, seed qBit, queue de retry, manifest, preview)
est réutilisée depuis `flows.movie` pour éviter toute duplication.
"""

from __future__ import annotations

import re
from pathlib import Path

import questionary
from rich.panel import Panel
from rich.table import Table

from trackr import ui
from trackr.config import Config, load_config
from trackr.media import torrent as torrent_mod
from trackr.media.lookup import MediaHit, lookup_by_id, search as tmdb_search
from trackr.media.mediainfo import MediaInfo, MediainfoError, probe, resolution_label
from trackr.nfo.builder import (
    build_description_bbcode,
    build_nfo,
    detect_dynamic_range,
    detect_team_tag,
    detect_version_markers,
    has_encoding_settings,
    suggest_title_c411,
    video_codec_tag,
)
from trackr.session import ensure_torr9_jwt
from trackr.trackers import c411 as c411_api
from trackr.trackers import c411_cats
from trackr.trackers import torr9_cats
from trackr.trackers.base import AuthError, TrackerError

from trackr.flows.movie import (
    TrackerPlan,
    _ask_c411_options,
    _ask_disc_structure,
    _ask_language_tag,
    _ask_source_hint,
    _ask_targets,
    _ask_vod_platform,
    _build_dir,
    _fetch_tracker_torrent,
    _normalize_path,
    _offer_seed,
    _post_plan,
    _print_post_result,
    _render_hit,
    _save_to_queue,
    _show_artifacts_recap,
    _show_final_summary,
    _show_preview,
    _write_manifest,
)

_VIDEO_EXT = {".mkv", ".mp4", ".m4v", ".avi", ".ts", ".m2ts"}

_SE_RX = re.compile(r"[Ss](\d{1,2})[\. _-]?[Ee](\d{1,3})")
_X_RX = re.compile(r"(?<!\d)(\d{1,2})x(\d{1,3})(?!\d)")
_S_RX = re.compile(r"(?:[Ss]aison|[Ss])[\. _-]?(\d{1,2})(?!\d)")


def _natural_key(name: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def _parse_se(name: str) -> tuple[int | None, int | None]:
    m = _SE_RX.search(name)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = _X_RX.search(name)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = _S_RX.search(name)
    if m:
        return int(m.group(1)), None
    return None, None


def run() -> None:
    ui.clear()
    ui.console.print(ui.banner())
    ui.console.print(
        Panel(
            "Flow guidé d'upload d'une série vers C411 et/ou Torr9.\n"
            "Fichier unique → épisode. Dossier multi-épisodes → pack saison.\n"
            f"[{ui.WARN}]POST réel après confirmation explicite.[/]",
            title=f"[bold {ui.ACCENT}]Uploader une série[/]",
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

    targets = _ask_targets(available)
    if not targets:
        return

    src_path = _ask_series_path()
    if not src_path:
        return

    collected = _collect_episodes(src_path)
    if not collected:
        return
    is_pack, season_episode, files, rep_file = collected

    if is_pack and not _check_coherence(files):
        ui.press_enter()
        return

    info = _probe_rep(rep_file)
    if not info:
        return
    if not _confirm_selection(info, rep_file, is_pack, season_episode, files):
        return

    source_hint = _ask_source_hint(rep_file)
    vod_platform = ""
    if source_hint in ("WEB", "WEB-DL", "WEBRip"):
        vod_platform = _ask_vod_platform()
    disc_structure = ""
    if source_hint == "BluRay":
        disc_structure = _ask_disc_structure(rep_file)
    language_tag = _ask_language_tag(rep_file, info)

    detected_team = detect_team_tag(rep_file)
    if detected_team != "NOTAG":
        ui.console.print(
            f"[{ui.WARN}]⚠ Filename porte le tag '-{detected_team}'. C411 sanctionne le DETAG "
            f"par omission (NOTAG sur un fichier qui a un tag identifiable).[/]"
        )
    team_label = (
        f"Tag TEAM (détecté : {detected_team}, sans le tiret) :"
        if detected_team != "NOTAG"
        else "Tag TEAM (sans le tiret, ex: NOTAG, FW, ZEKEY) :"
    )
    team_tag = questionary.text(team_label, default=detected_team).ask() or detected_team

    try:
        with ui.console.status("[cyan]Vérification du JWT Torr9 (pour recherche TMDB)…[/cyan]", spinner="dots"):
            jwt = ensure_torr9_jwt(cfg)
    except (AuthError, TrackerError) as e:
        ui.console.print(ui.error_panel("Recherche TMDB indisponible", str(e)))
        ui.press_enter()
        return

    hit = _search_tmdb_tv(cfg, jwt)
    if not hit:
        return

    try:
        nfo_text = build_nfo(rep_file)
    except MediainfoError as e:
        ui.console.print(ui.error_panel("Construction NFO échouée", str(e)))
        ui.press_enter()
        return
    if is_pack:
        ep_lines = "\n".join(f"  {f.name}" for f in files)
        nfo_text = (
            nfo_text.rstrip()
            + f"\n\n── Épisodes ({len(files)}) ───────────────────────────────────\n"
            + ep_lines
            + "\n"
        )
    is_reencode = has_encoding_settings(nfo_text)
    if not is_reencode:
        ui.console.print(
            f"[{ui.MUTED}]NFO sans « Encoding settings » → release directe : "
            f"codec en H264/H265 (pas x264/x265).[/]"
        )

    dynamic_range = detect_dynamic_range(rep_file, info)
    title_default = suggest_title_c411(
        hit, info, source=source_hint, language_tag=language_tag, team=team_tag,
        is_reencode=is_reencode,
        version_markers=detect_version_markers(rep_file),
        disc_structure=disc_structure,
        season_episode=season_episode,
        dynamic_range=dynamic_range,
    )
    ui.console.print(
        f"[{ui.MUTED}]Format : Nom.Année.Sxx[Eyy].[Marqueurs].Lang.Res."
        f"Source[.Structure][.HDR].Audio.Vidéo-TEAM (sans accents).[/]"
    )
    release_title = questionary.text("Titre release :", default=title_default).ask()
    if not release_title:
        return

    plans: list[TrackerPlan] = []
    for tracker in targets:
        ui.console.print()
        ui.console.print(f"[bold {ui.ACCENT}]── Configuration {tracker.upper()} ──[/]")
        try:
            if tracker == "c411":
                plan = _plan_c411_series(cfg, release_title, hit, language_tag)
            else:
                plan = _plan_torr9_series(cfg, release_title, hit, language_tag, source_hint)
        except (AuthError, TrackerError) as e:
            ui.console.print(ui.error_panel(f"Préparation {tracker} échouée", str(e)))
            ui.press_enter()
            return
        if plan is None:
            return
        plans.append(plan)

    out_dir = _build_dir(hit, info)
    ui.console.print(f"[{ui.MUTED}]Dossier de sortie : {out_dir}[/]")

    payload_size = sum(_safe_size(f) for f in files)
    description = build_description_bbcode(
        hit, info,
        release_title=release_title,
        source=source_hint,
        vod_platform=vod_platform,
        disc_structure=disc_structure,
        team_tag=team_tag,
        total_size=payload_size,
        file_count=len(files),
        dynamic_range=dynamic_range,
        season_episode=season_episode,
        episodes=[f.name for f in files] if is_pack else None,
    )
    nfo_path = out_dir / "release.nfo"
    nfo_path.write_text(nfo_text, encoding="utf-8")
    desc_path = out_dir / "release.description.bbcode"
    desc_path.write_text(description, encoding="utf-8")

    torrent_source = src_path if src_path.is_dir() else rep_file
    is_dir_source = src_path.is_dir()
    video_globs = sorted(f"*{e}" for e in _VIDEO_EXT)
    if is_dir_source:
        ui.console.print(
            f"[{ui.MUTED}]Dossier source : seuls les fichiers vidéo entrent dans le "
            f".torrent (vignettes, .trickplay, posters, .plexmatch… exclus).[/]"
        )
    for plan in plans:
        ui.console.print()
        torrent_out = out_dir / f"{plan.name}.torrent"
        try:
            built = torrent_mod.create_torrent(
                source_path=torrent_source,
                announce_url=plan.announce_url,
                output_path=torrent_out,
                source_tag=plan.source_tag,
                private=True,
                exclude_globs=["*"] if is_dir_source else None,
                include_globs=video_globs if is_dir_source else None,
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

    manifest_path = _write_manifest(
        out_dir, plans, torrent_source, hit, info, release_title,
        source_hint, vod_platform, language_tag, team_tag, nfo_path, desc_path,
    )
    _show_preview(plans, manifest_path, nfo_text, description)

    ui.console.print()
    confirm = questionary.confirm(
        f"Publier maintenant sur {' et '.join(p.name.upper() for p in plans)} ?",
        default=False,
    ).ask()
    if not confirm:
        ui.console.print(f"[{ui.MUTED}]Pas de POST. Les builds restent dans {out_dir}.[/]")
        ui.press_enter()
        return

    from trackr import pending as pending_mod

    results = []
    for plan in plans:
        result = _post_plan(plan, cfg, hit)
        if result.ok and plan.torrent_path:
            _fetch_tracker_torrent(plan, result, cfg)
            if plan.name == "c411" and result.info_hash:
                pending_mod.add("c411", result.info_hash, plan.title)
        results.append(result)
        _print_post_result(result)

    _show_final_summary(results, plans)

    if any(not r.ok for r in results):
        job_id = _save_to_queue(
            results, plans, torrent_source, hit,
            release_title=release_title,
            nfo_path=nfo_path, desc_path=desc_path, manifest_path=manifest_path,
        )
        if job_id:
            ui.console.print(
                f"\n[{ui.WARN}]💾 Upload partiellement échoué — sauvegardé en queue.[/]\n"
                f"[{ui.MUTED}]   Job id : {job_id}[/]"
            )

    _offer_seed(results, plans, cfg, torrent_source)
    _show_artifacts_recap(out_dir, plans, results)

    if questionary.confirm("\nUploader une autre série maintenant ?", default=False).ask():
        return run()

    ui.press_enter("Entrée pour revenir au menu")


# ─────────────────────────── steps ───────────────────────────


def _ask_series_path() -> Path | None:
    raw = questionary.path("Fichier OU dossier de la série (drag&drop OK) :").ask()
    if not raw:
        return None
    path = _normalize_path(raw)
    if not path.exists():
        ui.console.print(ui.error_panel("Chemin introuvable", str(path)))
        ui.press_enter()
        return None
    return path


def _safe_size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _collect_episodes(path: Path):
    """Renvoie (is_pack, season_episode, files_triés, rep_file) ou None."""
    if path.is_file():
        if path.suffix.lower() not in _VIDEO_EXT:
            ui.console.print(ui.error_panel("Pas un fichier vidéo", str(path)))
            ui.press_enter()
            return None
        files = [path]
    else:
        files = sorted(
            (p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in _VIDEO_EXT),
            key=lambda p: _natural_key(p.name),
        )
    if not files:
        ui.console.print(ui.error_panel("Aucune vidéo trouvée", str(path)))
        ui.press_enter()
        return None

    parsed = [(_parse_se(f.name)) for f in files]
    seasons = {s for s, _ in parsed if s is not None}

    if len(files) == 1:
        season, ep = parsed[0]
        if season is None:
            season = _ask_int("Numéro de saison ?", 1)
        if ep is None:
            ep = _ask_int("Numéro d'épisode ?", 1)
        if season is None or ep is None:
            return None
        return False, f"S{season:02d}E{ep:02d}", files, files[0]

    if len(seasons) > 1:
        ui.console.print(
            ui.error_panel(
                "Pack multi-saisons non géré",
                "Ce dossier contient plusieurs saisons "
                f"({', '.join(f'S{s:02d}' for s in sorted(seasons))}). "
                "Uploade une saison à la fois (un dossier = une saison complète).",
            )
        )
        ui.press_enter()
        return None

    season = next(iter(seasons)) if seasons else _ask_int("Numéro de saison du pack ?", 1)
    if season is None:
        return None

    eps = sorted(e for _, e in parsed if e is not None)
    if eps:
        expected = set(range(min(eps), max(eps) + 1))
        missing = sorted(expected - set(eps))
        if missing:
            ui.console.print(
                f"[{ui.WARN}]⚠ Épisodes potentiellement manquants : "
                f"{', '.join(f'E{m:02d}' for m in missing)}.[/]\n"
                f"[{ui.MUTED}][italic]C411 interdit les saisons incomplètes "
                f"(soit 1 épisode, soit la saison complète, soit l'intégrale).[/italic][/]"
            )
            if not questionary.confirm("Continuer quand même (saison réellement complète) ?", default=False).ask():
                return None

    return True, f"S{season:02d}", files, files[0]


def _ask_int(label: str, default: int) -> int | None:
    raw = questionary.text(label, default=str(default)).ask()
    if raw is None:
        return None
    raw = raw.strip()
    if not raw.isdigit():
        ui.console.print(f"[{ui.WARN}]Valeur invalide, on prend {default}.[/]")
        return default
    return int(raw)


def _audio_sig(info: MediaInfo) -> tuple:
    langs = tuple(sorted({(a.language or "").lower()[:2] for a in info.audio}))
    codecs = tuple(sorted({(a.codec or "").upper() for a in info.audio}))
    return langs, codecs


def _signature(info: MediaInfo, file_path: Path) -> tuple:
    return (
        resolution_label(info),
        video_codec_tag(info.video.codec, pure=True),
        info.container.lower(),
        _audio_sig(info),
        detect_team_tag(file_path),
    )


def _check_coherence(files: list[Path]) -> bool:
    """C411 : pack homogène obligatoire (résolution / codec / langues / team).

    Refuse si un fichier diverge du premier (multi-format / multi-tag interdit).
    """
    ref_sig = None
    ref_file = None
    divergent: list[tuple[Path, tuple]] = []
    with ui.console.status("[cyan]Vérification de la cohérence du pack…[/cyan]", spinner="dots") as st:
        for i, f in enumerate(files, 1):
            st.update(f"[cyan]Analyse {i}/{len(files)} — {f.name}[/cyan]")
            try:
                info = probe(f)
            except MediainfoError as e:
                ui.console.print(ui.error_panel("MediaInfo a échoué", f"{f.name} : {e}"))
                return False
            sig = _signature(info, f)
            if ref_sig is None:
                ref_sig, ref_file = sig, f
            elif sig != ref_sig:
                divergent.append((f, sig))

    if divergent:
        lines = [
            f"Référence ({ref_file.name}) :",
            f"  {ref_sig}",
            "",
            "Fichiers divergents (C411 interdit les packs multi-format / multi-tag) :",
        ]
        for f, sig in divergent[:8]:
            lines.append(f"  {f.name}")
            lines.append(f"    {sig}")
        if len(divergent) > 8:
            lines.append(f"  … et {len(divergent) - 8} autre(s).")
        ui.console.print(ui.error_panel("Pack non homogène", "\n".join(lines)))
        return False
    return True


def _probe_rep(rep_file: Path) -> MediaInfo | None:
    try:
        with ui.console.status("[cyan]Analyse mediainfo…[/cyan]", spinner="dots"):
            return probe(rep_file)
    except MediainfoError as e:
        ui.console.print(ui.error_panel("MediaInfo a échoué", str(e)))
        ui.press_enter()
        return None


def _confirm_selection(
    info: MediaInfo, rep_file: Path, is_pack: bool, season_episode: str, files: list[Path]
) -> bool:
    from trackr.flows.inspect import _render_panel

    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim", width=14)
    grid.add_column()
    grid.add_row("Mode", "Pack saison" if is_pack else "Épisode unique")
    grid.add_row("Tag", season_episode)
    grid.add_row("Fichiers", str(len(files)))
    if is_pack:
        grid.add_row("Référence", rep_file.name)
        total = sum(_safe_size(f) for f in files)
        grid.add_row("Poids total", f"{total / (1 << 30):.2f} GB")
    ui.console.print(
        Panel(grid, title=f"[bold {ui.ACCENT}]Sélection série[/]", border_style=ui.ACCENT)
    )
    ui.console.print(_render_panel(info))
    return bool(
        questionary.confirm(
            "Continuer avec cette sélection ?" if is_pack else "Continuer avec ce fichier ?",
            default=True,
        ).ask()
    )


def _search_tmdb_tv(cfg: Config, jwt: str) -> MediaHit | None:
    raw = questionary.text("Recherche TMDB (série) — titre OU id numérique :").ask()
    if not raw:
        return None
    raw = raw.strip()
    try:
        with ui.console.status("[cyan]Interrogation TMDB…[/cyan]", spinner="dots"):
            session = cfg.c411_session if cfg.c411_session_valid() else ""
            if raw.isdigit():
                hit = lookup_by_id(int(raw), c411_session=session, torr9_jwt=jwt, category="tv")
                hits = [hit] if hit else []
            else:
                hits = tmdb_search(raw, c411_session=session, torr9_jwt=jwt, category="tv", limit=10)
    except (AuthError, TrackerError) as e:
        ui.console.print(ui.error_panel("Recherche échouée", str(e)))
        ui.press_enter()
        return None

    if not hits:
        ui.console.print(ui.warn_panel("Aucun résultat", "Réessaie avec un autre titre ou id."))
        return _search_tmdb_tv(cfg, jwt)

    if len(hits) == 1:
        ui.console.print(_render_hit(hits[0]))
        if questionary.confirm("C'est bien cette série ?", default=True).ask():
            return hits[0]
        return _search_tmdb_tv(cfg, jwt)

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
    picked_id = questionary.select("Choisis la bonne série :", choices=choices).ask()
    if picked_id == -1 or picked_id is None:
        return _search_tmdb_tv(cfg, jwt)
    for h in hits:
        if h.tmdb_id == picked_id:
            ui.console.print(_render_hit(h))
            return h
    return None


# ─────────────────────────── plans ───────────────────────────


def _plan_c411_series(
    cfg: Config, release_title: str, hit: MediaHit, language_tag: str
) -> TrackerPlan | None:
    cat = c411_cats.movies_category()
    sub_choices = [questionary.Choice(s.name, value=s.id) for s in cat.subs]
    sub_id = questionary.select(
        "Sous-catégorie C411 :",
        choices=sub_choices,
        default=7,  # Série TV
    ).ask()
    if sub_id is None:
        return None
    sub = next(s for s in cat.subs if s.id == sub_id)

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
        source_tag="C411",
        category_id=cat.id,
        category_name=cat.name,
        subcategory_id=sub.id,
        subcategory_name=sub.name,
        title=release_title,
        options=options,
    )


def _plan_torr9_series(
    cfg: Config, release_title: str, hit: MediaHit, language_tag: str, source_hint: str
) -> TrackerPlan | None:
    cat = torr9_cats.series_category()
    sub_choices = [questionary.Choice(s.name, value=s.id) for s in cat.subs]
    sub_id = questionary.select(
        "Sous-catégorie Torr9 :",
        choices=sub_choices,
        default=5,  # Séries TV
    ).ask()
    if sub_id is None:
        return None
    sub = next(s for s in cat.subs if s.id == sub_id)

    tags = [t for t in [source_hint, language_tag, "FR"] if t]
    if hit.year:
        tags.append(str(hit.year))

    announce = f"https://tracker.torr9.net/announce/{cfg.torr9_passkey}"
    return TrackerPlan(
        name="torr9",
        announce_url=announce,
        source_tag="Torr9",
        category_id=cat.id,
        category_name=cat.name,
        subcategory_id=sub.id,
        subcategory_name=sub.name,
        title=release_title,
        tags=tags,
    )

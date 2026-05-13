"""Résolution des rejets C411 (notifications `torrent_revision_requested`).

L'utilisateur sélectionne un rejet, on affiche la raison complète du
modérateur, on tente de retrouver le build local (manifest) pour pouvoir
régénérer automatiquement le titre selon les règles C411 (codec H26x vs
x26x selon présence d'« Encoding settings » dans le NFO, suggestion de
plateforme VOD selon le débit). L'utilisateur ajuste, on PATCH le torrent
existant et on marque la notif comme lue. Pas de re-seed : le info_hash
ne change pas, qBittorrent seed déjà le bon fichier.
"""

from __future__ import annotations

import json
from pathlib import Path

import questionary
from platformdirs import user_cache_dir
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from trackr import ui
from trackr.config import Config, load_config
from trackr.media.lookup import MediaHit
from trackr.media.mediainfo import MediaInfo, MediainfoError, probe
from trackr.nfo.builder import (
    build_description_bbcode,
    has_encoding_settings,
    suggest_title_c411,
)
from trackr.trackers import c411 as c411_api
from trackr.trackers.base import AuthError, TrackerError


# Plages de bitrate vidéo attendues par C411 selon la plateforme VOD (1080p).
# Source : https://c411.org/wiki/cat-video
_NETFLIX_RANGE_KBPS = (5000, 7500)


VOD_PLATFORMS = [
    "Netflix",
    "Amazon",
    "Disney+",
    "AppleTV+",
    "Canal+",
    "Paramount+",
    "Max",
    "Crunchyroll",
    "YouTube",
    "",  # Inconnu
]


def run() -> None:
    ui.clear()
    ui.console.print(ui.banner())

    cfg = load_config()
    if not cfg.c411_session or not cfg.c411_session_valid():
        ui.console.print(
            ui.error_panel(
                "Session C411 requise",
                "Reconfigure C411 en mode Guidé pour rafraîchir la session web.",
            )
        )
        ui.press_enter()
        return

    try:
        with ui.console.status("[cyan]Récupération des notifications C411…[/cyan]", spinner="dots"):
            rejs = c411_api.list_rejections(cfg.c411_session)
    except (AuthError, TrackerError) as e:
        ui.console.print(ui.error_panel("Échec récupération rejets", str(e)))
        ui.press_enter()
        return

    if not rejs:
        ui.console.print(
            ui.success_panel(
                "Aucun rejet",
                "Tous tes uploads sont validés ou en attente. Rien à corriger.",
            )
        )
        ui.press_enter()
        return

    ui.console.print(
        Panel(
            f"{len(rejs)} rejet(s) C411 à corriger.\n"
            f"[{ui.MUTED}][italic]Le info_hash ne change pas — qBittorrent continue à seed sans intervention.[/italic][/]",
            title=f"[bold {ui.ERROR}]🚨 Rejets C411[/]",
            border_style=ui.ERROR,
        )
    )

    while True:
        choices = [_reject_choice(r) for r in rejs]
        choices.append(questionary.Choice("← Retour", value="back"))
        action = questionary.select("Quel rejet veux-tu corriger ?", choices=choices).ask()
        if action in (None, "back"):
            return

        r = next((x for x in rejs if x.notification_id == action), None)
        if not r:
            return

        _process_rejection(r, cfg)

        # Refresh la liste après action
        try:
            rejs = c411_api.list_rejections(cfg.c411_session)
        except (AuthError, TrackerError):
            return
        if not rejs:
            ui.console.print(f"\n[{ui.SUCCESS}]✓ Plus aucun rejet en attente.[/]")
            ui.press_enter()
            return


def _reject_choice(r: c411_api.Rejection) -> questionary.Choice:
    title = (r.torrent_name or "?")[:55]
    label = f"#{r.notification_id}  ·  {title}"
    return questionary.Choice(label, value=r.notification_id)


def _process_rejection(r: c411_api.Rejection, cfg: Config) -> None:
    ui.console.print()
    ui.console.print(
        Panel(
            Text(r.reason.strip()),
            title=f"[bold {ui.ERROR}]Raison du modérateur — {r.torrent_name}[/]",
            border_style=ui.ERROR,
        )
    )
    ui.console.print()

    # Récupérer l'état actuel du torrent depuis C411 (titre, description, options)
    try:
        with ui.console.status("[cyan]Lecture de l'état serveur…[/cyan]", spinner="dots"):
            current = c411_api.fetch_torrent(cfg.c411_session, r.info_hash)
    except (AuthError, TrackerError) as e:
        ui.console.print(ui.error_panel("Lecture serveur impossible", str(e)))
        ui.press_enter()
        return

    if current.get("status") != "revision_requested":
        ui.console.print(
            ui.warn_panel(
                "Statut différent",
                f"Le torrent est en statut « {current.get('status')} » côté serveur — "
                "il n'a peut-être plus besoin de correction. Marquer la notif comme lue ?",
            )
        )
        if questionary.confirm("Marquer comme lu ?", default=True).ask():
            _mark_read(r, cfg)
        return

    server_title = str(current.get("name") or "")
    server_desc = str(current.get("description") or "")

    # Essai de matcher un build local pour regen automatique
    manifest = _find_local_manifest(r.info_hash)
    new_title = server_title
    new_desc = server_desc
    new_options = _extract_options(current)

    if manifest:
        ui.console.print(
            f"[{ui.MUTED}]Build local trouvé : {manifest['__path']}[/]"
        )
        regen_title, regen_desc = _regen_artifacts(manifest, r.reason)
        if regen_title and regen_title != server_title:
            ui.console.print(
                f"[{ui.SUCCESS}]Titre regen :[/] [bold]{regen_title}[/]\n"
                f"[{ui.MUTED}]ancien     : {server_title}[/]"
            )
            if questionary.confirm("Adopter le nouveau titre ?", default=True).ask():
                new_title = regen_title
        if regen_desc:
            if questionary.confirm(
                "Régénérer la description (Poids total corrigé, plateforme VOD ajustée) ?",
                default=True,
            ).ask():
                new_desc = regen_desc
    else:
        ui.console.print(
            f"[{ui.MUTED}]Pas de build local pour ce info_hash — édition manuelle uniquement.[/]"
        )

    # Édition manuelle finale
    if questionary.confirm("Éditer le titre manuellement ?", default=False).ask():
        edited = questionary.text("Nouveau titre :", default=new_title).ask()
        if edited:
            new_title = edited

    edit_desc = questionary.confirm("Éditer la description manuellement ?", default=False).ask()
    if edit_desc:
        ui.console.print(
            f"[{ui.MUTED}]Ouverture éditeur — sauve et ferme pour valider.[/]"
        )
        new_desc = _edit_in_editor(new_desc) or new_desc

    # Récap avant PATCH
    ui.console.print()
    ui.console.print(
        Panel(
            Text.from_markup(
                f"[bold]Titre :[/] {new_title}\n"
                f"[bold]Description :[/] {len(new_desc)} caractères\n"
                f"[bold]Options :[/] {new_options or '—'}"
            ),
            title=f"[bold {ui.ACCENT}]Modifications à envoyer[/]",
            border_style=ui.ACCENT,
        )
    )
    if not questionary.confirm("Envoyer le PATCH à C411 ?", default=True).ask():
        ui.console.print(f"[{ui.MUTED}]Annulé.[/]")
        return

    try:
        with ui.console.status("[cyan]PATCH C411…[/cyan]", spinner="dots"):
            res = c411_api.edit_torrent(
                cfg.c411_session,
                r.info_hash,
                title=new_title,
                description=new_desc,
                description_format="standard",
                options=new_options or None,
            )
    except (AuthError, TrackerError) as e:
        ui.console.print(ui.error_panel("PATCH refusé", str(e)))
        return

    if res.success:
        modified = ", ".join(res.modified_fields) if res.modified_fields else "rien"
        # PATCH seul ne renvoie pas à la validation — il faut un POST /resubmit explicite.
        resubmit_msg = ""
        try:
            with ui.console.status("[cyan]Renvoi à la validation…[/cyan]", spinner="dots"):
                resubmit_msg = c411_api.resubmit_torrent(cfg.c411_session, r.info_hash)
        except (AuthError, TrackerError) as e:
            ui.console.print(ui.warn_panel(
                "Resubmit refusé",
                f"Le PATCH est passé mais le renvoi à la validation a échoué : {e}\n"
                "Tu peux cliquer manuellement sur « Renvoyer à la validation » côté site.",
            ))
            return
        ui.console.print(
            ui.success_panel(
                "Torrent corrigé et resoumis",
                f"Champs modifiés : {modified}\n✓ {resubmit_msg}",
            )
        )
        # Track le torrent en pending validation pour qu'il apparaisse dans le dashboard.
        from trackr import pending as pending_mod
        pending_mod.add("c411", r.info_hash, new_title)
        _mark_read(r, cfg)
    else:
        ui.console.print(ui.error_panel("Réponse C411 inattendue", res.message or "—"))


def _mark_read(r: c411_api.Rejection, cfg: Config) -> None:
    try:
        c411_api.mark_notification_read(cfg.c411_session, r.notification_id)
    except (AuthError, TrackerError) as e:
        ui.console.print(f"[{ui.MUTED}]Notif #{r.notification_id} non marquée lue : {e}[/]")


def _extract_options(current: dict) -> dict:
    """Re-extrait les options choisies depuis la réponse GET /api/torrents/{hash}."""
    meta = current.get("metadata") or {}
    raw = meta.get("options") or []
    out: dict = {}
    for opt in raw:
        oid = opt.get("optionId") or opt.get("id")
        vals = opt.get("values") or []
        ids = [v.get("id") for v in vals if v.get("id") is not None]
        if not oid or not ids:
            continue
        if opt.get("allowsMultiple", True):
            out[str(oid)] = ids
        else:
            out[str(oid)] = ids[0]
    return out


def _find_local_manifest(info_hash: str) -> dict | None:
    """Cherche un manifest.json dans le cache builds dont le c411.info_hash matche."""
    base = Path(user_cache_dir("trackr")) / "builds"
    if not base.exists():
        return None
    for manifest_path in sorted(base.glob("*/manifest.json"), reverse=True):
        try:
            m = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for plan in m.get("plans", []):
            if plan.get("tracker") == "c411" and plan.get("info_hash") == info_hash:
                m["__path"] = str(manifest_path.parent)
                m["__c411_plan"] = plan
                return m
    return None


def _suggest_vod_from_bitrate(video_bitrate: int, current_vod: str) -> str:
    """Si plateforme actuelle = Netflix mais débit hors plage Netflix, retire-la."""
    if current_vod != "Netflix":
        return current_vod
    lo, hi = _NETFLIX_RANGE_KBPS
    kbps = video_bitrate // 1000
    if lo <= kbps <= hi:
        return current_vod
    return ""  # à choisir par l'user dans le prompt suivant


def _regen_artifacts(manifest: dict, reason: str) -> tuple[str, str]:
    """Regen titre + description en appliquant les corrections déduites du rejet.

    Heuristiques :
    - NFO sans `Encoding settings` ⇒ codec direct (H265/H264), pas x26x.
    - Si rejet mentionne « WEB-DL Netflix » + débit hors plage ⇒ VOD à corriger.
    - Poids total = file size réel (déjà géré par build_description_bbcode).
    """
    nfo_path = Path(manifest.get("nfo_path") or "")
    src_path = Path(manifest.get("source_file") or "")
    if not nfo_path.exists() or not src_path.exists():
        return "", ""

    try:
        info: MediaInfo = probe(src_path)
    except MediainfoError:
        return "", ""

    nfo_text = nfo_path.read_text(encoding="utf-8", errors="ignore")
    is_reencode = has_encoding_settings(nfo_text)

    tmdb = manifest.get("tmdb") or {}
    hit = MediaHit(
        tmdb_id=int(tmdb.get("id") or 0),
        media_type=str(tmdb.get("media_type") or "movie"),
        title=str(tmdb.get("title") or ""),
        year=str(tmdb.get("year") or ""),
        rating=float(tmdb.get("rating") or 0.0),
        poster_url=str(tmdb.get("poster_url") or ""),
        description=str(tmdb.get("description") or ""),
    )

    source = str(manifest.get("source") or "WEB")
    language_tag = str(manifest.get("language_tag") or "VO")
    team_tag = str(manifest.get("team_tag") or "NOTAG")
    vod = str(manifest.get("vod_platform") or "")

    # Ajustement plateforme VOD si débit Netflix incohérent
    vod_corrected = _suggest_vod_from_bitrate(info.video.bitrate, vod)
    if vod_corrected != vod:
        ui.console.print(
            f"[{ui.WARN}]⚠ Débit {info.video.bitrate // 1000} kb/s incohérent avec "
            f"« Netflix » (plage attendue 5000-7500). Plateforme à reconfirmer.[/]"
        )
        vod = _prompt_vod(default=vod_corrected or "")

    new_title = suggest_title_c411(
        hit,
        info,
        source=source,
        language_tag=language_tag,
        team=team_tag,
        is_reencode=is_reencode,
    )

    # Taille payload réelle (déjà l'approche corrigée)
    try:
        payload_size = src_path.stat().st_size
    except OSError:
        payload_size = info.file_size

    new_desc = build_description_bbcode(
        hit,
        info,
        release_title=new_title,
        source=source,
        vod_platform=vod,
        team_tag=team_tag,
        total_size=payload_size,
    )

    return new_title, new_desc


def _prompt_vod(default: str) -> str:
    """Demande à l'user la plateforme VOD (sélecteur + saisie libre)."""
    choices = [questionary.Choice(p or "Inconnu / non précisé", value=p) for p in VOD_PLATFORMS]
    choices.append(questionary.Choice("Autre (saisie libre)", value="__custom__"))
    pick = questionary.select(
        "Plateforme VOD source ?",
        choices=choices,
        default=default if default in VOD_PLATFORMS else "",
    ).ask()
    if pick == "__custom__":
        return questionary.text("Nom de la plateforme :").ask() or ""
    return pick or ""


def _edit_in_editor(initial: str) -> str:
    """Ouvre $EDITOR (ou nano) avec un buffer temporaire et renvoie le contenu édité."""
    import os
    import subprocess
    import tempfile

    editor = os.environ.get("EDITOR") or "nano"
    with tempfile.NamedTemporaryFile("w+", suffix=".bbcode", delete=False, encoding="utf-8") as tmp:
        tmp.write(initial)
        tmp_path = tmp.name
    try:
        subprocess.call([editor, tmp_path])
        return Path(tmp_path).read_text(encoding="utf-8")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

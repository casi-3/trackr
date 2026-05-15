"""Gestion des brouillons (drafts) C411.

Quand un POST upload renvoie 429 (quota d'uploads en attente atteint), le flow
sauve automatiquement en brouillon côté C411. Ce module permet ensuite de :

- lister les brouillons
- publier un brouillon (POST /api/torrents avec les données du build local + DELETE draft)
- supprimer un brouillon (le draft uniquement, pas le build local)

La reprise nécessite que les artefacts (.torrent, .nfo, description, manifest)
soient toujours présents dans le cache local `~/.cache/trackr/builds/...`.
Si supprimés, on peut juste supprimer le draft côté C411 ou le publier
manuellement via le site web.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import questionary
from platformdirs import user_cache_dir
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from trackr import pending as pending_mod
from trackr import ui
from trackr.config import Config, load_config
from trackr.trackers import c411 as c411_api
from trackr.trackers.base import AuthError, TrackerError


@dataclass
class DraftEntry:
    """Combine un draft C411 avec son cache local s'il est retrouvé."""
    draft: c411_api.Draft
    cache_dir: Path | None = None    # dossier ~/.cache/trackr/builds/...
    torrent_path: Path | None = None
    nfo_path: Path | None = None
    manifest: dict | None = None

    @property
    def has_local_artifacts(self) -> bool:
        return (
            self.cache_dir is not None
            and self.torrent_path is not None
            and self.torrent_path.exists()
            and self.nfo_path is not None
            and self.nfo_path.exists()
        )


# ─────────────────────────── entrypoint ───────────────────────────


def count() -> int:
    """Nombre de brouillons C411 (rapide, pour badge menu). 0 si erreur."""
    cfg = load_config()
    if not cfg.is_c411_ready():
        return 0
    try:
        return len(c411_api.list_drafts(cfg.c411_api_key))
    except (AuthError, TrackerError):
        return 0


def run() -> None:
    ui.clear()
    ui.console.print(
        Panel(
            "Brouillons C411 — torrents préparés mais pas encore soumis à la validation.\n"
            f"[{ui.MUTED}]Tu peux publier (POST upload + suppression brouillon) ou supprimer un brouillon.[/]",
            title=f"[bold {ui.ACCENT}]📝 Brouillons C411[/]",
            border_style=ui.ACCENT,
        )
    )

    cfg = load_config()
    if not cfg.is_c411_ready():
        ui.console.print(ui.error_panel("C411 pas configuré", "Va dans Configuration → Identifiants C411."))
        ui.press_enter()
        return

    try:
        with ui.console.status("[cyan]Récupération des brouillons…[/cyan]", spinner="dots"):
            drafts = c411_api.list_drafts(cfg.c411_api_key)
    except AuthError as e:
        ui.console.print(ui.error_panel("Auth refusée", str(e)))
        ui.press_enter()
        return
    except TrackerError as e:
        ui.console.print(ui.error_panel("Erreur réseau", str(e)))
        ui.press_enter()
        return

    if not drafts:
        ui.console.print(
            ui.info_panel("Aucun brouillon", "Tu n'as aucun brouillon C411 actuellement.")
        )
        ui.press_enter()
        return

    entries = [_attach_local_artifacts(d) for d in drafts]

    while True:
        ui.clear()
        ui.console.print(_render_drafts_table(entries))
        ui.console.print()

        choices = []
        for i, e in enumerate(entries):
            ok = "📦" if e.has_local_artifacts else "⚠"
            title = (e.draft.title or _manifest_title(e) or "(sans titre)")[:60]
            label = f"{ok}  #{e.draft.id}  {title}"
            choices.append(questionary.Choice(label, value=i))
        choices.append(questionary.Choice("← Retour", value=None))

        idx = questionary.select("Choisis un brouillon :", choices=choices).ask()
        if idx is None:
            return

        e = entries[idx]
        action = _ask_action(e)
        if action is None:
            continue

        if action == "publish":
            if _do_publish(e, cfg):
                # Retirer de la liste, re-fetch les counts
                entries.pop(idx)
                if not entries:
                    ui.console.print(f"[{ui.MUTED}]Plus de brouillons.[/]")
                    ui.press_enter()
                    return
        elif action == "delete":
            if _do_delete(e, cfg):
                entries.pop(idx)
                if not entries:
                    ui.console.print(f"[{ui.MUTED}]Plus de brouillons.[/]")
                    ui.press_enter()
                    return


# ─────────────────────────── helpers ───────────────────────────


def _attach_local_artifacts(draft: c411_api.Draft) -> DraftEntry:
    """Scanne ~/.cache/trackr/builds/*/manifest.json pour retrouver le build local."""
    builds_root = Path(user_cache_dir("trackr")) / "builds"
    entry = DraftEntry(draft=draft)
    if not builds_root.is_dir():
        return entry
    for manifest_path in builds_root.glob("*/manifest.json"):
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for p in data.get("plans", []):
            if p.get("tracker") == "c411" and int(p.get("c411_draft_id") or 0) == draft.id:
                entry.cache_dir = manifest_path.parent
                tp = p.get("torrent_path")
                if tp:
                    entry.torrent_path = Path(tp)
                np = entry.cache_dir / "release.nfo"
                if np.exists():
                    entry.nfo_path = np
                entry.manifest = data
                return entry
    return entry


def _render_drafts_table(entries: list[DraftEntry]) -> Table:
    table = Table(title=f"[bold {ui.ACCENT}]Brouillons C411 ({len(entries)} / 15)[/]")
    table.add_column("#", style="dim", width=3)
    table.add_column("ID", style="cyan")
    table.add_column("Titre")
    table.add_column("Catégorie", style="dim")
    table.add_column("Mise à jour", style="dim")
    table.add_column("Reprise")
    for i, e in enumerate(entries, 1):
        cat = _format_category(e)
        title = e.draft.title or _manifest_title(e) or "(sans titre)"
        updated = (e.draft.updated_at or e.draft.created_at or "")[:10]
        reprise = (
            f"[{ui.SUCCESS}]possible[/]" if e.has_local_artifacts
            else f"[{ui.WARN}]cache absent[/]"
        )
        table.add_row(str(i), str(e.draft.id), title, cat, updated, reprise)
    return table


def _format_category(e: DraftEntry) -> str:
    """Catégorie/sous-catégorie depuis l'API, fallback sur le manifest local."""
    c411_plan: dict = {}
    if e.manifest:
        c411_plan = next((p for p in (e.manifest.get("plans") or []) if p.get("tracker") == "c411"), {})
    cat = (
        e.draft.category_name
        or c411_plan.get("category_name")
        or (str(e.draft.category_id) if e.draft.category_id else None)
        or (str(c411_plan.get("category_id")) if c411_plan.get("category_id") else "?")
    )
    sub = (
        e.draft.subcategory_name
        or c411_plan.get("subcategory_name")
        or (str(e.draft.subcategory_id) if e.draft.subcategory_id else None)
        or (str(c411_plan.get("subcategory_id")) if c411_plan.get("subcategory_id") else "?")
    )
    return f"{cat} / {sub}"


def _manifest_title(e: DraftEntry) -> str:
    if not e.manifest:
        return ""
    plans = e.manifest.get("plans") or []
    c411_plan = next((p for p in plans if p.get("tracker") == "c411"), {})
    return str(c411_plan.get("title") or "")


def _ask_action(e: DraftEntry) -> str | None:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim", width=14)
    grid.add_column()
    grid.add_row("ID", str(e.draft.id))
    grid.add_row("Titre", e.draft.title or _manifest_title(e) or "(sans titre)")
    grid.add_row("Catégorie", _format_category(e))
    if e.cache_dir:
        grid.add_row("Cache local", str(e.cache_dir))
    else:
        grid.add_row("Cache local", f"[{ui.WARN}]introuvable — publication auto impossible[/]")

    ui.console.print(Panel(grid, title=f"[bold {ui.ACCENT}]Brouillon #{e.draft.id}[/]", border_style=ui.ACCENT))

    choices = []
    if e.has_local_artifacts:
        choices.append(questionary.Choice("📤  Publier maintenant (POST upload + suppression brouillon)", value="publish"))
    else:
        choices.append(questionary.Choice(
            "📤  Publier (impossible — cache local introuvable)",
            value="publish_no_cache",
            disabled="cache absent",
        ))
    choices.append(questionary.Choice("🗑   Supprimer le brouillon (côté C411)", value="delete"))
    choices.append(questionary.Choice("← Retour à la liste", value=None))

    return questionary.select("Action :", choices=choices).ask()


def _do_publish(e: DraftEntry, cfg: Config) -> bool:
    if not e.has_local_artifacts:
        ui.console.print(ui.error_panel(
            "Cache absent",
            "Impossible de publier auto — les fichiers .torrent/.nfo locaux ne sont plus là.\n"
            "Publie manuellement via https://c411.org/user/drafts ou supprime le brouillon.",
        ))
        ui.press_enter()
        return False

    # Récupère les params depuis le manifest local
    plans = (e.manifest or {}).get("plans", [])
    c411_plan = next((p for p in plans if p.get("tracker") == "c411"), {})
    options = c411_plan.get("options") or {}
    title = c411_plan.get("title") or e.draft.title

    # Catégories : source de vérité = manifest local (l'API list_drafts ne ressort pas
    # ces IDs et renvoie 0/0). Fallback sur l'API si jamais le manifest est lacunaire.
    category_id = int(c411_plan.get("category_id") or e.draft.category_id or 0)
    subcategory_id = int(c411_plan.get("subcategory_id") or e.draft.subcategory_id or 0)
    if not category_id or not subcategory_id:
        ui.console.print(ui.error_panel(
            "Catégorie introuvable",
            "Impossible de retrouver la catégorie/sous-catégorie du brouillon "
            "(ni dans le cache local, ni dans la réponse C411). "
            "Publie manuellement via https://c411.org/user/drafts.",
        ))
        ui.press_enter()
        return False

    desc_path = e.cache_dir / "release.description.bbcode" if e.cache_dir else None
    description = ""
    if desc_path and desc_path.exists():
        description = desc_path.read_text(encoding="utf-8")
    else:
        ui.console.print(ui.error_panel(
            "Description manquante",
            f"Le fichier description.bbcode est absent dans {e.cache_dir}.",
        ))
        ui.press_enter()
        return False

    # rawg_data depuis manifest (re-fetch via API si manquant — anciens manifests
    # ne stockaient que rawg_id).
    rawg_data = (e.manifest or {}).get("rawg_data") or {}
    if not rawg_data:
        rawg_id = (e.manifest or {}).get("rawg_id")
        if rawg_id and cfg.c411_session_valid():
            try:
                lookup = c411_api.rawg_lookup(cfg.c411_session, int(rawg_id), presentation=False)
                rawg_data = lookup.game
            except (AuthError, TrackerError):
                rawg_data = {}

    if not questionary.confirm(
        f"Publier le brouillon #{e.draft.id} ({title}) ?",
        default=False,
    ).ask():
        return False

    try:
        with ui.console.status("[cyan]POST upload sur C411…[/cyan]", spinner="dots"):
            res = c411_api.upload(
                cfg.c411_api_key,
                torrent_path=e.torrent_path,
                nfo_path=e.nfo_path,
                title=title,
                category_id=category_id,
                subcategory_id=subcategory_id,
                description=description,
                description_format="standard",
                options=options,
                rawg_data=rawg_data or None,
            )
    except c411_api.QuotaError as ex:
        ui.console.print(ui.warn_panel(
            "Quota toujours plein",
            f"{ex}\nLe brouillon est conservé. Réessaie plus tard.",
        ))
        ui.press_enter()
        return False
    except AuthError as ex:
        ui.console.print(ui.error_panel("Auth refusée", str(ex)))
        ui.press_enter()
        return False
    except TrackerError as ex:
        ui.console.print(ui.error_panel("Upload échoué", str(ex)))
        ui.press_enter()
        return False

    # Upload OK → on supprime le draft côté serveur
    try:
        msg = c411_api.delete_draft(cfg.c411_api_key, e.draft.id)
    except (AuthError, TrackerError):
        msg = "(suppression brouillon non confirmée — supprime manuellement si besoin)"

    # Tracking pending dashboard
    if res.info_hash:
        pending_mod.add("c411", res.info_hash, title)

    ui.console.print(ui.success_panel(
        "✓ Brouillon publié",
        f"Status : [bold]{res.status or 'pending'}[/]\n"
        f"Hash   : [{ui.MUTED}]{res.info_hash or '?'}[/]\n"
        f"URL    : https://c411.org/torrent/{res.info_hash}\n"
        f"Brouillon : {msg}",
    ))

    # Maintenant qu'on a un info_hash signé serveur, on déclenche le seed
    # (fetch du .torrent re-signé puis ajout dans qBittorrent).
    _offer_seed_after_publish(e, res, cfg, title)

    ui.press_enter()
    return True


def _offer_seed_after_publish(
    e: DraftEntry,
    res: c411_api.UploadResult,
    cfg: Config,
    title: str,
) -> None:
    """Après la publication d'un brouillon, propose le seed dans qBittorrent.

    Reprend le source_path depuis le manifest local + détection Docker
    générique (même logique que le flow upload normal).
    """
    # Réutilise les helpers du flow game (fetch tracker torrent + détection Docker)
    from trackr import qbittorrent as qbt
    from trackr.config import save_config
    from trackr.flows.movie import (
        _get_qbt_default_save_path,
        _guess_container_path,
        _normalize_path,
    )

    if not cfg.is_qbt_ready():
        ui.console.print(
            f"\n[{ui.MUTED}][italic]qBittorrent n'est pas configuré — seed manuel à faire "
            f"si tu veux que ton torrent reste en seed.[/italic][/]"
        )
        return

    # Récupère le source_path depuis le manifest
    source_path_str = (e.manifest or {}).get("source_path") or ""
    if not source_path_str:
        ui.console.print(
            f"\n[{ui.WARN}]⚠ Source originale absente du manifest — seed non proposé. "
            f"Ajoute le torrent manuellement à qBittorrent si besoin.[/]"
        )
        return
    source_path = Path(source_path_str)

    # Fetch le .torrent re-signé par le tracker (binding correct passkey + source)
    tracker_torrent_path = e.torrent_path
    if e.torrent_path and res.info_hash and cfg.c411_session_valid():
        out_path = e.torrent_path.with_suffix(".from_tracker.torrent")
        try:
            with ui.console.status(
                "[cyan]Récupération du .torrent signé par C411…[/cyan]", spinner="dots"
            ):
                c411_api.download_torrent(cfg.c411_session, res.info_hash, out_path)
            tracker_torrent_path = out_path
        except (AuthError, TrackerError) as ex:
            ui.console.print(
                f"[{ui.WARN}]⚠ .torrent signé non récupéré : {ex}[/]\n"
                f"[{ui.MUTED}][italic]Fallback sur la version locale pour le seed.[/italic][/]"
            )

    ui.console.print()
    ui.console.print(
        f"[bold {ui.ACCENT}]Seeder dans qBittorrent ?[/]\n"
        f"[{ui.MUTED}][italic]Le brouillon est maintenant publié — on ajoute le .torrent "
        f"à qBit en pointant vers le fichier/dossier sur disque, qBit fait un recheck "
        f"rapide puis seed, aucun téléchargement.[/italic][/]"
    )
    if not questionary.confirm("Ajouter le torrent à qBittorrent maintenant ?", default=True).ask():
        return

    # Détection Docker générique (cf. flow upload normal)
    source_parent = str(source_path.parent)
    qbt_default_save = _get_qbt_default_save_path(cfg)
    docker_suspected = (
        qbt_default_save
        and source_parent.split("/")[1:2] != qbt_default_save.split("/")[1:2]
    )
    if docker_suspected:
        suggested = _guess_container_path(source_parent, qbt_default_save, cfg)
        ui.console.print(
            f"\n[{ui.WARN}]⚠ qBittorrent semble tourner en Docker (ou autre namespace de chemin).[/]\n"
            f"[{ui.MUTED}]  Source (côté host)        : {source_parent}/{source_path.name}[/]\n"
            f"[{ui.MUTED}]  save_path par défaut qBit : {qbt_default_save}[/]\n"
            + (f"[{ui.MUTED}]  Path container suggéré    : {suggested}[/]\n" if suggested else "")
            + f"[{ui.MUTED}][italic]  Indique le chemin **tel que qBit le voit** dans le conteneur.[/italic][/]"
        )
        default_save = suggested or qbt_default_save
    else:
        default_save = source_parent

    raw = questionary.path("Dossier de seed :", default=default_save).ask()
    if not raw:
        ui.console.print(f"[{ui.MUTED}]Seed annulé.[/]")
        return
    save_path = raw.strip().rstrip("/") or raw.strip()

    if not docker_suspected:
        local_path = _normalize_path(save_path)
        if not local_path.exists() or not local_path.is_dir():
            ui.console.print(ui.error_panel(
                "Dossier introuvable",
                f"{local_path}\n[italic]qBit refusera si le dossier n'existe pas.[/italic]",
            ))
            return
        save_path = str(local_path)

    tags = ["trackr-C411"]
    try:
        with ui.console.status("[cyan]Ajout dans qBittorrent…[/cyan]", spinner="dots"):
            if cfg.qbt_auth_mode == "api_key":
                qbt.add_torrent(
                    cfg.qbt_url, tracker_torrent_path, save_path,
                    api_key=cfg.qbt_api_key, tags=tags,
                )
            elif cfg.qbt_auth_mode == "login":
                sid = cfg.qbt_sid_cookie
                try:
                    if not sid:
                        sid = qbt.login(cfg.qbt_url, cfg.qbt_username, cfg.qbt_password)
                        cfg.qbt_sid_cookie = sid
                        save_config(cfg)
                    qbt.add_torrent(cfg.qbt_url, tracker_torrent_path, save_path, sid=sid, tags=tags)
                except qbt.QbtAuthError:
                    sid = qbt.login(cfg.qbt_url, cfg.qbt_username, cfg.qbt_password)
                    cfg.qbt_sid_cookie = sid
                    save_config(cfg)
                    qbt.add_torrent(cfg.qbt_url, tracker_torrent_path, save_path, sid=sid, tags=tags)
            else:
                ui.console.print(ui.error_panel("qBittorrent", "mode d'auth invalide"))
                return
    except qbt.QbtAuthError as ex:
        ui.console.print(ui.error_panel("qBittorrent — auth refusée", str(ex)))
        return
    except qbt.QbtError as ex:
        ui.console.print(ui.error_panel("qBittorrent — ajout échoué", str(ex)))
        return

    ui.console.print(ui.success_panel(
        "✓ Torrent ajouté dans qBittorrent",
        f"save_path : {save_path}\n"
        f"info hash : {res.info_hash}\n"
        f"[italic]Recheck en cours côté qBit ; le seed démarre dès qu'il a validé les pièces.[/italic]",
    ))


def _do_delete(e: DraftEntry, cfg: Config) -> bool:
    if not questionary.confirm(
        f"Supprimer le brouillon #{e.draft.id} ({e.draft.title}) ?\n"
        f"[dim](les fichiers locaux dans {e.cache_dir or '?'} ne seront PAS touchés)[/dim]",
        default=False,
    ).ask():
        return False
    try:
        with ui.console.status("[cyan]Suppression du brouillon…[/cyan]", spinner="dots"):
            msg = c411_api.delete_draft(cfg.c411_api_key, e.draft.id)
    except AuthError as ex:
        ui.console.print(ui.error_panel("Auth refusée", str(ex)))
        ui.press_enter()
        return False
    except TrackerError as ex:
        ui.console.print(ui.error_panel("Suppression échouée", str(ex)))
        ui.press_enter()
        return False

    ui.console.print(ui.success_panel("✓ Brouillon supprimé", msg))
    ui.press_enter()
    return True

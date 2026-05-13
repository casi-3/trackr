from __future__ import annotations

import time

import questionary
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from trackr import qbittorrent as qbt, ui
from trackr.config import Config, config_path, load_config, save_config
from trackr.trackers import c411 as c411_api
from trackr.trackers import torr9 as torr9_api
from trackr.trackers.base import AuthError, Profile, TrackerError


# ---------- Render ----------


def _render_status(cfg: Config) -> Panel:
    grid = Table.grid(padding=(0, 2), expand=True)
    grid.add_column(style="dim", width=24)
    grid.add_column()

    grid.add_row("Fichier de config", f"[{ui.MUTED}]{config_path()}[/]")
    grid.add_row("", "")

    # C411
    grid.add_row("[bold]C411[/]", ui.status_chip(cfg.is_c411_ready()))
    grid.add_row("   username", cfg.c411_username or f"[{ui.MUTED}]—[/]")
    grid.add_row("   API key", ui.mask_secret(cfg.c411_api_key))
    grid.add_row("   passkey", ui.mask_secret(cfg.c411_passkey))
    if cfg.c411_session:
        valid = cfg.c411_session_valid()
        chip = ui.status_chip(valid, "session 7j active", "session expirée")
        grid.add_row("   session web", chip)
    grid.add_row("", "")

    # Torr9
    grid.add_row("[bold]Torr9[/]", ui.status_chip(cfg.is_torr9_ready()))
    grid.add_row("   username", cfg.torr9_username or f"[{ui.MUTED}]—[/]")
    grid.add_row("   JWT", ui.mask_secret(cfg.torr9_jwt))
    if cfg.torr9_jwt:
        valid = cfg.torr9_jwt_valid()
        chip = ui.status_chip(valid, "valide", "expiré (re-login requis)")
        grid.add_row("   JWT statut", chip)
    grid.add_row("   passkey", ui.mask_secret(cfg.torr9_passkey))
    grid.add_row("", "")

    # qBittorrent
    grid.add_row("[bold]qBittorrent[/]", ui.status_chip(cfg.is_qbt_ready()))
    if cfg.qbt_url:
        grid.add_row("   URL", cfg.qbt_url)
    if cfg.qbt_auth_mode == "api_key":
        grid.add_row("   mode", "API Key")
        grid.add_row("   key", ui.mask_secret(cfg.qbt_api_key))
    elif cfg.qbt_auth_mode == "login":
        grid.add_row("   mode", "Login user/pass")
        grid.add_row("   username", cfg.qbt_username or f"[{ui.MUTED}]—[/]")
    grid.add_row("", "")

    grid.add_row("Host screens", cfg.default_screen_host)

    return Panel(grid, title=f"[bold {ui.ACCENT}]Configuration[/]", border_style=ui.ACCENT)


def _render_profile(tracker: str, profile: Profile, extras: dict[str, str]) -> Panel:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim", width=18)
    grid.add_column()

    grid.add_row("Tracker", f"[bold {ui.ACCENT}]{tracker}[/]")
    if profile.username:
        grid.add_row("Username", profile.username)
    if profile.email:
        grid.add_row("Email", profile.email)
    if profile.user_id:
        grid.add_row("User ID", str(profile.user_id))
    if profile.role:
        grid.add_row("Rôle", profile.role)
    if profile.ratio is not None:
        ratio_str = "∞" if profile.ratio == float("inf") else f"{profile.ratio:.2f}"
        color = ui.SUCCESS if (profile.ratio is None or profile.ratio == float("inf") or profile.ratio >= 1.0) else ui.WARN
        grid.add_row("Ratio", f"[{color}]{ratio_str}[/]")
    if profile.uploaded_bytes:
        grid.add_row("Uploaded", _human_size(profile.uploaded_bytes))
    if profile.downloaded_bytes:
        grid.add_row("Downloaded", _human_size(profile.downloaded_bytes))
    if profile.bonus:
        grid.add_row("Bonus", _human_size(profile.bonus))
    if profile.passkey:
        grid.add_row("Passkey", ui.mask_secret(profile.passkey, show=4))
    for k, v in extras.items():
        grid.add_row(k, v)

    return Panel(
        grid,
        title=f"[bold {ui.SUCCESS}]✓ Authentifié[/]",
        border_style=ui.SUCCESS,
        padding=(1, 2),
    )


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


# ---------- C411 ----------


def _configure_c411(cfg: Config) -> None:
    ui.clear()
    ui.console.print(ui.info_panel("Configuration · C411", "Choisis ton mode de connexion."))

    mode = questionary.select(
        "Comment veux-tu te connecter à C411 ?",
        choices=[
            questionary.Choice("⚡ Rapide — coller API key + passkey", value="quick"),
            questionary.Choice("🔐 Guidé — login complet (username + password + TOTP si actif)", value="guided"),
            questionary.Choice("← Retour", value="back"),
        ],
    ).ask()
    if mode in (None, "back"):
        return

    if mode == "quick":
        _c411_quick(cfg)
    else:
        _c411_guided(cfg)


def _c411_quick(cfg: Config) -> None:
    api_key = questionary.password(
        "API key (Bearer) — visible dans Profil → Clés API sur c411.org :"
    ).ask()
    if not api_key:
        return
    passkey = questionary.password(
        "Passkey (announce URL) — visible dans Profil → Tracker sur c411.org :"
    ).ask()
    if not passkey:
        return

    try:
        with ui.console.status("[cyan]Validation de l'API key auprès de C411…[/cyan]", spinner="dots"):
            c411_api.validate_api_key(api_key)
    except AuthError as e:
        ui.console.print(ui.error_panel("API key refusée", str(e)))
        ui.press_enter()
        return
    except TrackerError as e:
        ui.console.print(ui.error_panel("Erreur réseau", str(e)))
        ui.press_enter()
        return

    cfg.c411_api_key = api_key.strip()
    cfg.c411_passkey = passkey.strip()
    save_config(cfg)
    ui.console.print(
        ui.success_panel(
            "C411 configuré (mode rapide)",
            "API key validée. Passkey enregistrée.\n"
            f"[{ui.MUTED}]Note : ce mode ne récupère pas le profil. Bascule en mode guidé "
            f"si tu veux voir ton ratio, ton rôle, etc.[/]",
        )
    )
    ui.press_enter()


def _c411_guided(cfg: Config) -> None:
    username = questionary.text(
        "Username ou email C411 :",
        default=cfg.c411_username,
    ).ask()
    if not username:
        return
    password = questionary.password("Mot de passe :").ask()
    if not password:
        return

    try:
        with ui.console.status("[cyan]Connexion à C411…[/cyan]", spinner="dots"):
            pending, mfa_required = c411_api.login_step1(username, password)
    except AuthError as e:
        ui.console.print(ui.error_panel("Login refusé", str(e)))
        ui.press_enter()
        return
    except TrackerError as e:
        ui.console.print(ui.error_panel("Erreur réseau", str(e)))
        ui.press_enter()
        return

    if mfa_required:
        ui.console.print(
            ui.warn_panel(
                "Vérification en deux étapes (TOTP)",
                "Ouvre ton app authenticator et saisis le code à 6 chiffres.",
            )
        )
        success = False
        for attempt in range(3):
            code = questionary.text(f"Code TOTP (essai {attempt + 1}/3) :").ask()
            if not code:
                return
            try:
                with ui.console.status("[cyan]Validation du code…[/cyan]", spinner="dots"):
                    pending = c411_api.submit_totp(pending, code.strip())
                success = True
                break
            except AuthError as e:
                ui.console.print(f"[{ui.ERROR}]✗ {e}[/]")
            except TrackerError as e:
                ui.console.print(ui.error_panel("Erreur réseau", str(e)))
                ui.press_enter()
                return
        if not success:
            ui.console.print(ui.error_panel("Trop de tentatives", "Recommence quand tu veux."))
            ui.press_enter()
            return

    # Finalisation : profile + passkey + provisioning auto de la clé API
    try:
        with ui.console.status("[cyan]Récupération du profil, passkey et clé API…[/cyan]", spinner="dots"):
            result = c411_api.finalize(pending, provision_api_key=True)
    except AuthError as e:
        ui.console.print(ui.error_panel("Erreur post-login", str(e)))
        ui.press_enter()
        return
    except TrackerError as e:
        ui.console.print(ui.error_panel("Erreur réseau", str(e)))
        ui.press_enter()
        return

    cfg.c411_username = result.profile.username or username
    cfg.c411_passkey = result.profile.passkey
    cfg.c411_session = result.session_cookie
    cfg.c411_session_expires_at = result.session_expires_at.isoformat() if result.session_expires_at else ""
    if result.api_key:
        cfg.c411_api_key = result.api_key
    save_config(cfg)

    extras = {}
    if result.session_expires_at:
        extras["Session expire"] = result.session_expires_at.strftime("%d/%m/%Y %H:%M UTC")
    if result.api_key_provisioned:
        extras["API key"] = "[green]créée automatiquement (label : trackr-cli)[/green]"
    ui.console.print(_render_profile("C411", result.profile, extras))

    if not result.api_key_provisioned:
        ui.console.print(
            ui.warn_panel(
                "Création auto de la clé API impossible",
                "La session est OK, mais la création/rotation de la clé API a échoué "
                "(limite 5 clés ou autre erreur). Va sur c411.org → Profil → Intégrations API, "
                "crée une clé manuellement et reviens en mode Rapide pour la coller.",
            )
        )
    ui.press_enter()


# ---------- Torr9 ----------


def _configure_torr9(cfg: Config) -> None:
    ui.clear()
    ui.console.print(ui.info_panel("Configuration · Torr9", "Choisis ton mode de connexion."))

    mode = questionary.select(
        "Comment veux-tu te connecter à Torr9 ?",
        choices=[
            questionary.Choice("🔐 Login — username + password (JWT auto, passkey auto)", value="login"),
            questionary.Choice("⚡ JWT direct — coller un JWT existant + passkey", value="jwt"),
            questionary.Choice("← Retour", value="back"),
        ],
    ).ask()
    if mode in (None, "back"):
        return

    if mode == "jwt":
        _torr9_jwt_paste(cfg)
    else:
        _torr9_login(cfg)


def _torr9_login(cfg: Config) -> None:
    username = questionary.text(
        "Username Torr9 :",
        default=cfg.torr9_username,
    ).ask()
    if not username:
        return
    password = questionary.password("Mot de passe Torr9 :").ask()
    if not password:
        return
    store_pwd = questionary.confirm(
        "Mémoriser le mot de passe pour ré-authentifier auto à l'expiration du JWT (24h) ?",
        default=True,
    ).ask()

    try:
        with ui.console.status("[cyan]Connexion à Torr9…[/cyan]", spinner="dots"):
            result = torr9_api.login(username, password)
    except AuthError as e:
        ui.console.print(ui.error_panel("Login refusé", str(e)))
        ui.press_enter()
        return
    except TrackerError as e:
        ui.console.print(ui.error_panel("Erreur réseau", str(e)))
        ui.press_enter()
        return

    cfg.torr9_username = username
    cfg.torr9_password = password if store_pwd else ""
    cfg.torr9_jwt = result.token
    cfg.torr9_jwt_expires_at = result.token_expires_at.isoformat() if result.token_expires_at else ""
    cfg.torr9_passkey = result.profile.passkey
    save_config(cfg)

    extras = {}
    if result.token_expires_at:
        extras["JWT expire"] = result.token_expires_at.strftime("%d/%m/%Y %H:%M UTC")
    ui.console.print(_render_profile("Torr9", result.profile, extras))
    ui.press_enter()


def _torr9_jwt_paste(cfg: Config) -> None:
    jwt = questionary.password("JWT Torr9 (paste) :").ask()
    if not jwt:
        return

    try:
        with ui.console.status("[cyan]Validation du JWT et fetch du profil…[/cyan]", spinner="dots"):
            profile = torr9_api.fetch_profile(jwt.strip())
    except AuthError as e:
        ui.console.print(ui.error_panel("JWT refusé", str(e)))
        ui.press_enter()
        return
    except TrackerError as e:
        ui.console.print(ui.error_panel("Erreur réseau", str(e)))
        ui.press_enter()
        return

    cfg.torr9_username = profile.username or cfg.torr9_username
    cfg.torr9_jwt = jwt.strip()
    exp = torr9_api._decode_jwt_exp(jwt.strip())
    cfg.torr9_jwt_expires_at = exp.isoformat() if exp else ""
    cfg.torr9_passkey = profile.passkey or cfg.torr9_passkey
    save_config(cfg)

    extras = {}
    if exp:
        extras["JWT expire"] = exp.strftime("%d/%m/%Y %H:%M UTC")
    ui.console.print(_render_profile("Torr9", profile, extras))
    ui.press_enter()


# ---------- qBittorrent ----------


def _configure_qbittorrent(cfg: Config) -> None:
    ui.clear()
    ui.console.print(
        ui.info_panel(
            "Configuration · qBittorrent",
            "URL du WebUI + choix de la méthode d'authentification.",
        )
    )

    default_url = cfg.qbt_url or "http://localhost:8080"
    url = questionary.text("URL du WebUI qBittorrent :", default=default_url).ask()
    if not url:
        return
    url = url.strip().rstrip("/")

    # Probe tolérant : 403 = auth requise (cas tunnel/remote sans bypass), pas une erreur
    api_v = ""
    requires_auth = False
    try:
        with ui.console.status("[cyan]Test de la joignabilité…[/cyan]", spinner="dots"):
            api_v, requires_auth = qbt.probe(url)
    except qbt.QbtError as e:
        ui.console.print(ui.error_panel("Impossible de joindre qBittorrent", str(e)))
        ui.press_enter()
        return

    if requires_auth:
        ui.console.print(
            f"[{ui.MUTED}]Joignable. WebUI exige une authentification "
            f"(pas de bypass localhost depuis cette URL — normal pour un tunnel/remote).[/]"
        )
        # On ne connaît pas encore la version : on propose les deux modes,
        # le whoami post-auth nous dira si API Key est supportée.
        supports_api_key = True
    else:
        supports_api_key = qbt._version_tuple(api_v) >= (2, 14, 1)
        ui.console.print(
            f"[{ui.MUTED}]WebAPI v{api_v} détecté"
            + (
                f" · [bold {ui.SUCCESS}]API Key disponible[/]"
                if supports_api_key
                else f" · [{ui.WARN}]API Key non supportée (qBittorrent < 5.2.0)[/]"
            )
            + "[/]"
        )

    # Choix mode
    mode_choices = []
    if supports_api_key:
        mode_choices.append(
            questionary.Choice("🔑 API Key (recommandé, qBittorrent ≥ 5.2.0)", value="api_key")
        )
    mode_choices.append(questionary.Choice("👤 Login — username + password (cookie SID)", value="login"))
    mode_choices.append(questionary.Choice("← Retour", value="back"))

    mode = questionary.select("Mode d'authentification :", choices=mode_choices).ask()
    if mode in (None, "back"):
        return

    if mode == "api_key":
        _qbt_api_key_mode(cfg, url)
    else:
        _qbt_login_mode(cfg, url)


def _qbt_api_key_mode(cfg: Config, url: str) -> None:
    ui.console.print(
        Panel(
            Text.from_markup(
                "Génère une clé API dans qBittorrent :\n"
                "  [bold]Préférences → WebUI → API Key → Generate[/]\n\n"
                "La clé commence par [bold]qbt_[/] et fait 32 caractères.\n"
                f"[{ui.MUTED}]Elle ne peut pas être utilisée pour les endpoints /auth/login ni /logout.[/]"
            ),
            border_style=ui.ACCENT,
            title=f"[bold {ui.ACCENT}]Génération de la clé API[/]",
        )
    )
    api_key = questionary.password("Colle ta clé API qBittorrent :").ask()
    if not api_key:
        return
    api_key = api_key.strip()
    if not api_key.startswith("qbt_"):
        ui.console.print(
            ui.warn_panel(
                "Format inattendu",
                f"La clé doit commencer par 'qbt_'. Reçue : {api_key[:8]}…\nJe continue quand même au cas où.",
            )
        )

    try:
        with ui.console.status("[cyan]Validation de la clé API…[/cyan]", spinner="dots"):
            ident = qbt.whoami(url, api_key=api_key)
    except qbt.QbtAuthError as e:
        ui.console.print(ui.error_panel("Clé API refusée", str(e)))
        ui.press_enter()
        return
    except qbt.QbtError as e:
        ui.console.print(ui.error_panel("Erreur qBittorrent", str(e)))
        ui.press_enter()
        return

    cfg.qbt_url = url
    cfg.qbt_auth_mode = "api_key"
    cfg.qbt_api_key = api_key
    # On vide les credentials login pour éviter les confusions
    cfg.qbt_username = ""
    cfg.qbt_password = ""
    cfg.qbt_sid_cookie = ""
    save_config(cfg)
    _qbt_success(url, ident)


def _qbt_login_mode(cfg: Config, url: str) -> None:
    username = questionary.text(
        "Username qBittorrent :",
        default=cfg.qbt_username,
    ).ask()
    if not username:
        return
    password = questionary.password("Mot de passe :").ask()
    if not password:
        return
    store_pwd = questionary.confirm(
        "Mémoriser le mot de passe pour re-login auto à l'expiration du cookie SID ?",
        default=True,
    ).ask()

    try:
        with ui.console.status("[cyan]Login qBittorrent…[/cyan]", spinner="dots"):
            sid = qbt.login(url, username, password)
            ident = qbt.whoami(url, sid=sid)
    except qbt.QbtAuthError as e:
        ui.console.print(ui.error_panel("Login refusé", str(e)))
        ui.press_enter()
        return
    except qbt.QbtError as e:
        ui.console.print(ui.error_panel("Erreur qBittorrent", str(e)))
        ui.press_enter()
        return

    cfg.qbt_url = url
    cfg.qbt_auth_mode = "login"
    cfg.qbt_username = username
    cfg.qbt_password = password if store_pwd else ""
    cfg.qbt_sid_cookie = sid
    cfg.qbt_api_key = ""
    save_config(cfg)
    _qbt_success(url, ident)


def _qbt_success(url: str, ident: qbt.QbtIdentity) -> None:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim", width=18)
    grid.add_column()
    grid.add_row("URL", url)
    grid.add_row("App version", ident.app_version)
    grid.add_row("WebAPI version", ident.webapi_version)
    mode_label = "API Key (Bearer)" if ident.auth_mode == "api_key" else "Login (cookie SID)"
    grid.add_row("Mode auth", mode_label)
    grid.add_row("API Key supportée", "oui" if ident.supports_api_key else "non (qBit < 5.2.0)")
    ui.console.print(
        Panel(grid, title=f"[bold {ui.SUCCESS}]✓ qBittorrent connecté[/]", border_style=ui.SUCCESS, padding=(1, 2))
    )
    ui.press_enter()


# ---------- Screens / divers ----------


def _configure_screens(cfg: Config) -> None:
    ui.clear()
    ui.console.print(ui.info_panel("Configuration · Hébergement screenshots", ""))
    choice = questionary.select(
        "Host par défaut pour les captures :",
        choices=[
            questionary.Choice("Demander à chaque upload", value="ask"),
            questionary.Choice("Catbox", value="catbox"),
            questionary.Choice("Pixhost (films)", value="pixhost"),
            questionary.Choice("imgbb (films)", value="imgbb"),
        ],
        default=cfg.default_screen_host,
    ).ask()
    if choice:
        cfg.default_screen_host = choice
        save_config(cfg)
        ui.console.print(f"[{ui.SUCCESS}]✓ Host par défaut : {choice}[/]")
        time.sleep(0.5)


# ---------- Entrypoint ----------


def run() -> None:
    while True:
        ui.clear()
        cfg = load_config()
        ui.console.print(_render_status(cfg))

        action = questionary.select(
            "Que veux-tu configurer ?",
            choices=[
                questionary.Choice("Identifiants C411", value="c411"),
                questionary.Choice("Identifiants Torr9", value="torr9"),
                questionary.Choice("Client BitTorrent (qBittorrent)", value="qbt"),
                questionary.Choice("Host screenshots", value="screens"),
                questionary.Choice("← Retour au menu principal", value="back"),
            ],
        ).ask()

        if action in (None, "back"):
            return
        if action == "c411":
            _configure_c411(cfg)
        elif action == "torr9":
            _configure_torr9(cfg)
        elif action == "qbt":
            _configure_qbittorrent(cfg)
        elif action == "screens":
            _configure_screens(cfg)

from __future__ import annotations

from datetime import datetime, timezone

from rich.align import Align
from rich.columns import Columns
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.style import Style
from rich.table import Table
from rich.text import Text

from trackr import __version__

console = Console()

ACCENT = "#7dd3fc"
ACCENT_DIM = "#0ea5e9"
SUCCESS = "#22c55e"
WARN = "#facc15"
ERROR = "#ef4444"
MUTED = "#94a3b8"


LOGO_LINES = [
    "  ████████ ██████   █████   ██████ ██   ██ ██████ ",
    "     ██    ██   ██ ██   ██ ██      ██  ██  ██   ██",
    "     ██    ██████  ███████ ██      █████   ██████ ",
    "     ██    ██   ██ ██   ██ ██      ██  ██  ██   ██",
    "     ██    ██   ██ ██   ██  ██████ ██   ██ ██   ██",
]


def clear() -> None:
    import sys

    # \x1b[3J vide le scrollback, \x1b[2J vide l'écran visible, \x1b[H ramène le curseur en haut.
    # Rich.console.clear() ne fait que 2J+H — d'où l'empilement dans le scrollback.
    sys.stdout.write("\x1b[3J\x1b[2J\x1b[H")
    sys.stdout.flush()


def banner() -> Panel:
    logo = Text()
    n = len(LOGO_LINES)
    for i, line in enumerate(LOGO_LINES):
        # gradient from accent → accent_dim
        color = ACCENT if i < n / 2 else ACCENT_DIM
        logo.append(line + "\n", style=Style(color=color, bold=True))
    tagline = Text("publier partout. plus vite. plus propre.\n", style="italic dim")
    version = Text(f"v{__version__}", style=f"bold {MUTED}")
    body = Group(Align.center(logo), Align.center(tagline), Align.center(version))
    return Panel(body, border_style=ACCENT, padding=(1, 4))


def rule(text: str = "", style: str = ACCENT) -> RenderableType:
    from rich.rule import Rule

    return Rule(text, style=style)


def success_panel(title: str, body: RenderableType | str) -> Panel:
    content = Text(body) if isinstance(body, str) else body
    return Panel(content, title=f"[bold {SUCCESS}]✓ {title}[/]", border_style=SUCCESS)


def error_panel(title: str, body: RenderableType | str) -> Panel:
    content = Text(body) if isinstance(body, str) else body
    return Panel(content, title=f"[bold {ERROR}]✗ {title}[/]", border_style=ERROR)


def warn_panel(title: str, body: RenderableType | str) -> Panel:
    content = Text(body) if isinstance(body, str) else body
    return Panel(content, title=f"[bold {WARN}]! {title}[/]", border_style=WARN)


def info_panel(title: str, body: RenderableType | str) -> Panel:
    content = Text(body) if isinstance(body, str) else body
    return Panel(content, title=f"[bold {ACCENT}]{title}[/]", border_style=ACCENT)


def mask_secret(value: str, *, show: int = 4) -> str:
    if not value:
        return "—"
    if len(value) <= show * 2:
        return "•" * len(value)
    return f"{value[:show]}{'•' * (len(value) - show * 2)}{value[-show:]}"


def status_chip(ok: bool, ok_label: str = "prêt", ko_label: str = "non configuré") -> str:
    if ok:
        return f"[bold {SUCCESS}]●[/] [{SUCCESS}]{ok_label}[/]"
    return f"[bold {MUTED}]○[/] [{MUTED}]{ko_label}[/]"


def press_enter(prompt: str = "Appuie sur Entrée pour continuer") -> None:
    console.input(f"\n[dim]{prompt}…[/dim] ")


# ───────────────────────────── dashboard ─────────────────────────────


def _fmt_bytes(n: int) -> str:
    if n <= 0:
        return "—"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    val = float(n)
    i = 0
    while val >= 1024 and i < len(units) - 1:
        val /= 1024
        i += 1
    return f"{val:.1f} {units[i]}"


def _fmt_ratio(r: float | None) -> str:
    if r is None:
        return "—"
    if r == float("inf"):
        return "∞"
    return f"{r:.2f}"


def _fmt_int(n: int) -> str:
    if not n:
        return "—"
    return f"{n:,}".replace(",", " ")


def _fmt_age(iso: str, fallback: str = "") -> str:
    """Convertit un timestamp ISO en âge relatif court ('2j', '3h', '12m')."""
    if not iso:
        return fallback or "—"
    try:
        if iso.endswith("Z"):
            dt = datetime.fromisoformat(iso[:-1]).replace(tzinfo=timezone.utc)
        else:
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return fallback or iso[:10]
    delta = datetime.now(timezone.utc) - dt
    total = int(delta.total_seconds())
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m"
    if total < 86400:
        return f"{total // 3600}h"
    if total < 30 * 86400:
        return f"{total // 86400}j"
    return f"{total // (30 * 86400)}mo"


def _status_glyph(status: str) -> tuple[str, str]:
    s = status.lower()
    if s == "pending":
        return "⏳", WARN
    if s in ("active", "approved"):
        return "✓", SUCCESS
    if s in ("rejected", "failed"):
        return "✗", ERROR
    return "·", MUTED


def _stat_chip(snap, with_ratio: bool = True) -> Text:
    """Chip compact : `C411  12.31` ou `C411 ⚠` ou `C411 —` (non configuré)."""
    t = Text()
    t.append(snap.name, style=f"bold {ACCENT}")
    t.append("  ")
    if not snap.configured:
        t.append("—", style=MUTED)
        return t
    if snap.error and not snap.uploads:
        t.append("⚠", style=WARN)
        return t
    if with_ratio and snap.ratio is not None:
        t.append(_fmt_ratio(snap.ratio), style="white")
        pending = len(snap.pending)
        if pending:
            t.append(f"  ({pending} ⏳)", style=WARN)
    else:
        t.append("●", style=SUCCESS)
    return t


def _qbt_chip(snap) -> Text:
    t = Text()
    t.append("qBit", style=f"bold {ACCENT}")
    t.append("  ")
    if not snap.configured:
        t.append("—", style=MUTED)
    elif snap.error:
        t.append("⚠ à rafraîchir", style=WARN)
    else:
        t.append("●", style=SUCCESS)
        if snap.torrents_count:
            t.append(f"  {snap.torrents_count} seeds", style=MUTED)
    return t


def render_dashboard(dash) -> RenderableType:
    """Flat list : ligne de stats + tableau des derniers uploads tous trackers."""
    from trackr import upload_queue
    from trackr.dashboard import merge_recent

    # Ligne de stats (séparée par ·)
    chips: list[Text] = []
    for snap in (dash.c411, dash.torr9):
        if snap.configured or snap.error:
            chips.append(_stat_chip(snap))
    chips.append(_qbt_chip(dash.qbt))
    # Badge queue : N upload(s) en attente de reprise
    pending = upload_queue.pending_count()
    if pending:
        chip = Text()
        chip.append("queue", style=f"bold {ACCENT}")
        chip.append(f"  ⏳ {pending}", style=WARN)
        chips.append(chip)
    # Badge rejets C411 : N torrents en attente de correction
    n_rej = len(dash.c411.rejections)
    if n_rej:
        chip = Text()
        chip.append("rejets", style=f"bold {ACCENT}")
        chip.append(f"  🚨 {n_rej}", style=ERROR)
        chips.append(chip)
    stat_line = Text()
    for i, c in enumerate(chips):
        if i:
            stat_line.append("   ·   ", style=MUTED)
        stat_line.append_text(c)

    # Tableau plat des derniers uploads, tous trackers confondus
    recent = merge_recent(dash, limit=8)
    parts: list[RenderableType] = [Align.center(stat_line)]
    if recent:
        table = Table.grid(padding=(0, 2))
        table.add_column(width=2)                          # icône
        table.add_column(width=6, style=ACCENT, no_wrap=True)  # tracker
        table.add_column(no_wrap=True)                     # titre
        table.add_column(style=MUTED, justify="right", no_wrap=True)  # age

        for tracker, u in recent:
            glyph, color = _status_glyph(u.get("status", ""))
            title = (u.get("title") or "?")
            if len(title) > 60:
                title = title[:59] + "…"
            age = _fmt_age(u.get("created_at", ""), fallback=u.get("age", ""))
            table.add_row(
                Text(glyph, style=color),
                tracker,
                title,
                age,
            )

        header = Text("Derniers uploads", style=f"bold {ACCENT}")
        parts.append(Text(""))
        parts.append(Align.center(header))
        parts.append(Align.center(table))

    # Section rejets C411 (si présents)
    if dash.c411.rejections:
        rej_table = Table.grid(padding=(0, 2))
        rej_table.add_column(width=2)
        rej_table.add_column(no_wrap=True)  # titre
        rej_table.add_column(no_wrap=True)  # raison head
        rej_table.add_column(style=MUTED, justify="right", no_wrap=True)  # age
        for r in dash.c411.rejections[:5]:
            title = r.torrent_name or "?"
            if len(title) > 50:
                title = title[:49] + "…"
            # Première ligne utile de la raison (ignore le greeting)
            reason_lines = [l.strip() for l in (r.reason or "").split("\n") if l.strip()]
            reason_head = ""
            for l in reason_lines:
                if l.startswith("1.") or "à corriger" in l.lower() or "raison" in l.lower():
                    reason_head = l
                    break
            if not reason_head and reason_lines:
                reason_head = reason_lines[0]
            if len(reason_head) > 60:
                reason_head = reason_head[:59] + "…"
            rej_table.add_row(
                Text("🚨", style=ERROR),
                Text(title, style=ERROR),
                Text(reason_head, style=MUTED),
                Text(_fmt_age(r.created_at), style=MUTED),
            )
        parts.append(Text(""))
        parts.append(Align.center(Text("Rejets C411 — à corriger et resoumettre", style=f"bold {ERROR}")))
        parts.append(Align.center(rej_table))

    # Erreurs visibles (si un tracker a échoué malgré config OK)
    errors = []
    for snap in (dash.c411, dash.torr9):
        if snap.configured and snap.error:
            errors.append(Text.from_markup(f"[{WARN}]⚠ {snap.name} :[/] [{MUTED}]{snap.error}[/]"))
    if dash.qbt.configured and dash.qbt.error:
        errors.append(Text.from_markup(f"[{WARN}]⚠ qBit :[/] [{MUTED}]{dash.qbt.error}[/]"))
    if errors:
        parts.append(Text(""))
        for e in errors:
            parts.append(Align.center(e))

    return Group(*parts)

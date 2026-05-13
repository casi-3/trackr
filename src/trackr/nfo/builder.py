"""Génération de titres, NFO et descriptions selon les règles de chaque tracker.

Règles C411 : nommage strict (`Nom.Année.Lang.Res.Source.Audio.Vidéo-TEAM`, sans
accents), DETAG forbidden (le tag team du filename doit matcher le titre).
Règles Torr9 : plus relax, on réutilise le titre style C411 par défaut.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from trackr.media import mediainfo as mediainfo_mod
from trackr.media.lookup import MediaHit
from trackr.media.mediainfo import MediaInfo, resolution_label


# ─────────────────────────── helpers ───────────────────────────


def _bitrate_human(n: int) -> str:
    if n <= 0:
        return "?"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f} Mb/s"
    if n >= 1_000:
        return f"{n / 1_000:.0f} kb/s"
    return f"{n} b/s"


def _size_human(n: int) -> str:
    """Taille en unités décimales (GB, MB) — cohérent avec qBittorrent, OS et trackers."""
    if n <= 0:
        return "?"
    units = ["B", "kB", "MB", "GB", "TB"]
    size = float(n)
    for unit in units:
        if size < 1000 or unit == units[-1]:
            return f"{size:.2f} {unit}"
        size /= 1000
    return f"{size:.2f} {units[-1]}"


def _duration_human(seconds: float) -> str:
    if seconds <= 0:
        return "?"
    total = int(seconds // 1000) if seconds > 1e6 else int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    return f"{m}m {s:02d}s"


def _strip_accents(s: str) -> str:
    nfd = unicodedata.normalize("NFD", s)
    return "".join(c for c in nfd if unicodedata.category(c) != "Mn")


_DOT_KEEP = re.compile(r"[^a-zA-Z0-9]+")


def to_dot_case(s: str) -> str:
    """`Le Comte de Monte-Cristo` → `Le.Comte.de.Monte.Cristo`.

    Sans accent, séparateur point, casse d'origine préservée (les exemples C411
    gardent les articles 'de', 'la', etc. en minuscules quand TMDB les renvoie ainsi).
    """
    if not s:
        return ""
    s = _strip_accents(s)
    parts = [p for p in _DOT_KEEP.split(s) if p]
    return ".".join(parts)


# ─────────────────────── codec / language tags ───────────────────────


def video_codec_tag(codec: str, *, scene_style: bool = True) -> str:
    """HEVC → x265 / H265 selon `scene_style` ; AVC → x264 / H264 ; etc.

    `scene_style=True` ⇒ re-encode (x265/x264) ; `scene_style=False` ⇒ stream
    direct depuis la source (H265/H264). Pour C411 la règle est : si le NFO
    contient « Encoding settings » → re-encode → x26x ; sinon → H26x.
    """
    c = (codec or "").lower()
    if "hevc" in c or "h.265" in c or "h265" in c:
        return "x265" if scene_style else "H265"
    if "avc" in c or "h.264" in c or "h264" in c:
        return "x264" if scene_style else "H264"
    if "av1" in c:
        return "AV1"
    if "vp9" in c:
        return "VP9"
    return codec or ""


_ENCODING_SETTINGS_RX = re.compile(r"^\s*Encoding settings\s*:", re.IGNORECASE | re.MULTILINE)


def has_encoding_settings(nfo_text: str) -> bool:
    """Vrai si le NFO mediainfo contient une ligne « Encoding settings » (re-encode)."""
    return bool(_ENCODING_SETTINGS_RX.search(nfo_text or ""))


def audio_codec_tag(codec: str) -> str:
    c = (codec or "").upper().replace(" ", "").replace("-", "")
    if c == "EAC3":
        return "EAC3"
    if c == "AC3":
        return "AC3"
    if c == "AAC":
        return "AAC"
    if c == "DTS":
        return "DTS"
    if c == "TRUEHD":
        return "TrueHD"
    if c == "FLAC":
        return "FLAC"
    if c == "OPUS":
        return "OPUS"
    if c == "MP3":
        return "MP3"
    return codec or ""


def channels_tag(channels: str | int) -> str:
    if not channels:
        return ""
    try:
        n = int(str(channels).split()[0])
    except (ValueError, IndexError):
        return str(channels)
    mapping = {1: "1.0", 2: "2.0", 6: "5.1", 8: "7.1"}
    return mapping.get(n, str(n))


_LANG_RX = re.compile(
    r"\b(MULTi|MULTI)\.(VFF|VOF|VFQ|VFI|VF2|VOSTFR)\b|"
    r"\b(VFF|VOF|VFQ|VFI|VF2|VOSTFR|TRUEFRENCH|FRENCH|VOQ|VO)\b|"
    r"\b(MULTi|MULTI)\b",
    re.IGNORECASE,
)


def detect_language_tag(file_path: Path, info: MediaInfo) -> str:
    """Devine le tag de langue scene-style depuis le nom de fichier puis les pistes audio."""
    m = _LANG_RX.search(file_path.name)
    if m:
        raw = m.group(0).upper().replace("MULTI", "MULTi")
        return raw
    fr_audio = [a for a in info.audio if (a.language or "").lower().startswith("fr")]
    non_fr = [a for a in info.audio if not (a.language or "").lower().startswith("fr")]
    fr_subs = [s for s in info.subtitles if (s.language or "").lower().startswith("fr")]
    if fr_audio and non_fr:
        return "MULTi.VFF"
    if fr_audio:
        return "VFF"
    if fr_subs:
        return "VOSTFR"
    return "VO"


_SOURCE_RX = re.compile(
    r"\b(BluRay|BDRip|BRRip|WEB-?DL|WEB-?Rip|WEB|HDTV|DVDRip|DVD)\b",
    re.IGNORECASE,
)


# Tag d'équipe : dernière séquence `-XXX` du stem du fichier (ex `-FW`, `-V4L`).
_TEAM_RX = re.compile(r"-([A-Za-z0-9]+)$")


def detect_team_tag(file_path: Path) -> str:
    """Extrait le tag d'équipe depuis le nom de fichier source.

    C411 sanctionne le DETAG par omission : utiliser `-NOTAG` dans le titre
    alors que le filename porte un tag identifiable (`-V4L`, `-FW`, etc.) est
    interdit. On détecte le tag d'origine pour éviter ce piège.
    """
    stem = file_path.stem
    m = _TEAM_RX.search(stem)
    if not m:
        return "NOTAG"
    tag = m.group(1)
    # On ignore les "tags" qui ressemblent à un identifiant technique (codec, résolution…)
    blacklist = {
        "x264", "x265", "h264", "h265", "AV1", "VP9",
        "AAC", "AC3", "DTS", "EAC3", "FLAC", "MP3",
        "1080p", "720p", "2160p", "576p", "480p",
        "REMUX", "BDMV", "BDRip", "WEB", "DL",
    }
    if tag in blacklist or tag.lower() in {t.lower() for t in blacklist}:
        return "NOTAG"
    return tag


def detect_source_tag(file_path: Path) -> str:
    m = _SOURCE_RX.search(file_path.name)
    if not m:
        return ""
    raw = m.group(1).upper().replace("-", "").replace("WEBDL", "WEB-DL").replace("WEBRIP", "WEBRip")
    if raw == "WEB":
        return "WEB"
    if raw == "BLURAY":
        return "BluRay"
    if raw == "BDRIP":
        return "BDRip"
    if raw == "DVD":
        return "DVD"
    if raw == "DVDRIP":
        return "DVDRip"
    if raw == "HDTV":
        return "HDTV"
    return raw


# ─────────────────────── titre par tracker ───────────────────────


def suggest_title_c411(
    hit: MediaHit,
    info: MediaInfo,
    *,
    source: str,
    language_tag: str,
    team: str = "NOTAG",
    is_reencode: bool = True,
) -> str:
    """Format C411 Films : `Nom.Année.Langue.Résolution.Source.CodecAudio[.Channels].CodecVidéo-TEAM`.

    `is_reencode=False` ⇒ codec en H265/H264 (release directe depuis la source).
    """
    name = to_dot_case(hit.title)
    year = hit.year or ""
    res = resolution_label(info)  # "1080p" / "2160p" / etc.
    first_audio = info.audio[0] if info.audio else None
    acodec = audio_codec_tag(first_audio.codec) if first_audio else ""
    chans = channels_tag(first_audio.channels) if first_audio else ""
    vcodec = video_codec_tag(info.video.codec, scene_style=is_reencode)

    parts = [name]
    if year:
        parts.append(year)
    if language_tag:
        parts.append(language_tag)
    if res and res != "?":
        parts.append(res)
    if source:
        parts.append(source.replace(" ", ""))
    if acodec:
        if chans:
            parts.append(f"{acodec}.{chans}")
        else:
            parts.append(acodec)
    if vcodec:
        parts.append(vcodec)
    body = ".".join(parts)
    team_clean = (team or "NOTAG").strip().lstrip("-")
    return f"{body}-{team_clean}"


def suggest_title_torr9(
    hit: MediaHit,
    info: MediaInfo,
    *,
    source: str = "WEB",
    is_reencode: bool = True,
) -> str:
    """Torr9 accepte un format plus libre — on garde un nom lisible humain."""
    res = resolution_label(info)
    vcodec = video_codec_tag(info.video.codec, scene_style=is_reencode)
    first_audio = info.audio[0] if info.audio else None
    acodec = audio_codec_tag(first_audio.codec) if first_audio else ""
    chans = channels_tag(first_audio.channels) if first_audio else ""
    audio_part = f"{acodec}{chans}" if acodec and chans else acodec or chans

    base = hit.title
    if hit.year:
        base = f"{base} ({hit.year})"
    spec = " ".join(p for p in [res, vcodec, audio_part, source] if p)
    return f"{base} [{spec}]" if spec else base


# ─────────────────────── NFO ───────────────────────


def build_nfo(file_path: Path) -> str:
    """NFO standard = sortie texte brute de mediainfo (conforme C411 et largement adoptée)."""
    return mediainfo_mod.raw_text(file_path)


# ─────────────────────── description BBCode ───────────────────────


_LANG_DISPLAY: dict[str, tuple[str, str]] = {
    # ISO 639-1 → (flag country code, display name FR)
    "en": ("us", "Anglais"),
    "fr": ("fr", "Français"),
    "es": ("es", "Espagnol"),
    "de": ("de", "Allemand"),
    "it": ("it", "Italien"),
    "ja": ("jp", "Japonais"),
    "ko": ("kr", "Coréen"),
    "ru": ("ru", "Russe"),
    "pt": ("pt", "Portugais"),
    "zh": ("cn", "Chinois"),
    "ar": ("sa", "Arabe"),
    "nl": ("nl", "Néerlandais"),
    "tr": ("tr", "Turc"),
    "pl": ("pl", "Polonais"),
    "sv": ("se", "Suédois"),
    "no": ("no", "Norvégien"),
    "da": ("dk", "Danois"),
    "fi": ("fi", "Finnois"),
    "hi": ("in", "Hindi"),
    "cs": ("cz", "Tchèque"),
    "hu": ("hu", "Hongrois"),
    "el": ("gr", "Grec"),
    "he": ("il", "Hébreu"),
    "th": ("th", "Thaï"),
    "id": ("id", "Indonésien"),
    "vi": ("vn", "Vietnamien"),
}


def _flag_and_name(language: str, hint_title: str = "") -> tuple[str, str]:
    """Renvoie `(country_code_pour_flagcdn, label)` avec heuristique VFF/VFQ/VO."""
    code = (language or "").lower()[:2]
    hint_upper = (hint_title or "").upper()
    if code == "fr":
        if any(t in hint_upper for t in ("VFQ", "QUEBEC", "QUÉB", "CANAD")):
            return ("ca", "VFQ")
        if "VFF" in hint_upper or "TRUEFRENCH" in hint_upper:
            return ("fr", "VFF")
        if "VOF" in hint_upper:
            return ("fr", "VOF")
        return ("fr", "Français")
    if code == "en":
        if "VO" in hint_upper or "ORIGINAL" in hint_upper:
            return ("us", "VO Anglais")
        return ("us", "Anglais")
    return _LANG_DISPLAY.get(code, ("xx", language or "Inconnu"))


def _flag(cc: str) -> str:
    return f"[img=20x15]https://flagcdn.com/20x15/{cc}.png[/img]"


def _section_header(title: str) -> str:
    """Séparateur centré stylé, en remplacement des bannières /images/banners."""
    bar = "━" * 12
    return f"[center][b]{bar}  {title}  {bar}[/b][/center]"


def _audio_table(tracks) -> str:
    """Tableau BBCode des pistes audio (#, Langue, Canaux, Codec, Bitrate)."""
    out = ["[table]"]
    out.append("[tr][th]#[/th][th]Langue[/th][th]Canaux[/th][th]Codec[/th][th]Bitrate[/th][/tr]")
    for i, a in enumerate(tracks, 1):
        cc, name = _flag_and_name(a.language, a.title)
        flag = _flag(cc)
        chans = channels_tag(a.channels) or "?"
        codec = a.codec or "?"
        if a.title and a.title.strip():
            codec = f"{codec} ({a.title.strip()})"
        bitrate = _bitrate_human(a.bitrate)
        out.append(
            f"[tr][td]{i}[/td][td]{flag} {name}[/td][td]{chans}[/td][td]{codec}[/td][td]{bitrate}[/td][/tr]"
        )
    out.append("[/table]")
    return "".join(out)


def _subs_table(subs) -> str:
    """Tableau BBCode des sous-titres (#, Langue, Format, Type)."""
    out = ["[table]"]
    out.append("[tr][th]#[/th][th]Langue[/th][th]Format[/th][th]Type[/th][/tr]")
    for i, s in enumerate(subs, 1):
        cc, name = _flag_and_name(s.language, s.title)
        flag = _flag(cc)
        fmt = s.codec or "?"
        title_upper = (s.title or "").upper()
        if s.forced:
            sub_type = "Forced"
        elif "SDH" in title_upper or "HEARING" in title_upper:
            sub_type = "SDH"
        elif "CC" in title_upper.split():
            sub_type = "CC"
        else:
            sub_type = "Full"
        out.append(
            f"[tr][td]{i}[/td][td]{flag} {name}[/td][td]{fmt}[/td][td]{sub_type}[/td][/tr]"
        )
    out.append("[/table]")
    return "".join(out)


def build_description_bbcode(
    hit: MediaHit,
    info: MediaInfo,
    *,
    release_title: str = "",
    source: str = "",
    vod_platform: str = "",
    team_tag: str = "",
    file_count: int = 1,
    total_size: int | None = None,
) -> str:
    """`total_size` (bytes) prime sur `info.file_size` pour le « Poids total » —
    indispensable pour matcher la taille calculée par le tracker (= payload du
    .torrent), surtout si mediainfo renvoie une valeur légèrement différente."""
    lines: list[str] = []

    # Poster
    if hit.poster_url:
        lines.append(f"[center][img]{hit.poster_url}[/img][/center]")
        lines.append("")

    # En-tête : titre / année / TMDB
    lines.append(f"[b]Titre :[/b] {hit.title}")
    if hit.year:
        lines.append(f"[b]Année :[/b] {hit.year}")
    if hit.rating:
        lines.append(f"[b]Note TMDB :[/b] {hit.rating}/10")
    if hit.tmdb_id:
        lines.append(f"[b]TMDB id :[/b] {hit.tmdb_id}")
    lines.append("")

    # Synopsis
    if hit.description:
        lines.append(_section_header("SYNOPSIS"))
        lines.append("")
        lines.append(hit.description.strip())
        lines.append("")

    # Détails techniques vidéo
    lines.append(_section_header("DÉTAILS TECHNIQUES"))
    lines.append("")
    source_line = source or "?"
    if vod_platform:
        source_line = f"{source_line} ({vod_platform})"
    lines.append(f"[b]Source :[/b] {source_line}")
    lines.append(f"[b]Résolution :[/b] {resolution_label(info)}")
    lines.append(f"[b]Codec Vidéo :[/b] {info.video.codec or '?'}"
                 + (f" {info.video.profile}" if info.video.profile else ""))
    if info.video.bitrate:
        lines.append(f"[b]Débit vidéo :[/b] {_bitrate_human(info.video.bitrate)}")
    if info.video.fps:
        lines.append(f"[b]FPS :[/b] {info.video.fps:.3f}")
    if info.video.bit_depth:
        lines.append(f"[b]Profondeur :[/b] {info.video.bit_depth}-bit")
    lines.append("")

    # Audio
    if info.audio:
        lines.append(_section_header("LANGUES"))
        lines.append("")
        lines.append(_audio_table(info.audio))
        lines.append("")

    # Sous-titres
    if info.subtitles:
        lines.append(_section_header("SOUS-TITRES"))
        lines.append("")
        lines.append(_subs_table(info.subtitles))
        lines.append("")

    # Récap téléchargement
    lines.append(_section_header("TÉLÉCHARGEMENT"))
    lines.append("")
    if release_title:
        lines.append(f"[b]Release :[/b] {release_title}")
    if team_tag and team_tag != "NOTAG":
        lines.append(f"[b]Team :[/b] {team_tag}")
    lines.append(f"[b]Nombre de fichier(s) :[/b] {file_count}")
    poids = total_size if total_size and total_size > 0 else info.file_size
    lines.append(f"[b]Poids total :[/b] {_size_human(poids)}")
    lines.append(f"[b]Durée :[/b] {_duration_human(info.duration_s)}")
    lines.append(f"[b]Conteneur :[/b] {info.container or '?'}")
    lines.append("")

    # Footer
    lines.append(
        "[center][size=1][i]upload via "
        "[url=https://github.com/casi-3/trackr]Trackr[/url]"
        "[/i][/size][/center]"
    )
    return "\n".join(lines)


_SLUG_RX = re.compile(r"[^a-z0-9]+")


def slugify(text: str, *, max_len: int = 60) -> str:
    s = _strip_accents(text or "").lower()
    s = _SLUG_RX.sub("-", s).strip("-")
    return s[:max_len] or "untitled"

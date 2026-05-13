from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


class MediainfoError(RuntimeError):
    pass


@dataclass
class VideoTrack:
    codec: str = ""
    profile: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    bitrate: int = 0
    bit_depth: int = 0
    scan_type: str = ""
    duration_s: float = 0.0


@dataclass
class AudioTrack:
    codec: str = ""
    channels: str = ""
    sampling_rate: int = 0
    bitrate: int = 0
    language: str = ""
    title: str = ""


@dataclass
class SubtitleTrack:
    codec: str = ""
    language: str = ""
    title: str = ""
    forced: bool = False


@dataclass
class MediaInfo:
    path: Path
    container: str = ""
    file_size: int = 0
    overall_bitrate: int = 0
    duration_s: float = 0.0
    video: VideoTrack = field(default_factory=VideoTrack)
    audio: list[AudioTrack] = field(default_factory=list)
    subtitles: list[SubtitleTrack] = field(default_factory=list)


def _to_int(value) -> int:
    if value is None:
        return 0
    try:
        return int(float(str(value).split()[0]))
    except (ValueError, IndexError):
        return 0


def _to_float(value) -> float:
    if value is None:
        return 0.0
    try:
        return float(str(value).split()[0])
    except (ValueError, IndexError):
        return 0.0


def _resolution_label(width: int, height: int) -> str:
    if height >= 2000:
        return "2160p"
    if height >= 1300:
        return "1440p"
    if height >= 1000:
        return "1080p"
    if height >= 700:
        return "720p"
    if height >= 540:
        return "576p"
    if height >= 460:
        return "480p"
    return f"{height}p" if height else "?"


def resolution_label(info: MediaInfo) -> str:
    return _resolution_label(info.video.width, info.video.height)


def raw_text(path: Path, *, sanitize_path: bool = True) -> str:
    """Retourne la sortie texte brute de `mediainfo /path/file` — c'est le NFO standard.

    Par défaut, `Complete name` est nettoyé pour ne contenir que le nom du
    fichier (pas le chemin absolu, qui peut révéler la structure du disque).
    """
    if shutil.which("mediainfo") is None:
        raise MediainfoError("mediainfo introuvable dans le PATH.")
    proc = subprocess.run(
        ["mediainfo", str(path)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise MediainfoError(f"mediainfo a échoué : {proc.stderr.strip()}")
    out = proc.stdout
    if sanitize_path:
        import re

        out = re.sub(
            r"(Complete name\s*:\s*).+",
            lambda m: m.group(1) + path.name,
            out,
            count=1,
        )
    return out.strip() + "\n"


def probe(path: Path) -> MediaInfo:
    if shutil.which("mediainfo") is None:
        raise MediainfoError(
            "mediainfo introuvable dans le PATH. "
            "Installer avec `apt install mediainfo` (Debian/Ubuntu) ou `brew install media-info` (macOS)."
        )
    if not path.exists():
        raise MediainfoError(f"Fichier introuvable : {path}")

    proc = subprocess.run(
        ["mediainfo", "--Output=JSON", "--Full", str(path)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise MediainfoError(f"mediainfo a échoué : {proc.stderr.strip()}")

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise MediainfoError(f"sortie mediainfo invalide : {e}") from e

    tracks = data.get("media", {}).get("track", [])
    info = MediaInfo(path=path)

    for tr in tracks:
        kind = tr.get("@type", "")
        if kind == "General":
            info.container = tr.get("Format", "")
            info.file_size = _to_int(tr.get("FileSize"))
            info.overall_bitrate = _to_int(tr.get("OverallBitRate"))
            info.duration_s = _to_float(tr.get("Duration"))
        elif kind == "Video" and not info.video.codec:
            info.video = VideoTrack(
                codec=tr.get("Format", ""),
                profile=tr.get("Format_Profile", ""),
                width=_to_int(tr.get("Width")),
                height=_to_int(tr.get("Height")),
                fps=_to_float(tr.get("FrameRate")),
                bitrate=_to_int(tr.get("BitRate")),
                bit_depth=_to_int(tr.get("BitDepth")),
                scan_type=tr.get("ScanType", ""),
                duration_s=_to_float(tr.get("Duration")),
            )
        elif kind == "Audio":
            info.audio.append(
                AudioTrack(
                    codec=tr.get("Format", ""),
                    channels=tr.get("Channels", ""),
                    sampling_rate=_to_int(tr.get("SamplingRate")),
                    bitrate=_to_int(tr.get("BitRate")),
                    language=tr.get("Language", ""),
                    title=tr.get("Title", ""),
                )
            )
        elif kind == "Text":
            forced_raw = str(tr.get("Forced", "")).lower()
            info.subtitles.append(
                SubtitleTrack(
                    codec=tr.get("Format", ""),
                    language=tr.get("Language", ""),
                    title=tr.get("Title", ""),
                    forced=forced_raw in {"yes", "true", "1"},
                )
            )

    return info

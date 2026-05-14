"""Catalog statique des catégories C411 (Films & Vidéos, Jeux Vidéo).

Les sous-catégories sont stables et durcies ici pour éviter un appel
`GET /api/categories` à chaque flow. Les options dynamiques par sous-cat
(Langue, Genre, Type…) restent récupérées via
`GET /api/categories/{id}/options` au moment de l'upload.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Subcat:
    id: int
    name: str
    slug: str


@dataclass(frozen=True)
class Cat:
    id: int
    name: str
    slug: str
    subs: tuple[Subcat, ...]


FILMS_VIDEOS = Cat(
    id=1,
    name="Films & Vidéos",
    slug="films-videos",
    subs=(
        Subcat(1, "Animation", "animation"),
        Subcat(2, "Animation Série", "animation-serie"),
        Subcat(3, "Concert", "concert"),
        Subcat(4, "Documentaire", "documentaire"),
        Subcat(5, "Emission TV", "emission-tv"),
        Subcat(6, "Film", "film"),
        Subcat(7, "Série TV", "serie-tv"),
        Subcat(8, "Spectacle", "spectacle"),
        Subcat(9, "Sport", "sport"),
        Subcat(10, "Vidéo-clips", "video-clips"),
        Subcat(54, "Collection", "collection"),
        Subcat(57, "Série Documentaire", "serie-documentaire"),
    ),
)


def movies_category() -> Cat:
    return FILMS_VIDEOS


# ─────────────────────── Jeux Vidéo ───────────────────────


JEUX_VIDEO = Cat(
    id=5,
    name="Jeux Vidéo",
    slug="jeux-video",
    subs=(
        Subcat(28, "Autre", "autre"),
        Subcat(29, "Linux", "linux"),
        Subcat(30, "MacOS", "macos"),
        Subcat(31, "Microsoft", "microsoft"),
        Subcat(32, "Nintendo", "nintendo"),
        Subcat(33, "Smartphone", "smartphone"),
        Subcat(34, "Sony", "sony"),
        Subcat(35, "Tablette", "tablette"),
        Subcat(36, "Windows", "windows"),
        Subcat(55, "VR", "vr"),
    ),
)


def games_category() -> Cat:
    return JEUX_VIDEO


# ─────────────────────── Consoles Microsoft (cat=5, sub=31) ───────────────────────


@dataclass(frozen=True)
class Console:
    key: str            # identifiant interne (ex: "xbox360")
    label: str          # affichage menu (ex: "Xbox 360")
    title_tag: str      # tag de titre (ex: "[XBOX360]")
    containers: tuple[str, ...]  # conteneurs valides (premier = défaut)


MICROSOFT_CONSOLES: tuple[Console, ...] = (
    Console("xbox",    "Xbox (originale)",    "[XBOX]",    ("ISO", "JTAG", "RGH")),
    Console("xbox360", "Xbox 360",            "[XBOX360]", ("GOD", "ISO", "JTAG", "RGH")),
    Console("xone",    "Xbox One",            "[XONE]",    ("XVC",)),
    Console("xsx",     "Xbox Series X|S",     "[XSX]",     ("XVC",)),
)


# Régions et langues officielles (wiki C411 cat JV / Microsoft)
GAME_REGIONS: tuple[tuple[str, str], ...] = (
    ("PAL",    "PAL — Europe"),
    ("EU",     "EU — Europe (variante)"),
    ("NTSC",   "NTSC — Amérique du Nord"),
    ("US",     "US — Amérique du Nord (variante)"),
    ("NTSC-J", "NTSC-J — Japon"),
    ("JP",     "JP — Japon (variante)"),
    ("HK",     "HK — Hong Kong"),
)

GAME_LANGUAGES: tuple[tuple[str, str], ...] = (
    ("FR",    "FR — Français"),
    ("EN",    "EN — Anglais"),
    ("JP",    "JP — Japonais"),
    ("MULTI", "MULTI — Multilingue"),
)

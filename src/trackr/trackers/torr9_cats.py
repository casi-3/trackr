"""Catalog hardcodé des catégories Torr9.

L'API ne sert pas la liste — elle est figée dans le bundle web.

⚠️ L'endpoint POST /api/v1/torrents/upload attend les **noms affichés** (avec
accents et espaces), pas les slugs. On garde donc le `name` exact.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Subcat:
    id: int
    slug: str
    name: str


@dataclass(frozen=True)
class Cat:
    id: int
    slug: str
    name: str
    kind: str  # "movie", "tv", "music", "book", "game", "other"
    subs: tuple[Subcat, ...]


CATEGORIES: tuple[Cat, ...] = (
    Cat(1, "film", "Films", "movie", (
        Subcat(2, "movieanim", "Films d'animation"),
        Subcat(3, "docs", "Documentaires"),
        Subcat(51, "movie", "Films"),
        Subcat(52, "concert", "Concert"),
        Subcat(53, "spectacle", "Spectacle"),
        Subcat(54, "sport", "Sport"),
        Subcat(55, "videoclips", "Vidéo-clips"),
    )),
    Cat(4, "tv", "Séries", "tv", (
        Subcat(5, "series-tv", "Séries TV"),
        Subcat(6, "broadcast", "Émission TV"),
        Subcat(7, "animes", "Séries Animées"),
        Subcat(68, "manga-anime", "Mangas-Animes"),
    )),
    Cat(8, "audio", "Audio", "music", (
        Subcat(9, "music", "Musique"),
        Subcat(10, "podcast", "Podcasts"),
        Subcat(12, "karaoke", "Karaoké"),
    )),
    Cat(13, "book", "Livres", "book", (
        Subcat(14, "ebooks", "Livres papiers & eBooks"),
        Subcat(16, "bd", "BD"),
        Subcat(17, "comics", "Comics"),
        Subcat(18, "manga", "Mangas"),
        Subcat(31, "formation", "Formation"),
        Subcat(65, "audiobook", "Livres Audios"),
        Subcat(67, "magazine", "Magazine"),
    )),
    Cat(19, "game", "Jeux-vidéos", "game", (
        Subcat(20, "g-other", "Autres"),
        Subcat(21, "g-linux", "Linux"),
        Subcat(22, "g-macos", "MacOS"),
        Subcat(23, "g-microsoft", "Microsoft"),
        Subcat(24, "g-nintendo", "Nintendo"),
        Subcat(26, "g-sony", "Sony"),
        Subcat(28, "g-windows", "Windows"),
    )),
    Cat(29, "app", "Applications", "other", (
        Subcat(30, "other", "Autres"),
        Subcat(32, "linux", "Linux"),
        Subcat(33, "macos", "MacOS"),
        Subcat(36, "windows", "Windows"),
    )),
    Cat(37, "nulled", "NULLED", "other", (
        Subcat(38, "other", "Divers"),
        Subcat(39, "mobile", "Mobile"),
        Subcat(40, "script", "Scripts"),
        Subcat(41, "wordpress", "Wordpress"),
    )),
    Cat(42, "emulation", "Emulation", "other", (
        Subcat(43, "emulator", "Emulateur"),
        Subcat(44, "rom", "ROM/ISO"),
    )),
    Cat(69, "impression-3d", "Impression 3D", "other", (
        Subcat(70, "personnages-3d", "Personnages"),
        Subcat(71, "objets-3d", "Objets 3D"),
        Subcat(72, "pack-3d", "Pack"),
    )),
    Cat(73, "vo", "VO", "other", (
        Subcat(74, "films-vo", "Films"),
        Subcat(75, "series-vo", "Séries"),
        Subcat(76, "livres-vo", "Livres"),
    )),
    Cat(45, "xxx", "XXX", "other", (
        Subcat(46, "xxx-film", "Films/Vidéos +18"),
        Subcat(47, "xxx-image", "Images +18"),
        Subcat(48, "xxx-game", "Jeux +18"),
        Subcat(49, "xxx-book", "Ebooks +18"),
        Subcat(50, "hentai", "Hentai +18"),
        Subcat(66, "xxx-fansite", "FanSite +18"),
    )),
)


def find_cat(slug_or_id: str | int) -> Cat | None:
    for c in CATEGORIES:
        if c.slug == slug_or_id or c.id == slug_or_id:
            return c
    return None


def find_subcat(cat: Cat, slug_or_id: str | int) -> Subcat | None:
    for s in cat.subs:
        if s.slug == slug_or_id or s.id == slug_or_id:
            return s
    return None


def movies_category() -> Cat:
    c = find_cat("film")
    if not c:
        raise RuntimeError("Catégorie Films introuvable")
    return c


def series_category() -> Cat:
    c = find_cat("tv")
    if not c:
        raise RuntimeError("Catégorie Séries introuvable")
    return c

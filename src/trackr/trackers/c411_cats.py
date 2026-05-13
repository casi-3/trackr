"""Catalog statique des catégories C411 (Films & Vidéos).

Les sous-catégories de "Films & Vidéos" (parent id=1) sont stables et
durcies ici pour éviter un appel `GET /api/categories` à chaque flow. Les
options dynamiques par sous-cat (Langue, Genre, Type…) restent récupérées
via `GET /api/categories/{id}/options` au moment de l'upload.
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

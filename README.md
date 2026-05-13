# trackr

CLI guidé pour publier des torrents sur plusieurs trackers privés (C411,
Torr9) en un seul flow. Sélection des trackers, drag&drop du fichier,
l'outil guide jusqu'au POST.

> ⚠️ **Beta** — version de test. Retours bienvenus via les
> [issues](../../issues).

---

## Confidentialité

Aucune donnée n'est collectée, aucune donnée n'est partagée.

- Pas de télémétrie, pas d'analytics, pas de phone-home.
- Aucun serveur appartenant à trackr — l'outil n'a pas de backend.
- Les communications réseau vont uniquement vers les trackers configurés,
  l'instance qBittorrent renseignée, et TMDB via les proxies des trackers
  (aucune clé API TMDB à fournir).
- Identifiants stockés en local via
  [`platformdirs`](https://pypi.org/project/platformdirs/) :
  - Linux : `~/.config/trackr/config.toml`
  - macOS : `~/Library/Application Support/trackr/config.toml`
  - Windows : `%APPDATA%\trackr\config.toml`
- Permissions `600` sur Unix.

---

## Fonctionnalités

- Publication multi-tracker en un flow, avec confirmation explicite avant
  POST.
- Auto-détection depuis le filename et mediainfo : codec, résolution,
  source (WEB / BluRay / ...), langue (VFF / VOSTFR / MULTi / ...), tag
  d'équipe.
- Génération du titre, NFO et description BBCode conformes aux règles de
  chaque tracker. Tableaux audio et sous-titres avec drapeaux par langue.
- Recherche TMDB intégrée par titre ou ID, via les proxies des trackers.
- Création du `.torrent` avec progress bar et ETA.
- Seed automatique dans qBittorrent (mode API key ou login, détection du
  mapping Docker).
- File de reprise pour les uploads partiellement échoués — on retente
  uniquement les trackers en erreur, sans régénérer le `.torrent`.
- Mode batch pour enchaîner plusieurs uploads.
- Dashboard : ratio par tracker, uploads en attente de modération,
  derniers torrents publiés.

---

## Prérequis

- Python ≥ 3.10
- `mediainfo` (CLI) :
  - Debian / Ubuntu : `sudo apt install mediainfo`
  - macOS : `brew install media-info`
  - Windows : [binaire officiel](https://mediaarea.net/MediaInfo)
- qBittorrent ≥ 4.0 (optionnel, uniquement pour le seed automatique).
  L'auth par API Key nécessite qBittorrent ≥ 5.2.0 ; sinon mode
  user/password.

---

## Installation

```bash
git clone https://github.com/casi-3/trackr.git
cd trackr
pip install -e .
trackr
```

Compatible Linux, macOS, Windows.

---

## Premier lancement

1. `trackr` ouvre le menu interactif.
2. **Configuration** → renseigner les credentials pour chaque tracker à
   utiliser (mode rapide avec API key + passkey, ou mode guidé avec
   user/password et TOTP si activé).
3. **Configuration → Client BitTorrent** → URL qBittorrent et méthode
   d'authentification.
4. **Uploader un torrent** depuis le menu principal.

---

## Trackers supportés

| Tracker | Upload | Download `.torrent` signé | Catégories |
|---------|--------|--------------------------|------------|
| C411    | ✓      | ✓ (via session web)      | Films & Vidéos |
| Torr9   | ✓      | ✓ (via JWT)              | Films |

D'autres trackers et catégories pourront être ajoutés.

---

## Emplacement des fichiers

| Données | Linux |
|---|---|
| Configuration | `~/.config/trackr/config.toml` |
| `.torrent` / NFO / description générés | `~/.cache/trackr/builds/` |
| File de reprise | `~/.cache/trackr/queue/` |

Chemins adaptés sur macOS et Windows via `platformdirs`. Aucun fichier
n'est écrit dans le dossier du projet.

---

## Reporting de bugs

[Ouvrir une issue](../../issues/new) avec :

- L'étape concernée (par ex. « POST C411 », « seed dans qBit »…).
- Le message d'erreur affiché.
- OS, version Python (`python --version`), version trackr
  (`trackr --version`).

---

## Stack

Python 3.10+, [Typer](https://typer.tiangolo.com/),
[Rich](https://rich.readthedocs.io/),
[questionary](https://questionary.readthedocs.io/),
[httpx](https://www.python-httpx.org/),
[torf](https://github.com/rndusr/torf),
[platformdirs](https://pypi.org/project/platformdirs/).

---

## Licence

[MIT](LICENSE).

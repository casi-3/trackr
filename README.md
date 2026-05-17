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
  l'instance qBittorrent renseignée, les bases TMDB et RAWG.io via les
  proxies des trackers (aucune clé API à fournir), GitHub (vérification et
  téléchargement des mises à jour), et — seulement si tu l'actives pour
  les captures d'écran — l'hébergeur d'images Catbox.
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
- Upload de jeux vidéo (consoles Microsoft : Xbox, 360, One, Series X|S).
  Recherche RAWG.io pour la présentation, les genres, l'éditeur /
  développeur et le titre commercial exact. Nom et conteneur pré-remplis
  depuis le fichier source.
- Captures d'écran récupérées automatiquement depuis RAWG (nombre au
  choix) : liens directs ou réupload sur Catbox.
- Création du `.torrent` avec progress bar et ETA.
- Seed automatique dans qBittorrent (mode API key ou login, détection du
  mapping Docker).
- Brouillons C411 : si le quota d'upload est atteint, repli automatique en
  brouillon, puis publication ou suppression depuis le menu.
- File de reprise pour les uploads partiellement échoués — on retente
  uniquement les trackers en erreur, sans régénérer le `.torrent`.
- Mode batch pour enchaîner plusieurs uploads.
- Dashboard : ratio par tracker, uploads en attente de modération,
  derniers torrents publiés.
- Mise à jour intégrée : vérification au démarrage, changelog de la
  nouvelle version affiché, installation en un choix — le binaire se
  remplace en place et relance, y compris sous Windows.

---

## Installation

Deux options : binaire pré-compilé (autonome, `mediainfo` inclus) ou
install depuis les sources.

### Binaire pré-compilé (recommandé)

Télécharge le binaire pour ton OS depuis la
[page des releases](../../releases/latest) :

| OS | Binaire |
|---|---|
| Windows | `trackr-windows-x64.exe` |
| Linux | `trackr-linux-x64` |
| macOS | `trackr-macos-arm64` |

Pas de dépendance à installer — `mediainfo` est embarqué.

Sous Linux / macOS : `chmod +x trackr-*` puis exécute.

### Depuis les sources

Prérequis : Python ≥ 3.10 et `mediainfo` (CLI) installé séparément
(`apt install mediainfo`, `brew install media-info`, ou
[binaire officiel](https://mediaarea.net/MediaInfo) sous Windows).

```bash
git clone https://github.com/casi-3/trackr.git
cd trackr
pip install -e .
trackr
```

### Optionnel

qBittorrent ≥ 4.0 pour le seed automatique après upload.
L'auth par API Key nécessite qBittorrent ≥ 5.2.0 ; sinon mode
user/password.

---

## Premier lancement

1. `trackr` ouvre le menu interactif.
2. **Configuration** → renseigner les credentials pour chaque tracker à
   utiliser (mode rapide avec API key + passkey, ou mode guidé avec
   user/password et TOTP si activé).
3. **Configuration → Client BitTorrent** → URL qBittorrent et méthode
   d'authentification.
4. **Uploader un torrent** ou **Uploader un jeu** depuis le menu
   principal.

---

## Mise à jour

À chaque lancement, trackr vérifie s'il existe une version plus récente
sur GitHub. Le cas échéant, il affiche le changelog complet de cette
version et propose de l'installer :

- **Binaire** (Windows / Linux / macOS) : téléchargement, remplacement du
  binaire en place et relance automatique — rien à réinstaller.
- **Sources (git)** : `git pull --ff-only` puis relance.
- **pip** : la commande de mise à jour à lancer est affichée.

---

## Trackers supportés

| Tracker | Upload | Download `.torrent` signé | Catégories |
|---------|--------|--------------------------|------------|
| C411    | ✓      | ✓ (via session web)      | Films, Vidéos, Jeux vidéo (Microsoft) |
| Torr9   | ✓      | ✓ (via JWT)              | Films, Jeux vidéo (Microsoft) |

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

# Hydracker API App

Application portable GUI/CLI pour utiliser l'API Hydracker sans accès MySQL.

Elle utilise uniquement l'API Hydracker, pas MySQL.

- `GET /api/v1/content/liens/{id}`
- `GET /api/v1/titles/{titleId}/content/liens`
- `GET /api/v1/titles/{titleId}/content/nzbs`
- `GET /api/v1/search/{query}`
- `POST /api/v1/nzb`

## Configuration

Le token vient de `https://hydracker.com/account-settings`.

```bash
export HYDRACKER_API_TOKEN="..."
export HYDRACKER_API_BASE_URL="https://hydracker.com/api/v1"
export HYDRACKER_USER_AGENT="HydrackerApiApp/0.1.0 (contact@hydracker.com)"

export ONEFICHIER_API_TOKEN="..."
```

Le `User-Agent` descriptif est obligatoire d'après `swagger.yaml`.

Pour poster en Usenet/NZB avec Nyuu:

```bash
export HYDRACKER_POSTER="nyuu"
export NYUU_BIN="npx --yes nyuu"
export HYDRA_USENET_HOST="news.example.com"
export HYDRA_USENET_PORT="563"
export HYDRA_USENET_USER="..."
export HYDRA_USENET_PASSWORD="..."
export HYDRA_USENET_GROUPS="alt.binaries.multimedia"
export HYDRA_USENET_FROM="Hydracker <poster@hydracker.com>"
```

## CLI

```bash
cd /home/hemiad/sites/hydra/hydracker-api-app

python3 hydracker_api_app.py env
python3 hydracker_api_app.py get-lien 12345
python3 hydracker_api_app.py get-lien 12345,12346,12347
python3 hydracker_api_app.py search "matrix"
python3 hydracker_api_app.py title-links "https://hydracker.com/title/42"

python3 hydracker_api_app.py create-nzb \
  --title-id 42 \
  --qualite 52 \
  --langues TrueFrench \
  --lien-id 12345 \
  --nzb /path/release.nzb
```

Télécharger un lien 1fichier via l'API 1fichier:

```bash
python3 hydracker_api_app.py download-1f "https://1fichier.com/?abc123" --out downloads
```

Workflow titre Hydracker complet:

```bash
python3 hydracker_api_app.py sync-title "https://hydracker.com/title/42" \
  --poster nyuu \
  --download-dir downloads \
  --nzb-dir nzbs \
  --qualite 52 \
  --langues TrueFrench \
  --dry-run
```

Enlevez `--dry-run` pour télécharger via 1fichier, poster avec Nyuu puis appeler `POST /nzb` avec `lien_id`.

Workflow global par catégorie:

```bash
python3 hydracker_api_app.py sync-category \
  --genre action \
  --type movie \
  --pages 2 \
  --poster nyuu \
  --download-dir downloads \
  --nzb-dir nzbs \
  --dry-run
```

## GUI

Sur un poste avec environnement graphique:

```bash
python3 hydracker_api_app.py
```

Sur SSH ou serveur sans `DISPLAY`, l'app reste en CLI.

La GUI est séparée en trois onglets:

- `Workflow`: choix radio entre `Search`, `Categorie`, `Title ID/URL` et `Lien ID`, puis lancement du workflow.
- `Options`: tokens, dossiers, Nyuu/Usenet, thème `System`, `Dark` ou `Light`.
- `Logs`: journal debug des requêtes API, téléchargements, étapes Nyuu et progression.

Dans l'onglet `Workflow`, l'app n'affiche que le champ utile pour le mode choisi.
`Search` lance automatiquement `/search/{query}` après quelques caractères, et
`Title ID/URL` extrait l'ID depuis une URL Hydracker puis lance `/titles/{id}`.
Pour les séries, la liste des saisons apparaît et relance le détail du title
avec `seasonNumber` quand une saison est sélectionnée.
`Categorie` charge les IDs depuis `/channel`, puis affiche les titles via
`/channel/{id}?restriction=&order=last_content_added_at:desc&filters=&page=...&paginate=lengthAware&returnContentOnly=true`
avec boutons `Prev` et `Next`.
Les résultats sont affichés sous forme de cartes avec poster, titre, genres,
date de sortie, note, type et ID; le JSON brut reste dans les logs debug.

Le bouton `Download + NZB + Upload` lance le pipeline complet depuis la GUI:

- en mode `Title ID/URL`, il traite le title sélectionné;
- en mode `Categorie`, il traite les titles de la page channel affichée.

Le pipeline respecte `Dry run` dans les options: laissez-le coché pour tester,
enlevez-le pour télécharger via 1fichier, poster avec Nyuu et uploader le NZB.

Les options sont sauvegardées automatiquement dans:

```text
~/.config/hydracker-api-app/settings.json
```

Le fichier est créé avec permissions `600` quand le système le permet.

## Windows

```powershell
setx HYDRACKER_API_TOKEN "..."
py hydracker_api_app.py gui
py hydracker_api_app.py get-lien 12345
```

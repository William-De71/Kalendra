# Kalendra

Serveur **CalDAV** autonome, à lancer avec Docker, qui stocke tout dans une
base **SQLite** unique. Inspiré de Baïkal, mais sans PHP, sans nginx et sans
la moindre dépendance : Kalendra n'utilise que la bibliothèque standard de
Python.

- **CalDAV en lecture/écriture** pour GNOME Evolution (Fedora), Thunderbird,
  DAVx⁵ et iOS/macOS.
- **Flux ICS en lecture seule** pour Google Calendar et Proton Calendar, qui
  ne savent pas parler CalDAV à un serveur tiers (voir plus bas).
- **Une seule image, un seul fichier de données.** Sauvegarder son agenda,
  c'est copier `kalendra.db`.

---

## À lire avant de commencer : ce que Google et Proton savent vraiment faire

C'est le point qui coûte le plus de temps quand on auto-héberge un agenda, et
il n'a rien à voir avec la qualité du serveur.

| Client | Protocole utilisable | Sens de la synchro |
| --- | --- | --- |
| GNOME Evolution (Fedora) | CalDAV | lecture / écriture |
| Thunderbird | CalDAV | lecture / écriture |
| DAVx⁵ (Android) | CalDAV | lecture / écriture |
| iOS / macOS Calendrier | CalDAV | lecture / écriture |
| **Google Calendar** | abonnement ICS par URL | **lecture seule** |
| **Proton Calendar** | abonnement ICS par URL | **lecture seule** |

L'API CalDAV de Google sert à lire *les agendas hébergés par Google* depuis un
client tiers ; elle ne permet pas à Google Calendar de se connecter à votre
serveur. Google Calendar n'offre que « Ajouter un agenda → À partir de l'URL »,
c'est-à-dire un abonnement iCalendar en lecture seule, rafraîchi quand Google
le décide (souvent plusieurs heures, parfois jusqu'à 24 h — l'en-tête
`REFRESH-INTERVAL` publié par Kalendra n'est qu'une indication).

Proton Calendar, lui, chiffre les agendas côté client : ouvrir un accès CalDAV
supposerait de sortir les clés du navigateur. Proton propose donc lui aussi
uniquement l'abonnement à un calendrier externe par URL, en lecture seule.

**Conséquence pratique :** Kalendra expose les deux surfaces. Vous écrivez
depuis Evolution, Thunderbird ou votre téléphone en CalDAV ; Google et Proton
s'abonnent au flux `/feed/<jeton>.ics` pour voir les mêmes événements. Si vous
voulez pouvoir *créer* un événement depuis l'interface web de Google ou de
Proton et le retrouver dans Kalendra, aucun serveur CalDAV ne le permettra :
il faudrait passer par l'API Google Calendar et un jeton OAuth.

---

## Démarrage rapide

```sh
git clone https://github.com/OWNER/kalendra.git
cd kalendra
cp .env.example .env       # renseignez KALENDRA_ADMIN_PASSWORD
docker compose up -d
```

Puis ouvrez <http://localhost:5232/admin> avec le compte administrateur : la
page affiche l'URL CalDAV de chaque agenda et l'URL de son flux ICS.

Sans docker-compose :

```sh
docker run -d --name kalendra \
  -p 127.0.0.1:5232:5232 \
  -v kalendra-data:/data \
  -e KALENDRA_ADMIN_USER=will \
  -e KALENDRA_ADMIN_PASSWORD='…' \
  -e KALENDRA_PUBLIC_URL=https://cal.example.org \
  ghcr.io/OWNER/kalendra:1
```

Sans Docker du tout (Python ≥ 3.11, rien à installer) :

```sh
PYTHONPATH=src python -m kalendra --db ./kalendra.db user add will --admin --with-calendar perso
PYTHONPATH=src KALENDRA_DB=./kalendra.db python -m kalendra serve
```

---

## Raccorder les clients

### GNOME Evolution (Fedora)

`Nouveau → Calendrier → Type : CalDAV`, puis :

- **URL** : `https://cal.example.org/` (la découverte `/.well-known/caldav`,
  `current-user-principal` et `calendar-home-set` fait le reste)
- **Utilisateur** : votre identifiant Kalendra
- Cochez « Trouver les calendriers » : Evolution liste vos agendas.

Evolution utilise `sync-collection` (RFC 6578) : après la première
synchronisation, il ne récupère que les objets modifiés.

### Thunderbird

`Nouvel agenda → Sur le réseau → CalDAV`, URL
`https://cal.example.org/calendars/<utilisateur>/<agenda>/`.

### DAVx⁵ (Android)

`Ajouter un compte → Connexion avec une URL et un mot de passe`, URL de base
`https://cal.example.org/`. DAVx⁵ expose ensuite les agendas au calendrier
Android natif.

### iOS / macOS

`Réglages → Applications → Calendrier → Comptes → Ajouter un compte → Autre →
Compte CalDAV`. Serveur : `cal.example.org`. **TLS est obligatoire** côté
Apple : passez par un reverse proxy HTTPS.

### Google Calendar (lecture seule)

1. Interface d'administration → copiez l'URL `…/feed/<jeton>.ics` de l'agenda.
2. Google Calendar → *Autres agendas* → **+** → *À partir de l'URL* → collez
   l'URL → *Ajouter un agenda*.

L'URL doit être joignable depuis Internet (Google va la chercher lui-même) et
en HTTPS. Le rafraîchissement est piloté par Google, comptez plusieurs heures.

### Proton Calendar (lecture seule)

`Calendriers → Ajouter un calendrier → S'abonner à un calendrier`, puis collez
la même URL `…/feed/<jeton>.ics`. Proton rafraîchit environ toutes les heures.

> Le jeton du flux **est** le secret : quiconque possède l'URL lit l'agenda.
> Un bouton « Régénérer » dans l'administration (ou `kalendra calendar token`)
> invalide immédiatement l'ancienne URL. Coupez le flux d'un agenda que vous
> ne partagez pas.

---

## Mise en production

Kalendra parle HTTP en clair et s'appuie sur l'authentification HTTP Basic :
**mettez-le derrière un reverse proxy TLS**. Le serveur HTTP intégré est celui
de la bibliothèque standard, en mode multi-thread : parfait pour un usage
personnel ou familial derrière un proxy, pas pour exposer directement le
service à Internet sans filtrage.

Caddy (le plus court chemin, certificat automatique) :

```caddyfile
cal.example.org {
    reverse_proxy 127.0.0.1:5232
}
```

nginx :

```nginx
server {
    listen 443 ssl http2;
    server_name cal.example.org;

    # Indispensable : nginx doit laisser passer les verbes WebDAV.
    location / {
        proxy_pass http://127.0.0.1:5232;
        proxy_set_header Host              $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Host  $host;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_pass_request_headers on;
        client_max_body_size 10m;
    }
}
```

Pour servir Kalendra dans un sous-chemin (`https://exemple.org/cal/`),
positionnez `KALENDRA_BASE_PATH=/cal` : toutes les URLs générées dans les
réponses XML en tiendront compte.

### Sauvegarde

```sh
docker exec kalendra python -c \
  "import sqlite3; src=sqlite3.connect('/data/kalendra.db'); \
   dst=sqlite3.connect('/data/backup.db'); src.backup(dst); dst.close()"
```

`sqlite3.backup()` produit une copie cohérente même pendant une écriture, ce
que `cp` ne garantit pas en mode WAL.

---

## Configuration

Tout passe par l'environnement.

| Variable | Défaut | Rôle |
| --- | --- | --- |
| `KALENDRA_DB` | `/data/kalendra.db` | Chemin de la base SQLite |
| `KALENDRA_HOST` | `0.0.0.0` | Interface d'écoute |
| `KALENDRA_PORT` | `5232` | Port d'écoute |
| `KALENDRA_BASE_PATH` | *(vide)* | Préfixe d'URL derrière un proxy |
| `KALENDRA_PUBLIC_URL` | *(déduit)* | URL publique affichée dans l'admin |
| `KALENDRA_ADMIN_USER` | *(vide)* | Admin créé au premier démarrage |
| `KALENDRA_ADMIN_PASSWORD` | *(vide)* | Mot de passe de cet admin |
| `KALENDRA_ADMIN_UI` | `true` | Active l'interface web d'administration |
| `KALENDRA_FEEDS` | `true` | Active les flux ICS publics |
| `KALENDRA_FEED_REFRESH` | `60` | `REFRESH-INTERVAL` annoncé, en minutes |
| `KALENDRA_MAX_RESOURCE_SIZE` | `1048576` | Taille max d'un objet en `PUT` |
| `KALENDRA_MAX_REQUEST_BODY` | `8388608` | Taille max d'un corps de requête |
| `KALENDRA_AUTH_CACHE_TTL` | `60` | Cache des identifiants validés (s), `0` désactive |
| `KALENDRA_LOG_LEVEL` | `info` | `debug`, `info`, `warning`, `error` |

---

## Ligne de commande

```sh
kalendra user add will --admin --with-calendar perso
kalendra user list
kalendra user passwd will
kalendra calendar add will astreinte --display-name "Astreinte" --color '#e01b24'
kalendra calendar list
kalendra calendar token will perso     # régénère le jeton du flux ICS
kalendra serve --host 0.0.0.0 --port 5232
```

Dans un conteneur : `docker exec -it kalendra python -m kalendra user list`.

---

## Ce qui est implémenté

**RFC 4918 (WebDAV)** — `OPTIONS`, `PROPFIND` (`Depth: 0/1`, `allprop`,
`propname`), `PROPPATCH`, `GET`, `PUT`, `DELETE`, `MKCOL`, réponses
`207 Multi-Status`, ETags forts avec `If-Match` / `If-None-Match`.

**RFC 4791 (CalDAV)** — `MKCALENDAR`, découverte
(`current-user-principal`, `calendar-home-set`, `calendar-user-address-set`,
`supported-calendar-component-set`, `supported-report-set`,
`current-user-privilege-set`), rapports `calendar-query` (filtres de
composant, `time-range`, `prop-filter`/`text-match`), `calendar-multiget` et
`free-busy-query`, préconditions `valid-calendar-data`, `no-uid-conflict`,
`supported-calendar-component`.

**RFC 6578 (synchronisation)** — `sync-collection` avec jetons opaques
monotones, suppressions signalées en `404`, plus le `getctag` d'Apple pour les
clients plus anciens.

**RFC 5545 (iCalendar)** — analyseur complet des lignes de contenu (dépliage,
paramètres entre guillemets, échappement TEXT), `DATE` / `DATE-TIME` /
`DURATION`, résolution des `TZID` via la base système (y compris les noms
Microsoft courants), expansion des récurrences `FREQ`, `INTERVAL`, `COUNT`,
`UNTIL`, `BYDAY` (avec ordinal), `BYMONTHDAY`, `BYMONTH`, `BYHOUR`,
`BYMINUTE`, `BYSETPOS`, `WKST`, ainsi que `EXDATE` et `RDATE`.

### Limites assumées

- Pas de planification (`VFREEBUSY` inter-utilisateurs, boîtes
  `schedule-inbox` / `schedule-outbox` de la RFC 6638) : Kalendra ne fait pas
  d'invitations par courriel.
- Pas de CardDAV (contacts).
- Pas de `LOCK` / `UNLOCK` : les ETags suffisent à détecter les conflits, et
  aucun client CalDAV courant n'exige le verrouillage.
- `MOVE` et `COPY` ne sont pas implémentés (les clients ré-écrivent l'objet).
- `BYYEARDAY` et `BYWEEKNO` ne sont pas expansés : ces événements sont alors
  considérés comme candidats à toute plage horaire, donc renvoyés au client
  qui filtrera lui-même. On préfère un événement en trop à un événement perdu.
- Les partages entre utilisateurs ne sont pas gérés : chaque agenda appartient
  à un compte (un administrateur voit tout).

---

## Développement

Aucune dépendance à installer pour lancer la suite :

```sh
python -m unittest discover -s tests -t tests -v
```

Plus de cent tests couvrent l'analyseur iCalendar, l'expansion des
récurrences, le protocole CalDAV, les flux ICS, l'interface d'administration,
la CLI, et un serveur HTTP réellement démarré (pour vérifier que `PROPFIND`,
`REPORT` et `MKCALENDAR` traversent bien le socket).

Analyse statique :

```sh
pipx install ruff && ruff check src tests
```

Test de fumée contre un conteneur en cours d'exécution :

```sh
./scripts/smoke.sh http://127.0.0.1:5232 admin motdepasse
```

Validation avec un vrai client tiers (nécessite `pip install caldav`) :

```sh
KALENDRA_TEST_URL=http://127.0.0.1:5232 \
KALENDRA_TEST_USER=admin KALENDRA_TEST_PASSWORD=… \
python -m unittest discover -s tests/integration -t .
```

### Architecture

```
src/kalendra/
├── app.py         routage, authentification Basic, cache d'identifiants
├── server.py      serveur HTTP stdlib (tout verbe accepté)   ← par défaut
├── asgi.py        adaptateur ASGI facultatif (uvicorn --http h11)
├── dav.py         méthodes WebDAV/CalDAV et rapports
├── props.py       résolution des propriétés PROPFIND
├── resources.py   arbre de ressources et résolution d'URL
├── db.py          schéma SQLite, transactions, révisions de sync
├── ics.py         analyseur/générateur iCalendar
├── rrule.py       expansion des récurrences
├── feed.py        flux ICS publics
├── admin.py       interface web d'administration
└── cli.py         ligne de commande
```

Le cœur est **synchrone** : `Kalendra.dispatch(Request) -> Response` ne
connaît rien du transport, ce qui rend chaque test unitaire immédiat et
permet de changer de serveur HTTP sans toucher à la logique.

### Publier une version

1. Mettre à jour `__version__` dans `src/kalendra/__init__.py`.
2. Ajouter la section correspondante dans `CHANGELOG.md`.
3. `git tag v1.2.3 && git push --tags`.

Le workflow `release.yml` vérifie la cohérence version/étiquette/changelog,
rejoue les tests, publie l'image multi-architecture (amd64 + arm64) sur GHCR
avec SBOM et attestation de provenance, puis crée la release GitHub.

---

## Licence

MIT — voir [LICENSE](LICENSE).

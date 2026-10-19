# Kalendra

Serveur **CalDAV** autonome, à lancer avec Docker, qui stocke tout dans une
base **SQLite** unique. Inspiré de Baïkal, mais sans PHP, sans nginx et sans
la moindre dépendance : Kalendra n'utilise que la bibliothèque standard de
Python.

- **CalDAV en lecture/écriture** pour GNOME Evolution (Fedora), Thunderbird,
  DAVx⁵ et iOS/macOS.
- **CardDAV en lecture/écriture** pour les mêmes clients : les carnets
  d'adresses vivent sous `/addressbooks/<compte>/<carnet>/`.
- **Flux ICS en lecture seule** pour Google Calendar et Proton Calendar, qui
  ne savent pas parler CalDAV à un serveur tiers (voir plus bas).
- **Une seule image, un seul fichier de données.** Sauvegarder ses agendas,
  c'est une commande : `kalendra backup`.

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

Ce `docker-compose.yml` démarre le serveur sur le port 5232, crée le compte
administrateur et conserve la base dans un volume `kalendra-data`.

```yaml
services:
  kalendra:
    image: ghcr.io/william-de71/kalendra:1
    container_name: kalendra
    restart: unless-stopped
    ports:
      # Publié sur toutes les interfaces : le serveur est joignable depuis le
      # réseau local. Derrière un reverse proxy TLS installé sur la même
      # machine, préférez "127.0.0.1:5232:5232" pour n'exposer que le proxy.
      - 5232:5232
    volumes:
      - kalendra-data:/data
    environment:
      # Compte administrateur créé au tout premier démarrage uniquement.
      KALENDRA_ADMIN_USER: "admin"
      KALENDRA_ADMIN_PASSWORD: "changez-moi-vraiment"
      # Facultatif : à ne renseigner que derrière un reverse proxy, pour que
      # l'interface d'administration affiche l'URL publique plutôt que celle
      # de la requête reçue.
      # KALENDRA_PUBLIC_URL: "https://cal.example.org"
      TZ: "Europe/Paris"
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5232/health', timeout=4).status==200 else 1)"]
      interval: 30s
      timeout: 5s
      retries: 3
    security_opt:
      - no-new-privileges:true
    read_only: true
    tmpfs:
      - /tmp

volumes:
  kalendra-data:
```

```sh
docker compose up -d
```

Le tag `:1` suit toutes les versions 1.x, correctifs et nouveautés compris,
sans rupture de compatibilité. Épinglez `:1.0.0` si vous préférez décider
vous-même quand mettre à jour.

Puis ouvrez `/admin` sur l'adresse de la machine qui héberge Docker — par
exemple <http://192.168.1.10:5232/admin> — avec le compte administrateur. La
page liste les comptes, et la fiche de chacun affiche l'URL CalDAV de ses
agendas, celle de ses carnets d'adresses et l'URL de chaque flux ICS.

Le mot de passe n'est lu qu'au tout premier démarrage, sur une base vide :
le changer ensuite dans le fichier n'a aucun effet, cela se fait depuis
`/admin`. Mettez-le plutôt dans un `.env` à côté du `docker-compose.yml`
(`KALENDRA_ADMIN_PASSWORD=…` puis `KALENDRA_ADMIN_PASSWORD: "${KALENDRA_ADMIN_PASSWORD:?}"`)
si vous préférez ne pas le voir en clair.

Sans docker-compose :

```sh
docker run -d --name kalendra \
  -p 5232:5232 \
  -v kalendra-data:/data \
  -e KALENDRA_ADMIN_USER=admin \
  -e KALENDRA_ADMIN_PASSWORD='…' \
  ghcr.io/william-de71/kalendra:1
```

Pour construire l'image depuis les sources plutôt que d'utiliser celle
publiée, le dépôt fournit son propre `docker-compose.yml` :

```sh
git clone https://github.com/William-De71/Kalendra.git
cd Kalendra
cp .env.example .env       # renseignez KALENDRA_ADMIN_PASSWORD
docker compose up -d
```

Sans Docker du tout (Python ≥ 3.11, rien à installer) :

```sh
PYTHONPATH=src python -m kalendra --db ./kalendra.db user add admin --admin --with-calendar perso
PYTHONPATH=src KALENDRA_DB=./kalendra.db python -m kalendra serve
```

---

## Raccorder les clients

Le plus simple : ouvrez **`/view/compte/`** (« Mon compte » dans la barre
latérale). La page affiche les adresses exactes à saisir pour votre compte,
les étapes pour chaque client ci-dessous, un **profil de configuration Apple**
à télécharger, et permet de créer des **mots de passe d'application** — un par
appareil, révocable seul si l'appareil est perdu, et qui n'ouvre que la
synchronisation, jamais l'interface web. On y change aussi le mot de passe de
son compte (l'actuel est redemandé), et on y télécharge **toutes ses données**
en une archive `.zip` : agendas en `.ics`, carnets en `.vcf`.

Pour passer d'un compte à l'autre dans le navigateur, suivez **« Changer
d'utilisateur »** sous votre nom, en bas de la barre latérale : la fenêtre de
connexion réapparaît. (L'authentification HTTP Basic n'a pas de déconnexion à
proprement parler ; fermer le navigateur oublie aussi les identifiants.)

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

Téléchargez le profil depuis `/view/compte/` (« Télécharger le profil ») et
installez-le : il crée d'un coup le compte d'agendas et le compte de contacts,
seul le mot de passe est demandé — donnez-lui un mot de passe d'application.

À la main : `Réglages → Applications → Calendrier → Comptes → Ajouter un
compte → Autre → Compte CalDAV`. Serveur : `cal.example.org`. **TLS est
obligatoire** côté Apple : passez par un reverse proxy HTTPS.

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

## Consulter ses agendas et ses contacts depuis un navigateur

`/view/` liste les agendas et les carnets auxquels le compte connecté a accès, et
`/view/<utilisateur>/<agenda>/` affiche une grille mensuelle : semaines
commençant le lundi, récurrences développées, exceptions respectées, journées
entières sur toute leur durée. Un clic sur un événement ouvre son détail, y
compris sa source iCalendar brute. Les mois se parcourent par les liens
« précédent » et « suivant », ou directement par `?m=2026-03`.

Deux autres vues se choisissent dans la barre de titre : **Semaine**
(`?vue=semaine&d=2026-03-16`), qui liste tous les événements de chaque jour
au lieu de les replier en « +N autres », et **Liste** (`?vue=liste&m=2026-03`),
une ligne par événement avec ses horaires, pour les jours du mois qui ont
quelque chose. L'onglet **Tâches** (`?vue=taches`) liste les tâches (VTODO) en
cours par échéance puis priorité, signale les retards, et replie celles qui
sont terminées.

La **recherche** (`/view/recherche/?q=…`, champ en haut de `/view/`) parcourt
titres, lieux, descriptions et catégories de vos événements et tâches, sans
tenir compte des majuscules ni des accents : « reunion » trouve « Réunion ».
Les résultats à venir viennent d'abord, puis les passés.

`/view/contacts/<utilisateur>/<carnet>/` fait de même pour un carnet
d'adresses : la liste des fiches triée par nom, puis le détail d'un contact
(courriels, téléphones, adresses, organisation) et sa source vCard brute.

**Exporter** : chaque agenda se télécharge en un `.ics` (`?export=ics`) et
chaque carnet en un `.vcf` (`?export=vcf`), objets recopiés tels quels — de
quoi déménager vers un autre serveur, ou garder une copie d'un seul agenda.
Un carnet accepte aussi un `.vcf` **importé** (export de Google Contacts, de
Thunderbird, d'un téléphone) : une fiche par carte, et un réimport met à jour
au lieu de dupliquer.

C'est une **vue, pas un éditeur** : aucun événement ni contact ne se modifie
depuis ces pages. Créer et modifier des événements reste le travail de votre client
CalDAV, qui le fait mieux — fuseaux, récurrences avec exceptions, participants
et rappels sont exactement là où un éditeur maison accumulerait les bugs, avec
le risque de perdre des propriétés en réécrivant un objet.

Les heures sont affichées dans le fuseau du serveur (variable `TZ` du
conteneur). Les journées entières, elles, ne sont jamais converties : elles
resteraient sinon décalées d'un jour dans les fuseaux à l'ouest de Greenwich.

`KALENDRA_ADMIN_UI=false` coupe cette vue en même temps que l'administration.

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

Derrière un proxy, positionnez aussi **`KALENDRA_TRUST_PROXY=true`** (et
seulement dans ce cas) : sans lui, toutes les requêtes semblent venir du proxy,
et le freinage des connexions décrit ci-dessous bloquerait tout le monde à la
fois.

### Connexions et journal

Après `KALENDRA_AUTH_MAX_FAILURES` échecs d'authentification (10 par défaut)
en `KALENDRA_AUTH_LOCKOUT` secondes (300), une adresse reçoit `429 Too Many
Requests` pendant ce même délai, sans même que le mot de passe soit vérifié.
Le compteur porte sur l'adresse et jamais sur le compte : sinon n'importe qui
pourrait bloquer un utilisateur dont il connaît le nom. Les identifiants déjà
validés depuis moins d'une minute passent malgré le blocage, si bien qu'un
membre de la famille derrière la même box qu'un attaquant continue de
synchroniser.

Chaque requête produit une ligne de journal `clé=valeur`, lisible telle quelle
et analysable sans configuration par Loki, Vector ou `jq -R` :

```
ip=203.0.113.7 method=REPORT path=/calendars/alice/perso/ status=207 ms=4.2 user=alice
```

Le jeton des flux publics n'y figure jamais (`path=/feed/…`) : c'est leur seul
secret. Les sondes `/health` passent au niveau `debug`, et
`KALENDRA_ACCESS_LOG=false` coupe le journal.

### Quota

`KALENDRA_QUOTA_BYTES` limite l'espace d'un compte, agendas et carnets
confondus (`0`, la valeur par défaut, laisse illimité). Un `PUT` qui le
dépasserait reçoit `507` avec la précondition `DAV:quota-not-exceeded` de la
RFC 4331, et les collections annoncent `quota-available-bytes`. L'espace
utilisé figure sur la page « Mon compte ».

### Importer un calendrier public (vacances scolaires, jours fériés…)

Kalendra n'a **aucun client HTTP sortant** : il ne va jamais chercher un
calendrier ailleurs. Un fichier `.ics` public s'importe donc explicitement,
de deux façons.

**Depuis le navigateur**, sur `/view/` : dépliez « Ajouter un calendrier externe
(vacances scolaires, jours fériés…) », donnez un nom et déposez le fichier —
l'agenda est créé et rempli en une fois. Pour verser un fichier dans un agenda
existant, le même bloc figure sur sa page. Chaque utilisateur le fait sur ses
propres agendas, sans être administrateur.

**En ligne de commande**, avec `scripts/import-ics.py` — utile pour un `cron` :

```sh
# Vacances scolaires de la zone B, depuis data.education.gouv.fr
curl -O https://fr.ftp.opendatasoft.com/openscol/fr-en-calendrier-scolaire/Zone-B.ics
./scripts/import-ics.py --user admin --password '…' \
    --calendar vacances --file Zone-B.ics --create
```

Un `.ics` public agrège tous ses événements dans un seul `VCALENDAR`, alors que
CalDAV impose une ressource par événement : le script découpe le fichier et
dépose un objet par `VEVENT`, en recopiant l'entête et les `VTIMEZONE` dans
chacun. Le nom de chaque ressource dérive de l'UID d'origine, si bien que
**réimporter le même fichier met à jour les événements au lieu de les
dupliquer**. Ajoutez `--dry-run` pour voir le plan sans rien écrire.

Deux limites à connaître :

- **Ce n'est pas un abonnement.** Le calendrier scolaire couvre plusieurs années
  d'avance, donc un import annuel suffit ; pour une source qui bouge, relancez
  le script depuis un `cron`.
- **Les UID du fichier officiel changent à chaque régénération** par le
  ministère (ils dérivent de l'horodatage de production). Si le fichier amont est
  republié avec de nouveaux UID, un réimport créera de nouveaux objets à côté des
  anciens. Dans ce cas, videz l'agenda avant de réimporter — ou réservez-lui un
  agenda dédié, ce que fait `--create`, précisément pour pouvoir le vider sans
  risque.

L'agenda ainsi rempli se comporte comme n'importe quel autre : visible dans
Evolution ou DAVx⁵, consultable sous `/view/`, et exposable en lecture seule à
Google ou Proton via son flux `/feed/<jeton>.ics`.

### Sauvegarde

```sh
docker exec kalendra python -m kalendra backup /data/sauvegardes/
```

La commande produit une copie cohérente **serveur en marche**, ce que `cp` ne
garantit pas : en mode WAL, les dernières écritures vivent encore dans
`kalendra.db-wal`, et une copie prise au mauvais moment sort déchirée.
`kalendra backup` passe par l'API de sauvegarde de SQLite, vérifie l'intégrité
du résultat et produit un fichier autonome, sans `-wal` à côté. Donnée un
dossier, elle nomme le fichier `kalendra-AAAAMMJJ-HHMMSS.db` — de quoi la
lancer chaque nuit depuis un `cron` ; `--compact` (`VACUUM INTO`) donne un
fichier plus petit, au prix d'un peu plus de temps. Restaurer, c'est arrêter le
conteneur et remettre ce fichier à la place de `kalendra.db`.

Pour un seul agenda ou un seul carnet : `kalendra calendar export` et
`kalendra addressbook export` ; pour tout un compte, `kalendra user export`
(voir la ligne de commande).

#### Sauvegarde automatique

Inutile de passer par le `cron` de l'hôte : avec **`KALENDRA_BACKUP_DIR`**, le
serveur fait lui-même une sauvegarde chaque nuit à `KALENDRA_MAINTENANCE_HOUR`
(3 h, heure locale du conteneur) et ne garde que les `KALENDRA_BACKUP_KEEP`
plus récentes (7). La rotation ne supprime jamais que des fichiers nommés
comme ses propres sauvegardes.

```yaml
    environment:
      KALENDRA_BACKUP_DIR: "/data/sauvegardes"
```

Le dossier vit alors dans le même volume que la base : cela protège d'une
erreur de manipulation ou d'une corruption, pas de la perte du disque.
Recopiez-le ailleurs (un second volume, un `rsync` vers une autre machine)
pour cela. Un échec est journalisé et compté dans les métriques, sans arrêter
les sauvegardes suivantes ; `kalendra maintenance` lance le même passage à la
main.

#### Journal de synchronisation

Chaque objet supprimé laisse une trace, qui permet à un client de savoir qu'il
doit l'effacer lui aussi. Ces traces sont oubliées au bout de
`KALENDRA_SYNC_RETENTION_DAYS` jours (90 ; `0` les garde toujours), au même
passage nocturne. Un client resté hors ligne plus longtemps refait simplement
une synchronisation complète.

### Métriques Prometheus

`KALENDRA_METRICS=true` sert `/metrics` au format texte de Prometheus :
requêtes par méthode et statut, histogramme des durées, échecs et blocages
d'authentification, nombre de comptes, de collections et d'objets, taille de
la base, et l'état de la sauvegarde automatique — de quoi alerter si
`time() - kalendra_backup_last_success_timestamp_seconds` dépasse deux jours.
Aucun nom de compte ni chemin n'y figure.

La route n'est pas derrière l'authentification Basic : positionnez
`KALENDRA_METRICS_TOKEN` pour exiger `Authorization: Bearer <jeton>` du
collecteur, ou ne l'exposez pas au-delà du réseau interne.

---

## Configuration

Tout passe par l'environnement.

| Variable | Défaut | Rôle |
| --- | --- | --- |
| `KALENDRA_DB` | `/data/kalendra.db` | Chemin de la base SQLite |
| `KALENDRA_HOST` | `0.0.0.0` | Interface d'écoute *dans* le conteneur — à laisser telle quelle : l'accès réseau se règle par `ports:` |
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
| `KALENDRA_AUTH_MAX_FAILURES` | `10` | Échecs de connexion tolérés par adresse, `0` désactive le freinage |
| `KALENDRA_AUTH_LOCKOUT` | `300` | Fenêtre de comptage et durée du blocage (s) |
| `KALENDRA_TRUST_PROXY` | `false` | Lit l'adresse du client dans `X-Forwarded-For` (derrière un proxy uniquement) |
| `KALENDRA_QUOTA_BYTES` | `0` | Espace par compte en octets, `0` = illimité |
| `KALENDRA_ACCESS_LOG` | `true` | Une ligne de journal par requête |
| `KALENDRA_BACKUP_DIR` | *(vide)* | Dossier de la sauvegarde automatique quotidienne |
| `KALENDRA_BACKUP_KEEP` | `7` | Sauvegardes automatiques conservées, `0` = toutes |
| `KALENDRA_MAINTENANCE_HOUR` | `3` | Heure locale du passage de maintenance |
| `KALENDRA_SYNC_RETENTION_DAYS` | `90` | Durée de conservation des suppressions, `0` = toujours |
| `KALENDRA_METRICS` | `false` | Sert `/metrics` (Prometheus) |
| `KALENDRA_METRICS_TOKEN` | *(vide)* | Jeton Bearer exigé sur `/metrics` |
| `KALENDRA_LOG_LEVEL` | `info` | `debug`, `info`, `warning`, `error` |

---

## Ligne de commande

```sh
kalendra user add admin --admin --with-calendar perso --with-addressbook contacts
kalendra user list
kalendra user passwd admin
kalendra calendar add admin astreinte --display-name "Astreinte" --color '#e01b24'
kalendra calendar list
kalendra calendar token admin perso     # régénère le jeton du flux ICS
kalendra calendar export admin perso -o perso.ics
kalendra calendar import admin vacances Zone-B.ics
kalendra addressbook add admin contacts --display-name "Contacts"
kalendra addressbook list
kalendra addressbook export admin contacts -o contacts.vcf
kalendra addressbook import admin contacts google-contacts.vcf
kalendra addressbook rm admin contacts
kalendra app-password add admin "téléphone"   # affiche le secret une seule fois
kalendra app-password list admin
kalendra app-password rm admin 3
kalendra backup /data/sauvegardes/            # ou un nom de fichier ; --compact
kalendra maintenance                          # purge et sauvegarde, comme chaque nuit
kalendra user export admin -o admin.zip       # tous les agendas et carnets du compte
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
`free-busy-query`, options `<C:expand>` et `<C:limit-recurrence-set>` de
`calendar-data`, préconditions `valid-calendar-data`, `no-uid-conflict`,
`supported-calendar-component`.

**RFC 4331 (quotas)** — `quota-used-bytes`, `quota-available-bytes` et la
précondition `quota-not-exceeded`.

**RFC 6578 (synchronisation)** — `sync-collection` avec jetons opaques
monotones, suppressions signalées en `404`, plus le `getctag` d'Apple pour les
clients plus anciens.

**RFC 5545 (iCalendar)** — analyseur complet des lignes de contenu (dépliage,
paramètres entre guillemets, échappement TEXT), `DATE` / `DATE-TIME` /
`DURATION`, résolution des `TZID` via la base système (y compris les noms
Microsoft courants), expansion des récurrences `FREQ`, `INTERVAL`, `COUNT`,
`UNTIL`, `BYDAY` (avec ordinal), `BYMONTHDAY`, `BYMONTH`, `BYWEEKNO`,
`BYYEARDAY`, `BYHOUR`, `BYMINUTE`, `BYSETPOS`, `WKST`, ainsi que `EXDATE`,
`RDATE` et les exceptions `RECURRENCE-ID`. Les règles sont développées dans le
fuseau de leur `DTSTART`, si bien qu'une réunion hebdomadaire à 10 h reste à
10 h de part et d'autre du changement d'heure.

### Limites assumées

- Pas d'édition d'événements depuis le navigateur : la vue mensuelle est en
  lecture seule, par choix (voir la section précédente).
- Pas de planification (`VFREEBUSY` inter-utilisateurs, boîtes
  `schedule-inbox` / `schedule-outbox` de la RFC 6638) : Kalendra ne fait pas
  d'invitations par courriel.
- Pas de `LOCK` / `UNLOCK` : les ETags suffisent à détecter les conflits, et
  aucun client CalDAV courant n'exige le verrouillage.
- `MOVE` et `COPY` ne sont pas implémentés (les clients ré-écrivent l'objet).
- Une règle de récurrence hors du sous-ensemble géré (un `BYDAY` ordinal
  combiné à `BYWEEKNO`, une valeur hors des bornes de la RFC) n'est pas
  développée : l'événement est alors considéré comme candidat à toute plage
  horaire, donc renvoyé au client qui filtrera lui-même. On préfère un
  événement en trop à un événement perdu.
- Les partages entre utilisateurs ne sont pas gérés : chaque agenda appartient
  à un compte (un administrateur voit tout).

---

## Développement

Aucune dépendance à installer pour lancer la suite :

```sh
python -m unittest discover -s tests -t tests -v
```

Plus de trois cents tests couvrent l'analyseur iCalendar, l'expansion des
récurrences, le protocole CalDAV, les flux ICS, l'interface d'administration,
la CLI, et un serveur HTTP réellement démarré (pour vérifier que `PROPFIND`,
`REPORT` et `MKCALENDAR` traversent bien le socket).

`tests/test_fuzz.py` soumet en plus aux analyseurs des objets mutés au hasard
(graine fixe, donc reproductible) et vérifie qu'aucun ne provoque d'erreur 500.
Pour chercher plus loin en local :

```sh
KALENDRA_FUZZ_ROUNDS=20000 KALENDRA_FUZZ_SEED=7 \
  python -m unittest discover -s tests -t tests -p test_fuzz.py
```

Analyse statique :

```sh
pipx install ruff && ruff check src tests
```

Vérification de bout en bout contre une instance en cours d'exécution (fonctionne
aussi bien contre un conteneur local que contre votre déploiement réel) :

```sh
./scripts/verify-deployment.sh http://127.0.0.1:5232 admin motdepasse
```

Validation avec un vrai client tiers (nécessite `pip install caldav`, qui
apporte aussi `icalendar`, avec lequel `tests/integration/test_feed_consumer.py`
relit les flux publics comme le ferait Google) :

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
├── calendarview.py vue mensuelle en lecture seule
└── cli.py         ligne de commande
```

Le cœur est **synchrone** : `Kalendra.dispatch(Request) -> Response` ne
connaît rien du transport, ce qui rend chaque test unitaire immédiat et
permet de changer de serveur HTTP sans toucher à la logique.

### Publier une version

Kalendra suit le **versionnage sémantique** : `patch` pour un correctif,
`minor` pour une nouveauté rétrocompatible, `major` pour une rupture de
compatibilité.

**Depuis GitHub** — le plus simple. Décrivez d'abord les changements sous
`## [Non publié]` dans `CHANGELOG.md`, puis : onglet **Actions** →
**Préparer une version** → *Run workflow* → choisissez **patch**, **minor** ou
**major**.

Le workflow calcule le numéro suivant, rejoue les tests et le lint, met à jour
`__version__` et bascule « Non publié » vers une section datée, committe,
pousse l'étiquette `vX.Y.Z` et enchaîne sur la publication. Cochez
*Simuler* pour voir le numéro et le diff sans rien pousser.

**Depuis un poste**, si vous préférez maîtriser le commit :

1. Mettre à jour `__version__` dans `src/kalendra/__init__.py`.
2. Ajouter la section correspondante dans `CHANGELOG.md`.
3. `git tag v1.1.0 && git push --tags`.

Dans les deux cas, `release.yml` vérifie la cohérence version/étiquette/
changelog, rejoue les tests, publie l'image multi-architecture (amd64 + arm64)
sur GHCR avec SBOM et attestation de provenance, puis crée la release GitHub
en reprenant les notes du changelog.

---

## Licence

MIT — voir [LICENSE](LICENSE).

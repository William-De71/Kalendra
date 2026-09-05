# Kalendra — mémoire du projet

Ce fichier est lu automatiquement par Claude Code au démarrage d'une session
dans ce dépôt. Il consigne les décisions de conception et leurs raisons, pour
qu'on n'ait pas à les redécouvrir — ou pire, à les défaire par inadvertance.

Kalendra est un serveur CalDAV multi-utilisateur stockant tout dans une base
SQLite unique, avec des flux ICS publics en lecture seule. Cible : Python
≥ 3.11, déploiement Docker derrière un reverse proxy TLS.

---

## Décisions structurantes — à ne pas défaire sans raison

### Zéro dépendance d'exécution

`pyproject.toml` déclare `dependencies = []` et ce n'est pas un accident.
Conséquences concrètes :

- L'image Docker ne lance **aucun `pip install`** : elle copie `src/kalendra`
  dans `/app` et pose `PYTHONPATH=/app`. La construction ne touche pas le
  réseau, donc elle est reproductible et rapide.
- La suite de tests tourne sur un clone nu, sans environnement virtuel.
- Rien à surveiller côté chaîne d'approvisionnement.

C'est ce qui a imposé d'écrire nous-mêmes l'analyseur iCalendar (`ics.py`) et
l'expansion des récurrences (`rrule.py`) plutôt que d'utiliser `icalendar` et
`python-dateutil`.

**Avant d'ajouter une dépendance, il faut un argument qui pèse plus que tout
ça.** Les seules dépendances tolérées sont dans les extras : `asgi` (uvicorn,
facultatif) et `dev` (ruff, caldav, pytest — jamais requis pour exécuter).

### On ne réécrit jamais un objet calendrier

Le corps déposé par le client en `PUT` est stocké **tel quel** dans
`objects.data` et restitué octet pour octet en `GET`. On n'analyse l'objet que
pour en extraire des métadonnées d'index (UID, type, bornes temporelles,
récurrence, résumé).

La raison : un VEVENT contient des propriétés qu'on ne comprend pas
nécessairement (extensions X-, participants, pièces jointes, alarmes exotiques).
Les réécrire, c'est les perdre silencieusement à chaque synchronisation. Un
serveur de stockage n'a pas à avoir d'opinion sur le contenu.

Corollaire : toute fonctionnalité qui impliquerait de régénérer un objet
(éditeur web d'événements, normalisation, « nettoyage ») doit être considérée
avec méfiance.

### Le cœur est synchrone et ignore le transport

`Kalendra.dispatch(Request) -> Response` (`app.py`) ne sait rien de HTTP au-delà
de ces deux objets. Deux adaptateurs l'exposent :

- `server.py` — `ThreadingHTTPServer` de la stdlib, le mode par défaut.
- `asgi.py` — enveloppe ASGI facultative.

Pourquoi la stdlib plutôt qu'un framework : les verbes WebDAV (`PROPFIND`,
`REPORT`, `MKCALENDAR`, `PROPPATCH`) ne figurent dans aucune liste blanche.
`BaseHTTPRequestHandler` accepte n'importe quel jeton de méthode ; l'analyseur
httptools d'uvicorn, lui, refuse ce qu'il ne connaît pas — d'où le
`--http h11` obligatoire si on passe par ASGI.

Conséquence pratique pour les tests : ils appellent `dispatch()` directement,
sans socket. C'est pourquoi la suite complète s'exécute en moins d'une seconde.

### Les carnets d'adresses partagent les tables des agendas

CardDAV a été ajouté après coup. Plutôt que de dupliquer `calendars`, `objects`
et `changes`, un carnet est une ligne de `calendars` avec `kind =
'addressbook'` (colonne introduite par la migration de schéma v2).

Ce choix lui donne sans effort les révisions de synchronisation, les ETags et
le journal des changements — du code déjà éprouvé côté calendrier. La
contrepartie est que **toute requête sur `calendars` doit filtrer sur `kind`** :
`list_calendars()` et `list_all_calendars()` le font, et l'oublier ferait
apparaître les carnets là où l'on attend des agendas (CalDAV, `/view/`,
tableau de bord).

`Resource.collection_kind` porte la même distinction côté HTTP : c'est lui qui
décide du `resourcetype`, du `Content-Type` et de l'analyseur appliqué au `PUT`.

Comme pour les événements, **une carte n'est jamais réécrite** : `vcard.py`
n'en extrait que l'UID, le nom affiché et l'adresse mail.

### L'import de calendrier ne sort jamais sur le réseau

`/view/<user>/<agenda>/import` accepte un `.ics` **téléversé**, jamais une URL
que le serveur irait chercher. C'est délibéré : donner à tout compte le pouvoir
de déclencher une requête HTTP sortante ouvrirait la porte au SSRF (scan du
réseau interne, métadonnées cloud) et introduirait une dépendance réseau dans
un serveur qui n'en a aucune. L'utilisateur télécharge le fichier lui-même,
puis le dépose.

Ce sont les **seules routes en écriture de `/view/`** — import, création et
suppression d'agenda — d'où le jeton anti-CSRF sur chacune : le navigateur
rejoue automatiquement les identifiants Basic. Tout le reste de la vue refuse
les méthodes autres que `GET`/`HEAD`.

Chaque compte gère ses propres agendas sans être administrateur ; seul un
`is_admin` peut viser un autre compte. Créer un agenda et y verser un fichier
se fait en une seule requête, parce que c'est le geste réel pour un calendrier
externe — et si l'import ne produit aucun événement, l'agenda tout juste créé
est retiré plutôt que de laisser une coquille vide.

Un `.ics` publié agrège ses événements dans un seul `VCALENDAR` alors que
CalDAV impose une ressource par événement : `importics.py` découpe et recopie
l'entête et les `VTIMEZONE` dans chaque objet. Le nom de ressource dérive de
l'UID, si bien qu'un réimport met à jour au lieu de dupliquer. Un composant
illisible est ignoré et signalé, sans faire échouer les autres — importer 67
événements sur 68 vaut mieux que rien.

### Google et Proton ne font pas de CalDAV

Ce point revient sans cesse, autant l'écrire une fois pour toutes.

Google Calendar ne peut pas se connecter à un serveur CalDAV tiers : son API
CalDAV sert à lire *les agendas hébergés par Google*. Son interface n'offre que
« Ajouter un agenda → À partir de l'URL », c'est-à-dire un abonnement iCalendar
en lecture seule. Proton Calendar chiffre les agendas côté client, ce qui exclut
un accès CalDAV, et propose lui aussi uniquement l'abonnement par URL.

D'où `feed.py` : chaque agenda expose `/feed/<jeton>.ics`, **hors
authentification** (ces services ne présentent aucun identifiant), protégé par
un jeton aléatoire révocable. Le jeton *est* le secret.

Ne pas « corriger » l'absence d'authentification sur cette route : c'est le
choix de conception, pas un oubli.

---

## Carte du code

| Fichier | Rôle |
| --- | --- |
| `app.py` | Routage, authentification Basic, cache d'identifiants |
| `server.py` | Serveur HTTP stdlib (tout verbe accepté) |
| `asgi.py` | Adaptateur ASGI facultatif |
| `dav.py` | Méthodes WebDAV/CalDAV, filtres et rapports |
| `props.py` | Résolution des propriétés `PROPFIND` |
| `resources.py` | Arbre de ressources, résolution d'URL |
| `db.py` | Schéma SQLite, transactions, révisions de synchronisation |
| `ics.py` | Analyseur et générateur iCalendar |
| `importics.py` | Import d'un `.ics` agrégé, un objet par composant |
| `vcard.py` | Analyseur vCard (CardDAV) |
| `rrule.py` | Expansion des récurrences |
| `feed.py` | Flux ICS publics |
| `admin.py` | Interface web d'administration |
| `calendarview.py` | Vue mensuelle et vue contacts, en lecture seule |
| `cli.py` | Ligne de commande |
| `http.py` | `Request` / `Response`, sans framework |
| `xmlutil.py` | Espaces de noms DAV, sérialisation, analyse XML sûre |
| `security.py` | PBKDF2, jetons, ETags |
| `config.py` | Configuration, uniquement par variables d'environnement |

### Arbre d'URL

```
/                                     racine, découverte
/.well-known/caldav                   301 vers /
/.well-known/carddav                  301 vers /
/principals/<user>/                   principal
/calendars/<user>/                    calendar-home-set
/calendars/<user>/<agenda>/           collection calendrier
/calendars/<user>/<agenda>/<x>.ics    ressource
/addressbooks/<user>/                 addressbook-home-set
/addressbooks/<user>/<carnet>/        carnet d'adresses
/addressbooks/<user>/<carnet>/<x>.vcf carte de visite
/feed/<jeton>.ics                     flux public, sans authentification
/view/                                liste des agendas consultables
/view/<user>/<agenda>/?m=AAAA-MM      vue mensuelle, lecture seule
/view/<user>/<agenda>/<x>.ics         détail d'un objet
/view/<user>/<agenda>/import          POST, dépôt d'un .ics (CSRF)
/view/agendas/creer                   POST, création (+ import) d'un agenda
/view/<user>/<agenda>/supprimer       POST, suppression d'un agenda
/view/contacts/<user>/<carnet>/       liste des contacts, lecture seule
/view/contacts/<user>/<carnet>/<x>.vcf  fiche d'un contact
/admin…                               interface d'administration (is_admin)
/health                               sonde, sans authentification
```

`/view/` est accessible à tout compte authentifié, sur ses propres agendas et
carnets (un administrateur voit tout) ; `/admin` exige `is_admin`. Le préfixe
`/view/contacts/` n'est emprunté que si le segment suivant désigne un compte
existant — sans quoi un utilisateur nommé « contacts » perdrait l'accès à ses
propres agendas. Les deux sont
coupées ensemble par `KALENDRA_ADMIN_UI=false`.

### Fuseaux dans la vue mensuelle

Les instants sont convertis vers le fuseau local du serveur (variable `TZ`),
**sauf les journées entières** : elles sont stockées à minuit UTC, et les
convertir les ferait basculer la veille dans tout fuseau à l'ouest de
Greenwich. `remplir()` traite donc ces deux cas séparément — c'est
intentionnel, et couvert par `FuseauTests`.

La suite doit passer sous plusieurs fuseaux ; le vérifier avant de toucher à
l'affichage des dates :

```sh
for tz in UTC Europe/Paris America/Los_Angeles Pacific/Auckland; do
  TZ=$tz python -m unittest discover -s tests -t tests
done
```

### Synchronisation

`calendars.sync_rev` est un compteur monotone incrémenté à chaque écriture. Il
alimente à la fois le `getctag` d'Apple et le `sync-token` de la RFC 6578,
tous deux au format `urn:kalendra:sync:<calendar_id>:<rev>`.

La table `changes` garde une ligne par `href` avec sa dernière révision et un
drapeau `deleted`, ce qui permet de répondre à `sync-collection` sans conserver
un journal qui grossit indéfiniment.

**Attention :** changer le format du jeton invalide les synchronisations en
cours — les clients referont une synchronisation complète.

---

## Conventions

### Commentaires et documentation

**En anglais**, comme les identifiants et les messages de commit : le dépôt est
public et le vocabulaire des RFC est anglais de toute façon (`calendar`,
`sync_rev`, `href`). Ce fichier et le `README.md` restent en français, tout
comme l'interface web et les messages d'erreur HTTP — ce que voient les
utilisateurs.

Ils expliquent le **pourquoi**, jamais le quoi. Un commentaire qui paraphrase
la ligne suivante est du bruit ; un commentaire qui dit pourquoi on a écarté
l'approche évidente vaut de l'or six mois plus tard.

### Tests

`unittest` de la stdlib, jamais `pytest` comme dépendance requise — la suite
doit tourner sur un clone nu. Elle reste néanmoins exécutable *sous* pytest en
intégration continue.

```sh
python -m unittest discover -s tests -t tests -v
```

`tests/helpers.py` fournit `ServerTestCase` : application jetable sur une base
temporaire, deux comptes (`will` administrateur, `alice` simple utilisateur),
deux agendas, et un `Client` qui appelle `dispatch()` sans socket.

Ce fichier abaisse aussi `security.PBKDF2_ITERATIONS` à 1000. Sans ça la suite
prend vingt secondes au lieu d'une demi-seconde. Ne pas le remonter, et ne pas
non plus baisser la valeur de production (240 000).

`tests/test_server.py` démarre un vrai serveur sur un port éphémère : c'est le
seul endroit qui prouve que les verbes WebDAV traversent le socket. Ne pas le
convertir en test `dispatch()`.

`tests/integration/` n'est lancé qu'en intégration continue, contre un
conteneur démarré, avec la vraie bibliothèque cliente `caldav`.

`scripts/verify-deployment.sh` vérifie un déploiement réel avec `curl` :
il attrape ce que les tests unitaires ne peuvent pas voir — `PYTHONPATH`
mal posé, droits sur `/data`, `tzdata` absent, reverse proxy qui avale les
verbes WebDAV.

### Sécurité

- Aucune DTD acceptée dans le XML entrant (`xmlutil.parse_xml`) : cela
  neutralise XXE et « billion laughs » sans dépendance tierce.
- Un compte inexistant déclenche quand même une vérification PBKDF2 factice
  (`DUMMY_HASH`), pour ne pas révéler par le temps de réponse quels
  identifiants existent.
- Le cache d'identifiants (`_AuthCache`, 60 s par défaut) existe parce qu'un
  client CalDAV enchaîne les requêtes et que PBKDF2 coûte ~100 ms. Un
  changement de mot de passe prend donc effet au plus tard après ce délai.
- L'interface d'administration porte un jeton anti-CSRF sur chaque formulaire :
  le navigateur rejoue automatiquement les identifiants Basic, donc l'absence
  de jeton serait exploitable.
- Les noms de ressources déposées sont validés par `SAFE_HREF` (`dav.py`).

### Base de données

Toute écriture passe par `transaction()` (`BEGIN IMMEDIATE`) pour éviter les
conflits d'écriture concurrents. Exception : `init_db()` — `executescript()`
valide implicitement la transaction en cours et échouerait à l'intérieur.

Une connexion par thread, WAL activé.

---

## Périmètre volontairement exclu

- **Édition d'événements côté web.** Evolution et Thunderbird le font bien.
  Un éditeur maison devrait gérer fuseaux, journées entières, récurrences avec
  exceptions, participants et rappels — c'est là que sont les bugs, et le pire
  scénario est qu'il perde des propriétés en réécrivant un objet.
- **Planification RFC 6638** (invitations, `schedule-inbox`/`outbox`).
- **`LOCK` / `UNLOCK`** : les ETags suffisent, aucun client courant ne l'exige.
- **`MOVE` / `COPY`** : les clients ré-écrivent l'objet.
- **Partage d'agenda entre utilisateurs.**
- **`BYYEARDAY` et `BYWEEKNO`** dans `rrule.py` : non expansés. Ces objets sont
  alors considérés comme candidats à toute plage, donc renvoyés au client qui
  filtrera. Principe général : **en cas de doute, on montre l'événement plutôt
  que de le masquer** — un événement en trop se voit, un événement perdu non.

---

## Publier une version

Versionnage sémantique à partir de la 1.0.0 : `patch` pour un correctif,
`minor` pour une nouveauté rétrocompatible, `major` pour une rupture. Le
numéro majeur est un engagement envers les déploiements existants — casser la
compatibilité impose 2.0.0, ce n'est pas une routine.

Trois fichiers portent le numéro et doivent rester d'accord :
`src/kalendra/__init__.py`, `pyproject.toml` et `CHANGELOG.md`.
`scripts/bump-version.py` les met à jour ensemble et refuse de travailler si
les deux premiers divergent déjà.

Deux chemins, une seule publication.

**Actions → « Préparer une version » → patch / minor / major.**
`prepare-release.yml` calcule le numéro (`scripts/bump-version.py`), rejoue
tests et lint, réécrit `__version__` et bascule « Non publié » du CHANGELOG
vers une section datée, committe et pousse l'étiquette.

**Ou à la main :** `__version__`, section du CHANGELOG, puis
`git tag vX.Y.Z && git push --tags`.

`release.yml` fait la publication dans les deux cas : cohérence
version/étiquette/changelog, tests, image multi-architecture sur
`ghcr.io/william-de71/kalendra` avec SBOM et attestation, release GitHub.

**Attention à la casse :** le dépôt est `William-De71/Kalendra`, mais GHCR
refuse les majuscules (« repository name must be lowercase »). Les URLs
github.com gardent donc la casse du dépôt, les références `ghcr.io/` sont en
minuscules. Dans `release.yml`, `docker/metadata-action` s'en charge seul ;
l'attestation et la commande `docker pull` des notes de release passent par
une étape de mise en minuscules explicite.

**Pourquoi `workflow_call` et pas seulement le déclencheur par étiquette :**
un `push` effectué avec le `GITHUB_TOKEN` ne redéclenche aucun workflow — c'est
la protection anti-boucle de GitHub. L'étiquette poussée par
`prepare-release.yml` ne lancerait donc jamais `release.yml` ; celui-ci est
appelé explicitement. La seule alternative serait un jeton personnel à
maintenir en secret.

`scripts/bump-version.py` calcule tout avant d'écrire quoi que ce soit : une
écriture partielle laisserait `__version__` incrémenté sans section
correspondante, précisément l'incohérence que `release.yml` refuse ensuite.

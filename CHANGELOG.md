# Journal des modifications

Le format suit [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/) et le
versionnage sémantique.

## [Non publié]

## [1.0.0] — 2026-09-05

### Ajouté

#### Socle initial

- Serveur CalDAV complet sur SQLite : `PROPFIND`, `PROPPATCH`, `REPORT`,
  `MKCALENDAR`, `MKCOL`, `PUT`, `GET`, `DELETE`, `OPTIONS`.
- Rapports `calendar-query` (filtres de composant, plage temporelle,
  `text-match`), `calendar-multiget`, `free-busy-query` et `sync-collection`
  (RFC 6578) avec jetons de synchronisation incrémentale.
- Découverte conforme : `/.well-known/caldav`, `current-user-principal`,
  `calendar-home-set`, `getctag`, `supported-report-set`.
- Gestion des ETags avec `If-Match` / `If-None-Match` pour la détection de
  conflits d'écriture.
- Flux ICS publics en lecture seule (`/feed/<jeton>.ics`) destinés à Google
  Calendar et Proton Calendar, avec jeton révocable et réponse `304`.
- Analyseur iCalendar maison (RFC 5545) et expansion des récurrences
  (`FREQ`, `INTERVAL`, `COUNT`, `UNTIL`, `BYDAY`, `BYMONTHDAY`, `BYMONTH`,
  `BYSETPOS`, `EXDATE`, `RDATE`).
- Interface web d'administration protégée par jeton anti-CSRF et CLI
  (`kalendra user`, `kalendra calendar`).
- Multi-utilisateur avec cloisonnement strict des agendas ; comptes
  administrateurs.
- Image Docker sans dépendance Python, utilisateur non privilégié,
  `HEALTHCHECK` intégré.
- Suite de 100+ tests exécutable sans installer quoi que ce soit
  (`python -m unittest discover -s tests -t tests`).

#### Ajouté ensuite

- **CardDAV** (RFC 6352) : carnets d'adresses sous `/addressbooks/`, cartes de
  visite, rapports `addressbook-query` et `addressbook-multiget`,
  `sync-collection`, découverte `/.well-known/carddav`. Les carnets partagent
  les tables des agendas (colonne `calendars.kind`), ce qui leur donne les
  révisions de synchronisation et les ETags déjà éprouvés.
- `vcard.py` : analyseur vCard sans dépendance, réutilisant le dépliage de
  lignes d'`ics.py`.
- Vue web des contacts sous `/view/contacts/<user>/<carnet>/` : liste triée par
  nom, fiche détaillée (courriels, téléphones, adresses, organisation) et
  source vCard brute.
- Import d'un fichier `.ics` depuis l'interface web
  (`/view/<user>/<agenda>/import`) et création d'un agenda pré-rempli
  (`/view/agendas/creer`) : chaque compte intègre un calendrier externe —
  vacances scolaires, jours fériés — sans être administrateur.
- Suppression d'un agenda par son propriétaire depuis `/view/`.
- `importics.py` : découpe un `.ics` agrégé en une ressource par composant, le
  nom dérivant de l'UID pour qu'un réimport mette à jour au lieu de dupliquer.
- `scripts/import-ics.py` : même import en ligne de commande, pour un `cron`.
- Commandes `kalendra addressbook add|list|rm` et option
  `user add --with-addressbook`.
- Tableau de bord d'administration enrichi : compteurs globaux, état des
  services, et recherche sur les comptes.
- Fiche par utilisateur (`/admin/users/<id>`) : agendas, carnets, modification
  du compte et suppression, la liste d'accueil ne portant plus qu'une ligne par
  compte.
- `db.update_user()`, `db.count_admins()`, `db.stats()`, `db.object_counts()`,
  `db.list_addressbooks()`, `db.list_all_addressbooks()`.
- `http.parse_multipart()` : lecture d'un formulaire `multipart/form-data`,
  écrite à la main faute de `cgi.FieldStorage` en Python 3.13.
- Migration de schéma v2, appliquée automatiquement aux bases existantes.
- Vue mensuelle en lecture seule sous `/view/` : grille du mois, récurrences
  développées avec leurs exceptions, journées entières sur toute leur durée,
  navigation entre les mois, page de détail d'un événement montrant sa source
  iCalendar telle qu'elle est stockée.
- `ics.expand_occurrences()` : développe un objet en instances concrètes, en
  tenant compte de `RRULE`, `RDATE`, `EXDATE` et des remplacements par
  `RECURRENCE-ID`.
- `CLAUDE.md` : mémoire du projet — décisions de conception, conventions et
  pièges, lue automatiquement par Claude Code au démarrage d'une session.

### Modifié

- La vue mensuelle et la vue d'un objet renvoient vers l'administration pour un
  compte `is_admin`.
- `users/edit` ne réécrit que les champs présents dans le formulaire : un POST
  partiel n'efface plus l'adresse électronique.
- `list_calendars()` et `list_all_calendars()` filtrent sur `kind` pour que les
  carnets ne remontent pas là où l'on attend des agendas.
- L'en-tête `DAV:` annonce `addressbook`.

### Corrigé

- Le tri de la liste des contacts portait sur le HTML généré, donc sur l'ordre
  des noms de fichiers plutôt que sur les noms affichés.
- Un carnet d'adresses ne pouvait pas porter le même nom qu'un agenda du même
  compte : l'unicité portait sur `(user_id, name)` et non sur
  `(user_id, kind, name)`. `MKCOL` renvoyait alors 500. Corrigé par la
  migration de schéma v3 ; un conflit de nom réel renvoie désormais 409.
- `scripts/verify-deployment.sh` cherchait les URLs de flux sur `/admin`, où
  elles ne figurent plus depuis leur déplacement sur la fiche d'un compte. Le
  script s'interrompait silencieusement en annonçant un succès.

[Non publié]: https://github.com/William-De71/Kalendra/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/William-De71/Kalendra/releases/tag/v1.0.0

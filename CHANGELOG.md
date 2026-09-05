# Journal des modifications

Le format suit [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/) et le
versionnage sémantique.

## [Non publié]

## [1.0.0] — 2026-09-05

### Ajouté

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

[Non publié]: https://github.com/OWNER/kalendra/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/OWNER/kalendra/releases/tag/v1.0.0

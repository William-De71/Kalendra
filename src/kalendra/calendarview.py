"""Read-only month view.

Consult a calendar from a browser without going through a client. Deliberately
a view and not an editor: see the "Périmètre volontairement exclu" note in
CLAUDE.md. Nothing can be written from these pages beyond the import form, and
no calendar object is ever rewritten.

Rendering happens entirely server-side, with no JavaScript and no dependency:
a seven-column HTML table and links to move between months.
"""

from __future__ import annotations

import calendar as _calendar
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from html import escape
from urllib.parse import parse_qs, quote

from . import __version__
from .account import changer_utilisateur, handle_account
from .export import export_addressbook, export_calendar
from .http import Request, Response, error, parse_multipart, text_response
from .ics import (
    InvalidCalendarData,
    Occurrence,
    component_window,
    expand_occurrences,
    from_unix,
    parse_calendar,
    parse_datetime_value,
    to_unix,
    to_utc,
)
from .importics import importer
from .importvcf import importer_cartes
from .resources import Kind, resolve
from .security import csrf_valid
from .vcard import InvalidCardData, parse_vcard
from .webui import BASE_STYLE, rail, titre

MOIS = (
    "janvier",
    "février",
    "mars",
    "avril",
    "mai",
    "juin",
    "juillet",
    "août",
    "septembre",
    "octobre",
    "novembre",
    "décembre",
)

JOURS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")

#: How many events a cell shows before folding into "+N more".
MAX_PAR_JOUR = 4

STYLE = """
/* Palette, body and sidebar come from webui.BASE_STYLE; only the two shades
   the month grid needs are added here. */
:root { --hors:#f2f2f6; --today:#fff8e1; }
@media (prefers-color-scheme: dark) { :root { --hors:#191920; --today:#2a2416; } }
main { padding:20px 24px 40px; }
.mois { font-size:17px; font-weight:600; text-transform:capitalize; }
table.grille { width:100%; border-collapse:collapse; table-layout:fixed;
  border:1px solid var(--line); border-radius:12px; overflow:hidden; }
table.grille th { background:var(--card); color:var(--muted); font-size:12px;
  text-transform:uppercase; letter-spacing:.05em; padding:8px 6px;
  border-bottom:1px solid var(--line); }
table.grille td { border:1px solid var(--line); vertical-align:top; height:118px;
  padding:5px 6px; background:var(--card); }
table.grille td.hors { background:var(--hors); }
table.grille td.hors .numero { color:var(--muted); }
table.grille td.today { background:var(--today); }
.numero { font-size:12.5px; color:var(--muted); font-weight:600; }
.evenement { display:block; margin:3px 0 0; padding:2px 5px; border-radius:5px;
  font-size:12.5px; text-decoration:none; color:var(--fg);
  border-left:3px solid var(--accent); background:rgba(48,84,200,.09);
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
@media (prefers-color-scheme: dark) { .evenement { background:rgba(138,166,255,.14); } }
.evenement .heure { color:var(--muted); font-variant-numeric:tabular-nums; }
.reste { display:block; font-size:12px; color:var(--muted); margin-top:3px;
  text-decoration:none; }
.vide { color:var(--muted); padding:28px 0; text-align:center; }
.flash { padding:10px 14px; border-radius:10px; margin:0 24px 14px; border:1px solid var(--line);
  background:var(--card); }
details.import { margin-top:18px; border:1px solid var(--line); border-radius:10px;
  padding:12px 14px; background:var(--card); }
details.import summary { cursor:pointer; color:var(--accent); font-size:14px; }
details.import form { display:flex; gap:10px; flex-wrap:wrap; align-items:center;
  margin-top:12px; }
details.import input[type=file] { font:inherit; min-width:0; flex:1; }
details.import button { font:inherit; padding:7px 13px; border-radius:8px;
  border:1px solid transparent; background:var(--accent); color:#fff; cursor:pointer; }
details.import p { font-size:12.5px; margin:10px 0 0; }
details.import select { font:inherit; padding:7px 9px; border:1px solid var(--line);
  border-radius:8px; background:var(--bg); color:var(--fg); }
details.import form.danger { margin-top:14px; padding-top:12px;
  border-top:1px solid var(--line); }
details.import button.danger { background:transparent; border-color:var(--line);
  color:#b3261e; }
@media (prefers-color-scheme: dark) {
  details.import button.danger { color:#f2836b; }
}
section.detail { background:var(--card); border:1px solid var(--line);
  border-radius:12px; padding:18px 20px; max-width:760px; }
section.detail h2 { margin:0 0 14px; font-size:19px; }
dl { display:grid; grid-template-columns:auto 1fr; gap:8px 18px; margin:0; }
dt { color:var(--muted); font-size:13px; }
dd { margin:0; }
pre { background:var(--bg); border:1px solid var(--line); border-radius:8px;
  padding:12px; overflow-x:auto; font:12px/1.45 ui-monospace, SFMono-Regular,
  Menlo, monospace; margin-top:18px; }
ul.agendas { list-style:none; padding:0; margin:0; }
ul.agendas li { border:1px solid var(--line); border-radius:10px; padding:12px 14px;
  margin-bottom:10px; background:var(--card); display:flex; align-items:center; gap:10px; }
ul.agendas a { font-weight:600; text-decoration:none; }
ul.agendas .compteur { color:var(--muted); font-size:13px; margin-left:auto; }
h2.section { font-size:13px; text-transform:uppercase; letter-spacing:.06em;
  color:var(--muted); margin:26px 0 12px; }
h2.section:first-child { margin-top:0; }
/* Owner heading, shown only to an administrator: the accounts are the outline
   of the page, so they sit closer to their list than to the block above. */
h3.proprietaire { font-size:14px; margin:18px 0 8px; color:var(--fg); }
h3.proprietaire:first-of-type { margin-top:0; }
footer { color:var(--muted); font-size:12.5px; padding:0 24px 32px; }
/* View switcher: the current mode reads as pressed rather than as a link. */
.titre .actions a[aria-current] { background:var(--rail-active); font-weight:600;
  border-color:var(--line); color:var(--fg); }
.titre .actions .sep { width:1px; align-self:stretch; background:var(--line);
  margin:4px 2px; }
/* Week: every event of the day is listed, so cells grow instead of folding
   into "+N more", and titles may wrap. */
table.semaine td { height:340px; }
table.semaine .evenement { white-space:normal; }
table.semaine th .date { display:block; color:var(--fg); font-size:15px;
  text-transform:none; letter-spacing:0; }
table.semaine th.today .date { color:var(--accent); }
section.liste { max-width:760px; }
form.recherche { display:flex; gap:6px; }
form.recherche input { font:inherit; font-size:13.5px; padding:5px 10px; width:220px;
  border:1px solid var(--line); border-radius:8px; background:var(--bg); color:var(--fg); }
section.liste .agenda { color:var(--muted); font-size:12.5px; white-space:nowrap; }
section.liste .lieu { color:var(--muted); font-size:13px; }
section.taches li { align-items:baseline; }
section.taches .coche { color:var(--muted); width:1em; flex:none; }
section.taches .echeance { color:var(--muted); font-size:13px; margin-left:auto;
  white-space:nowrap; font-variant-numeric:tabular-nums; }
section.taches .retard { color:var(--danger); font-weight:600; }
section.taches .prio { font-size:11.5px; border:1px solid var(--line); border-radius:6px;
  padding:0 6px; color:var(--muted); white-space:nowrap; }
section.taches .prio.haute { color:var(--danger); border-color:currentColor; }
section.taches .faite a { color:var(--muted); text-decoration:line-through; }
section.taches details { margin-top:22px; }
section.taches summary { cursor:pointer; color:var(--muted); font-size:14px; }
section.liste h3 { font-size:14px; margin:22px 0 8px; }
/* "Lundi 5 octobre": French capitalises the sentence, not every word. */
section.liste h3::first-letter { text-transform:uppercase; }
section.liste h3:first-child { margin-top:0; }
section.liste h3.today { color:var(--accent); }
section.liste ul { list-style:none; margin:0; padding:0; border:1px solid var(--line);
  border-radius:10px; background:var(--card); }
section.liste li { display:flex; gap:14px; padding:9px 14px;
  border-top:1px solid var(--line); }
section.liste li:first-child { border-top:none; }
section.liste .heure { color:var(--muted); font-variant-numeric:tabular-nums;
  min-width:96px; font-size:13.5px; }
section.liste a { text-decoration:none; color:var(--fg); }
section.liste a:hover { color:var(--accent); }
@media (max-width:640px) {
  table.grille td { height:82px; }
  .evenement { font-size:11.5px; }
  main { padding:14px; }
}
"""


@dataclass
class Jour:
    jour: date
    dans_le_mois: bool
    aujourdhui: bool
    evenements: list[Occurrence] = field(default_factory=list)


# ------------------------------------------------------------------- calculs


def mois_precedent(annee: int, mois: int) -> tuple[int, int]:
    return (annee - 1, 12) if mois == 1 else (annee, mois - 1)


def mois_suivant(annee: int, mois: int) -> tuple[int, int]:
    return (annee + 1, 1) if mois == 12 else (annee, mois + 1)


def parse_mois(raw: str, defaut: date) -> tuple[int, int]:
    """Decode the `m=YYYY-MM` parameter, falling back to the default month."""
    try:
        annee, _, mois = raw.partition("-")
        annee_i, mois_i = int(annee), int(mois)
    except (ValueError, AttributeError):
        return defaut.year, defaut.month
    if not (1 <= mois_i <= 12) or not (1 <= annee_i <= 9999):
        return defaut.year, defaut.month
    return annee_i, mois_i


def parse_jour(raw: str, defaut: date) -> date:
    """Decode the `d=YYYY-MM-DD` parameter of the week view."""
    try:
        return date.fromisoformat(raw)
    except (ValueError, TypeError):
        return defaut


def semaine_de(jour: date, aujourdhui: date) -> list[list[Jour]]:
    """The Monday-to-Sunday week holding `jour`, as a one-row grid."""
    lundi = jour - timedelta(days=jour.weekday())
    return [
        [
            Jour(jour=j, dans_le_mois=True, aujourdhui=(j == aujourdhui))
            for j in (lundi + timedelta(days=n) for n in range(7))
        ]
    ]


def grille(annee: int, mois: int, aujourdhui: date) -> list[list[Jour]]:
    """Weeks of the month, starting on Monday, spill-over days included."""
    cal = _calendar.Calendar(firstweekday=0)
    return [
        [
            Jour(jour=j, dans_le_mois=(j.month == mois), aujourdhui=(j == aujourdhui))
            for j in semaine
        ]
        for semaine in cal.monthdatescalendar(annee, mois)
    ]


def _local(moment_unix: int, tz: timezone | None) -> datetime:
    """Convert a UTC instant to the server's display time zone."""
    moment = from_unix(moment_unix)
    return moment.astimezone(tz) if tz is not None else moment.astimezone()


def remplir(db, calendar_row, semaines: list[list[Jour]], tz: timezone | None) -> None:
    """Place occurrences into the grid cells.

    The queried window overflows by a day on each side: an event may start the
    previous day in UTC and land inside the grid once converted to the display
    time zone.
    """
    if not semaines:
        return
    debut = semaines[0][0].jour - timedelta(days=1)
    fin = semaines[-1][-1].jour + timedelta(days=2)
    borne_debut = to_unix(datetime(debut.year, debut.month, debut.day, tzinfo=UTC))
    borne_fin = to_unix(datetime(fin.year, fin.month, fin.day, tzinfo=UTC))

    cases = {jour.jour: jour for semaine in semaines for jour in semaine}

    for row in db.query_objects(
        calendar_row["id"], components=["VEVENT", "VTODO"], start=borne_debut, end=borne_fin
    ):
        for occurrence in expand_occurrences(row["data"], borne_debut, borne_fin, href=row["href"]):
            # A zero-length occurrence still occupies its start day; DTEND
            # being exclusive, step back a second for the last cell.
            dernier = max(occurrence.end - 1, occurrence.start)
            if occurrence.all_day:
                # An all-day event has no time of day: converting it to a zone
                # west of Greenwich would shift it to the previous day.
                debut_local = from_unix(occurrence.start).date()
                fin_locale = from_unix(dernier).date()
            else:
                debut_local = _local(occurrence.start, tz).date()
                fin_locale = _local(dernier, tz).date()
            courant = debut_local
            while courant <= fin_locale:
                case = cases.get(courant)
                if case is not None:
                    case.evenements.append(occurrence)
                courant += timedelta(days=1)

    for case in cases.values():
        case.evenements.sort(key=lambda o: (not o.all_day, o.start, o.summary))


# -------------------------------------------------------------------- rendu


def _page(
    titre_page: str, corps: str, base: str = "", admin: bool = False, connecte: str = ""
) -> Response:
    html = (
        "<!doctype html><html lang=fr><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width, initial-scale=1">'
        f"<title>{escape(titre_page)}</title><style>{BASE_STYLE}{STYLE}</style>"
        "</head><body>"
        + rail(base, "agendas", admin, connecte, __version__)
        + f"<div class=zone>{corps}</div></body></html>"
    )
    return text_response(200, html, "text/html; charset=utf-8")


def _lien_mois(base: str, user: str, nom: str, annee: int, mois: int) -> str:
    return f"{base}/view/{quote(user)}/{quote(nom)}/?m={annee:04d}-{mois:02d}"


def _puce(occurrence: Occurrence, base: str, user: str, nom: str, tz) -> str:
    cible = f"{base}/view/{quote(user)}/{quote(nom)}/{quote(occurrence.href)}"
    if occurrence.all_day:
        heure = ""
    else:
        heure = f"<span class=heure>{_local(occurrence.start, tz):%H:%M}</span> "
    titre = escape(occurrence.summary or "(sans titre)")
    return f"<a class=evenement href='{cible}' title='{titre}'>{heure}{titre}</a>"


def _formulaire_import(base: str, user: str, nom: str, token: str) -> str:
    """Upload an .ics into the displayed calendar.

    Folded into a <details>: this is an occasional action, it has no business
    taking up screen space below the grid on every visit.
    """
    if not token:
        return ""
    action = f"{base}/view/{quote(user)}/{quote(nom)}/import"
    suppression = f"{base}/view/{quote(user)}/{quote(nom)}/supprimer"
    return (
        "<details class=import><summary>Ajouter des événements depuis un fichier "
        ".ics (vacances scolaires, jours fériés…)</summary>"
        f"<form method=post action='{action}' enctype='multipart/form-data'>"
        f"<input type=hidden name=csrf value='{escape(token)}'>"
        "<input type=file name=fichier accept='.ics,text/calendar' required>"
        "<button>Importer</button></form>"
        "<p class=muted>Les événements sont ajoutés à cet agenda. Réimporter le "
        "même fichier met à jour les événements au lieu de les dupliquer. "
        "Kalendra ne télécharge rien : récupérez le fichier vous-même, puis "
        "déposez-le ici.</p>"
        f"<form method=post action='{suppression}' class=danger "
        "onsubmit=\"return confirm('Supprimer cet agenda et tous ses événements ?')\">"
        f"<input type=hidden name=csrf value='{escape(token)}'>"
        "<button class=danger>Supprimer cet agenda</button></form></details>"
    )


def _lien_vue(base: str, user: str, nom: str, **params: str) -> str:
    requete = "&".join(f"{cle}={quote(valeur)}" for cle, valeur in params.items())
    return f"{base}/view/{quote(user)}/{quote(nom)}/" + (f"?{requete}" if requete else "")


def _horaire(occurrence: Occurrence, tz) -> str:
    if occurrence.all_day:
        return "journée"
    debut = _local(occurrence.start, tz)
    fin = _local(max(occurrence.end, occurrence.start), tz)
    if fin.date() != debut.date() or fin == debut:
        return f"{debut:%H:%M}"
    return f"{debut:%H:%M}–{fin:%H:%M}"


def _entete_agenda(
    config,
    calendar_row,
    user: str,
    vue: str,
    periode: str,
    precedent: str | None,
    aujourdhui: str | None,
    suivant: str | None,
) -> str:
    """Title bar shared by the views: navigation, mode, export.

    The task list spans no period, so it passes no navigation links.
    """
    base = config.base_path
    nom = calendar_row["name"]

    def onglet(cle: str, texte: str, cible: str) -> str:
        marque = " aria-current='page'" if cle == vue else ""
        return f"<a href='{cible}'{marque}>{texte}</a>"

    return titre(
        "<span class=pastille style='background:"
        f"{escape(calendar_row['color'] or '#3584e4')}'></span>"
        f"<h1>{escape(calendar_row['display_name'] or nom)}</h1>"
        f"<span class=meta>{escape(periode)}</span>",
        (
            f"<a href='{precedent}' aria-label='période précédente'>←</a>"
            f"<a href='{aujourdhui}'>aujourd'hui</a>"
            f"<a href='{suivant}' aria-label='période suivante'>→</a>"
            "<span class=sep></span>"
            if precedent
            else ""
        )
        + onglet("mois", "Mois", _lien_vue(base, user, nom))
        + onglet("semaine", "Semaine", _lien_vue(base, user, nom, vue="semaine"))
        + onglet("liste", "Liste", _lien_vue(base, user, nom, vue="liste"))
        + onglet("taches", "Tâches", _lien_vue(base, user, nom, vue="taches"))
        + "<span class=sep></span>"
        f"<a href='{_lien_vue(base, user, nom, export='ics')}' download>Exporter (.ics)</a>",
    )


def _pied(total: int) -> str:
    return (
        f"<footer>{total} occurrence{'s' if total > 1 else ''} affichée"
        f"{'s' if total > 1 else ''} · vue en lecture seule : "
        "pour créer ou modifier un événement, utilisez votre client CalDAV.</footer>"
    )


def rendre_mois(
    config,
    calendar_row,
    user: str,
    annee: int,
    mois: int,
    tz,
    semaines: list[list[Jour]],
    aujourdhui: date,
    admin: bool = False,
    token: str = "",
    message: str = "",
    connecte: str = "",
) -> Response:
    base = config.base_path
    pa, pm = mois_precedent(annee, mois)
    sa, sm = mois_suivant(annee, mois)
    nom = calendar_row["name"]
    titre_agenda = calendar_row["display_name"] or nom

    lignes = []
    for semaine in semaines:
        cellules = []
        for jour in semaine:
            classes = []
            if not jour.dans_le_mois:
                classes.append("hors")
            if jour.aujourdhui:
                classes.append("today")
            visibles = jour.evenements[:MAX_PAR_JOUR]
            reste = len(jour.evenements) - len(visibles)
            puces = "".join(_puce(o, base, user, nom, tz) for o in visibles)
            if reste > 0:
                # The week view lists them all: the overflow links there.
                cible = _lien_vue(base, user, nom, vue="semaine", d=jour.jour.isoformat())
                puces += (
                    f"<a class=reste href='{cible}'>+{reste} autre{'s' if reste > 1 else ''}</a>"
                )
            attribut = f" class='{' '.join(classes)}'" if classes else ""
            cellules.append(f"<td{attribut}><div class=numero>{jour.jour.day}</div>{puces}</td>")
        lignes.append("<tr>" + "".join(cellules) + "</tr>")

    entetes = "".join(f"<th>{j[:3]}</th>" for j in JOURS)
    total = sum(len(jour.evenements) for semaine in semaines for jour in semaine)

    corps = (
        _entete_agenda(
            config,
            calendar_row,
            user,
            "mois",
            f"{MOIS[mois - 1]} {annee}",
            _lien_mois(base, user, nom, pa, pm),
            _lien_mois(base, user, nom, aujourdhui.year, aujourdhui.month),
            _lien_mois(base, user, nom, sa, sm),
        )
        + (f"<div class=flash>{escape(message)}</div>" if message else "")
        + f"<main><table class=grille><tr>{entetes}</tr>{''.join(lignes)}</table>"
        + _formulaire_import(base, user, nom, token)
        + "</main>"
        + _pied(total)
    )
    return _page(
        f"{titre_agenda} — {MOIS[mois - 1]} {annee}",
        corps,
        base,
        admin,
        connecte,
    )


def rendre_semaine(
    config,
    calendar_row,
    user: str,
    tz,
    semaine: list[Jour],
    aujourdhui: date,
    admin: bool = False,
    connecte: str = "",
) -> Response:
    base = config.base_path
    nom = calendar_row["name"]
    lundi, dimanche = semaine[0].jour, semaine[-1].jour

    entetes = "".join(
        f"<th{' class=today' if j.aujourdhui else ''}>{JOURS[n][:3]}"
        f"<span class=date>{j.jour.day}</span></th>"
        for n, j in enumerate(semaine)
    )
    cellules = []
    for jour in semaine:
        puces = "".join(_puce(o, base, user, nom, tz) for o in jour.evenements)
        classe = " class=today" if jour.aujourdhui else ""
        cellules.append(f"<td{classe}>{puces}</td>")
    total = sum(len(j.evenements) for j in semaine)

    if lundi.month == dimanche.month:
        periode = f"du {lundi.day} au {dimanche.day} {MOIS[dimanche.month - 1]} {dimanche.year}"
    else:
        periode = (
            f"du {lundi.day} {MOIS[lundi.month - 1]} au {dimanche.day} "
            f"{MOIS[dimanche.month - 1]} {dimanche.year}"
        )

    def vers(jour: date) -> str:
        return _lien_vue(base, user, nom, vue="semaine", d=jour.isoformat())

    corps = (
        _entete_agenda(
            config,
            calendar_row,
            user,
            "semaine",
            periode,
            vers(lundi - timedelta(days=7)),
            vers(aujourdhui),
            vers(lundi + timedelta(days=7)),
        )
        + f"<main><table class='grille semaine'><tr>{entetes}</tr>"
        f"<tr>{''.join(cellules)}</tr></table></main>" + _pied(total)
    )
    titre_agenda = calendar_row["display_name"] or nom
    return _page(f"{titre_agenda} — semaine {periode}", corps, base, admin, connecte)


def rendre_liste(
    config,
    calendar_row,
    user: str,
    annee: int,
    mois: int,
    tz,
    semaines: list[list[Jour]],
    aujourdhui: date,
    admin: bool = False,
    connecte: str = "",
) -> Response:
    """Agenda view: the month's days that hold something, one line per event."""
    base = config.base_path
    nom = calendar_row["name"]
    pa, pm = mois_precedent(annee, mois)
    sa, sm = mois_suivant(annee, mois)

    blocs = []
    total = 0
    for semaine in semaines:
        for jour in semaine:
            if not jour.dans_le_mois or not jour.evenements:
                continue
            total += len(jour.evenements)
            lignes = "".join(
                f"<li><span class=heure>{_horaire(o, tz)}</span>"
                f"<a href='{base}/view/{quote(user)}/{quote(nom)}/{quote(o.href)}'>"
                f"{escape(o.summary or '(sans titre)')}</a></li>"
                for o in jour.evenements
            )
            classe = " class=today" if jour.aujourdhui else ""
            blocs.append(
                f"<h3{classe}>{JOURS[jour.jour.weekday()]} {jour.jour.day} "
                f"{MOIS[jour.jour.month - 1]}</h3><ul>{lignes}</ul>"
            )

    def vers(a: int, m: int) -> str:
        return _lien_vue(base, user, nom, vue="liste", m=f"{a:04d}-{m:02d}")

    contenu = "".join(blocs) or "<p class=vide>Rien de prévu ce mois-ci.</p>"
    corps = (
        _entete_agenda(
            config,
            calendar_row,
            user,
            "liste",
            f"{MOIS[mois - 1]} {annee}",
            vers(pa, pm),
            vers(aujourdhui.year, aujourdhui.month),
            vers(sa, sm),
        )
        + f"<main><section class=liste>{contenu}</section></main>"
        + _pied(total)
    )
    titre_agenda = calendar_row["display_name"] or nom
    return _page(f"{titre_agenda} — {MOIS[mois - 1]} {annee}", corps, base, admin, connecte)


@dataclass
class Tache:
    href: str
    titre: str
    #: Due date in UTC, None when the task has none.
    echeance: datetime | None
    journee: bool
    faite: bool
    priorite: int
    avancement: int | None
    terminee: datetime | None


def _instant(composant, nom: str) -> tuple[datetime | None, bool]:
    """A DATE or DATE-TIME property in UTC, and whether it was a bare date."""
    prop = composant.get(nom)
    if prop is None:
        return None, False
    valeur = parse_datetime_value(prop.value, prop.param("TZID"))
    return to_utc(valeur), valeur is not None and not isinstance(valeur, datetime)


def lister_taches(db, calendar_row) -> list[Tache]:
    """The calendar's VTODOs, read from the stored objects without altering them.

    A recurring task shows once, through its master component: a to-do list
    wants "what is left to do", not every past and future instance.
    """
    taches = []
    for row in db.query_objects(calendar_row["id"], components=["VTODO"]):
        try:
            composants = parse_calendar(row["data"]).children
        except InvalidCalendarData:
            # Unreadable: still listed, under its summary, rather than hidden.
            taches.append(
                Tache(row["href"], row["summary"] or row["href"], None, False, False, 0, None, None)
            )
            continue
        todo = next(
            (c for c in composants if c.name == "VTODO" and c.get("RECURRENCE-ID") is None),
            None,
        )
        if todo is None:
            continue

        echeance, journee = _instant(todo, "DUE")
        terminee, _ = _instant(todo, "COMPLETED")
        statut = todo.value("STATUS").strip().upper()
        try:
            avancement = int(todo.value("PERCENT-COMPLETE") or "")
        except ValueError:
            avancement = None
        try:
            priorite = int(todo.value("PRIORITY") or "0")
        except ValueError:
            priorite = 0
        faite = statut in {"COMPLETED", "CANCELLED"} or terminee is not None or avancement == 100
        resume = todo.get("SUMMARY")
        taches.append(
            Tache(
                row["href"],
                (resume.text if resume else "") or "(sans titre)",
                echeance,
                journee,
                faite,
                priorite if 0 <= priorite <= 9 else 0,
                avancement,
                terminee,
            )
        )
    return taches


def _priorite(valeur: int) -> str:
    # RFC 5545 §3.8.1.9: 1-4 high, 5 medium, 6-9 low, 0 undefined.
    if 1 <= valeur <= 4:
        return "<span class='prio haute'>haute</span>"
    if valeur == 5:
        return "<span class=prio>moyenne</span>"
    if 6 <= valeur <= 9:
        return "<span class=prio>basse</span>"
    return ""


def rendre_taches(
    db, config, calendar_row, user: str, tz, admin: bool = False, connecte: str = ""
) -> Response:
    """Open tasks by due date then priority; completed ones folded below."""
    base = config.base_path
    nom = calendar_row["name"]
    maintenant = datetime.now(UTC)
    taches = lister_taches(db, calendar_row)
    a_faire = sorted(
        (t for t in taches if not t.faite),
        key=lambda t: (
            t.echeance is None,
            t.echeance or maintenant,
            t.priorite == 0,
            t.priorite,
            t.titre.casefold(),
        ),
    )
    faites = sorted(
        (t for t in taches if t.faite),
        key=lambda t: t.terminee or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )

    def ligne(tache: Tache) -> str:
        cible = f"{base}/view/{quote(user)}/{quote(nom)}/{quote(tache.href)}"
        quand = ""
        if tache.echeance is not None:
            if tache.journee:
                # Like all-day events: a date, never shifted by a time zone.
                texte = f"{tache.echeance:%d/%m/%Y}"
                en_retard = tache.echeance.date() < maintenant.date()
            else:
                texte = f"{_local(to_unix(tache.echeance), tz):%d/%m/%Y %H:%M}"
                en_retard = tache.echeance < maintenant
            classe = "echeance retard" if en_retard and not tache.faite else "echeance"
            quand = f"<span class='{classe}'>{texte}</span>"
        avancement = (
            f"<span class=prio>{tache.avancement} %</span>"
            if tache.avancement and 0 < tache.avancement < 100 and not tache.faite
            else ""
        )
        coche = "✓" if tache.faite else "○"
        classe = " class=faite" if tache.faite else ""
        return (
            f"<li{classe}><span class=coche aria-hidden=true>{coche}</span>"
            f"<a href='{cible}'>{escape(tache.titre)}</a>"
            f"{_priorite(tache.priorite) if not tache.faite else ''}{avancement}{quand}</li>"
        )

    contenu = (
        f"<ul>{''.join(ligne(t) for t in a_faire)}</ul>"
        if a_faire
        else "<p class=vide>Aucune tâche en cours.</p>"
    )
    if faites:
        contenu += (
            f"<details><summary>{len(faites)} tâche{'s' if len(faites) > 1 else ''} "
            f"terminée{'s' if len(faites) > 1 else ''}</summary>"
            f"<ul>{''.join(ligne(t) for t in faites)}</ul></details>"
        )
    corps = (
        _entete_agenda(
            config,
            calendar_row,
            user,
            "taches",
            f"{len(a_faire)} à faire",
            None,
            None,
            None,
        )
        + f"<main><section class='liste taches'>{contenu}</section></main>"
        "<footer>Vue en lecture seule : pour cocher ou modifier une tâche, utilisez "
        "votre client (Thunderbird, Evolution, Tasks.org…).</footer>"
    )
    titre_agenda = calendar_row["display_name"] or nom
    return _page(f"{titre_agenda} — tâches", corps, base, admin, connecte)


#: Beyond this many hits, the query needs narrowing rather than scrolling.
MAX_RESULTATS = 100


def _plier(texte: str) -> str:
    """Case- and accent-insensitive form: "Réunion" and "reunion" must meet."""
    decompose = unicodedata.normalize("NFKD", texte.casefold())
    return "".join(c for c in decompose if not unicodedata.combining(c))


def _formulaire_recherche(base: str, valeur: str = "") -> str:
    return (
        f"<form class=recherche method=get action='{base}/view/recherche/' role=search>"
        f"<input type=search name=q value='{escape(valeur)}' "
        "placeholder='Rechercher un événement…' aria-label='Rechercher'></form>"
    )


def chercher(db, calendriers, requete: str) -> list[tuple]:
    """Objects whose title, place, description or categories contain `requete`.

    Matching happens on the parsed text, not in SQL: LIKE folds ASCII case
    only, and "Réunion" would then miss "reunion". At a family's scale,
    reading every object of one's own calendars stays well under a second.
    """
    aiguille = _plier(requete.strip())
    trouves = []
    for proprietaire, calendrier in calendriers:
        for row in db.query_objects(calendrier["id"], components=["VEVENT", "VTODO", "VJOURNAL"]):
            try:
                composants = parse_calendar(row["data"]).children
            except InvalidCalendarData:
                composants = []
            textes = [row["summary"] or ""]
            lieu = ""
            journee = any(component_window(c)[2] for c in composants if c.name == "VEVENT")
            for composant in composants:
                for nom in ("SUMMARY", "LOCATION", "DESCRIPTION", "CATEGORIES"):
                    for prop in composant.all(nom):
                        textes.append(prop.text)
                        if nom == "LOCATION" and not lieu:
                            lieu = prop.text
            if any(aiguille in _plier(t) for t in textes):
                trouves.append((proprietaire, calendrier, row, lieu, journee))
                if len(trouves) > MAX_RESULTATS:
                    return trouves
    return trouves


def rendre_recherche(
    db, config, user_row, admin: bool, requete: str, connecte: str = ""
) -> Response:
    base = config.base_path
    if admin:
        calendriers = [(row["username"], row) for row in db.list_all_calendars()]
    else:
        calendriers = [(user_row["username"], row) for row in db.list_calendars(user_row["id"])]

    requete = requete.strip()[:200]
    if len(requete) < 2:
        contenu = "<p class=vide>Tapez au moins deux caractères.</p>"
        total = 0
    else:
        trouves = chercher(db, calendriers, requete)
        tronque = len(trouves) > MAX_RESULTATS
        trouves = trouves[:MAX_RESULTATS]
        total = len(trouves)
        maintenant = to_unix(datetime.now(UTC))
        # Upcoming first, soonest at the top; then the past, latest first —
        # what one searches a calendar for is usually ahead.
        a_venir = sorted(
            (t for t in trouves if (t[2]["dtend"] or t[2]["dtstart"] or 0) >= maintenant),
            key=lambda t: t[2]["dtstart"] or 0,
        )
        passes = sorted(
            (t for t in trouves if (t[2]["dtend"] or t[2]["dtstart"] or 0) < maintenant),
            key=lambda t: t[2]["dtstart"] or 0,
            reverse=True,
        )

        def ligne(trouve) -> str:
            proprietaire, calendrier, row, lieu, journee = trouve
            cible = (
                f"{base}/view/{quote(proprietaire)}/{quote(calendrier['name'])}/"
                f"{quote(row['href'])}"
            )
            if row["dtstart"] is None:
                quand = "—"
            elif journee:
                # Stored at midnight UTC: converting would move it to the day
                # before anywhere west of Greenwich.
                quand = f"{from_unix(row['dtstart']):%d/%m/%Y}"
            else:
                quand = f"{_local(row['dtstart'], None):%d/%m/%Y}"
            if row["recurring"]:
                quand += " ↻"
            agenda = calendrier["display_name"] or calendrier["name"]
            if admin:
                agenda = f"{proprietaire} · {agenda}"
            return (
                f"<li><span class=heure>{quand}</span>"
                f"<span><a href='{cible}'>{escape(row['summary'] or '(sans titre)')}</a>"
                + (f"<br><span class=lieu>{escape(lieu)}</span>" if lieu else "")
                + "</span>"
                f"<span class=agenda style='margin-left:auto'>"
                f"<span class=pastille style='background:"
                f"{escape(calendrier['color'] or '#3584e4')}'></span> {escape(agenda)}</span></li>"
            )

        blocs = []
        if a_venir:
            blocs.append(f"<h3>À venir</h3><ul>{''.join(ligne(t) for t in a_venir)}</ul>")
        if passes:
            blocs.append(f"<h3>Passés</h3><ul>{''.join(ligne(t) for t in passes)}</ul>")
        contenu = "".join(blocs) or "<p class=vide>Aucun résultat.</p>"
        if tronque:
            contenu += (
                f"<p class=vide>Plus de {MAX_RESULTATS} résultats : précisez la recherche.</p>"
            )

    corps = (
        titre(
            "<h1>Recherche</h1>"
            + (
                f"<span class=meta>{total} résultat{'s' if total > 1 else ''}</span>"
                if requete
                else ""
            ),
            _formulaire_recherche(base, requete),
        )
        + f"<main><section class=liste>{contenu}</section></main>"
        "<footer>Titres, lieux, descriptions et catégories ; les dates sont celles du "
        "premier passage (↻ : événement récurrent).</footer>"
    )
    return _page("Kalendra — recherche", corps, base, admin, connecte)


def rendre_index(
    db,
    config,
    user_row,
    admin: bool,
    token: str = "",
    message: str = "",
    connecte: str = "",
) -> Response:
    base = config.base_path
    if admin:
        agendas = [(row["username"], row) for row in db.list_all_calendars()]
    else:
        agendas = [(user_row["username"], row) for row in db.list_calendars(user_row["id"])]

    if admin:
        carnets = [(row["username"], row) for row in db.list_all_addressbooks()]
    else:
        carnets = [(user_row["username"], row) for row in db.list_addressbooks(user_row["id"])]

    def ligne(proprietaire: str, row, prefixe: str, unite: str) -> str:
        compte = db.calendar_stats(row["id"])
        cible = f"{base}/view/{prefixe}{quote(proprietaire)}/{quote(row['name'])}/"
        return (
            "<li>"
            f"<span class=pastille style='background:{escape(row['color'] or '#3584e4')}'>"
            "</span>"
            f"<a href='{cible}'>{escape(row['display_name'] or row['name'])}</a>"
            f"<span class=compteur>{compte} {unite}{'s' if compte > 1 else ''}</span></li>"
        )

    # Grouped by account for an administrator, who sees everyone's: a flat list
    # mixing accounts forces the reader to check the owner on every row. A
    # plain user only ever sees their own, so a heading would be noise.
    def liste(entrees, prefixe: str, unite: str, vide: str) -> str:
        if not entrees:
            return f"<ul class=agendas><li class=vide>{vide}</li></ul>"
        if not admin:
            return (
                "<ul class=agendas>"
                + "".join(ligne(p, row, prefixe, unite) for p, row in entrees)
                + "</ul>"
            )
        par_compte: dict[str, list] = {}
        for proprietaire, row in entrees:
            par_compte.setdefault(proprietaire, []).append(row)
        blocs = []
        for proprietaire in sorted(par_compte, key=str.casefold):
            lignes_compte = "".join(
                ligne(proprietaire, row, prefixe, unite) for row in par_compte[proprietaire]
            )
            blocs.append(
                f"<h3 class=proprietaire>{escape(proprietaire)}</h3>"
                f"<ul class=agendas>{lignes_compte}</ul>"
            )
        return "".join(blocs)

    creation = ""
    if token:
        # An administrator creates for any account, hence the selector; an
        # ordinary user has nothing to choose.
        if admin:
            options = "".join(
                f"<option value='{escape(u['username'])}'>{escape(u['username'])}</option>"
                for u in db.list_users()
            )
            choix = f"<select name=proprietaire>{options}</select>"
        else:
            choix = ""
        creation = (
            "<details class=import><summary>Nouvel agenda</summary>"
            f"<form method=post action='{base}/view/agendas/creer' "
            "enctype='multipart/form-data'>"
            f"<input type=hidden name=csrf value='{escape(token)}'>"
            f"{choix}"
            "<input name=name placeholder='identifiant (vacances)' required "
            "pattern='[A-Za-z0-9._-]{1,64}'>"
            "<input name=display_name placeholder='nom affiché'>"
            "<input name=color type=color value='#3584e4'>"
            "<button>Créer</button></form>"
            "<p class=muted>L'identifiant figure dans l'URL CalDAV de l'agenda : "
            "il n'est plus modifiable ensuite.</p></details>"
            "<details class=import><summary>Ajouter un calendrier externe "
            "(vacances scolaires, jours fériés…)</summary>"
            f"<form method=post action='{base}/view/agendas/creer' "
            "enctype='multipart/form-data'>"
            f"<input type=hidden name=csrf value='{escape(token)}'>"
            f"{choix}"
            "<input name=name placeholder='identifiant (vacances)' required "
            "pattern='[A-Za-z0-9._-]{1,64}'>"
            "<input type=file name=fichier accept='.ics,text/calendar' required>"
            "<button>Créer et importer</button></form>"
            "<p class=muted>Crée un agenda dédié et y verse le fichier. "
            "Pour les vacances scolaires : téléchargez le <code>.ics</code> de "
            "votre zone sur "
            "<code>fr.ftp.opendatasoft.com/openscol/fr-en-calendrier-scolaire/</code> "
            "(Zone-A.ics, Zone-B.ics, Zone-C.ics), puis déposez-le ici. "
            "Kalendra ne télécharge rien lui-même : récupérez le fichier, "
            "déposez-le. Un agenda dédié se vide et se réimporte sans toucher "
            "à vos propres événements.</p></details>"
        )

    corps = (
        titre("<h1>Agendas et contacts</h1>", _formulaire_recherche(base))
        + (f"<div class=flash>{escape(message)}</div>" if message else "")
        + "<main><h2 class=section>Agendas</h2>"
        + liste(agendas, "", "objet", "Aucun agenda.")
        + creation
        + "<h2 class=section>Carnets d'adresses</h2>"
        + liste(carnets, "contacts/", "contact", "Aucun carnet.")
        + "</main>"
    )
    return _page("Kalendra — agendas et carnets", corps, base, admin, connecte)


def rendre_carnet(
    db,
    config,
    carnet_row,
    user: str,
    admin: bool = False,
    connecte: str = "",
    token: str = "",
    message: str = "",
) -> Response:
    """List an address book's cards, sorted by display name."""
    base = config.base_path
    nom = carnet_row["name"]
    titre_carnet = carnet_row["display_name"] or nom

    fiches = []
    for row in db.list_objects(carnet_row["id"]):
        try:
            card = parse_vcard(row["data"])
        except InvalidCardData:
            # Unreadable card: show it anyway, under its href — hiding it would
            # suggest it does not exist.
            card = None
        affiche = (card.fn if card and card.fn else "") or row["summary"] or row["href"]
        fiches.append((affiche, card.email if card else "", row["href"]))

    # Sort on the display name, case-insensitively: without an explicit key we
    # would sort the HTML, and so the order of hrefs.
    fiches.sort(key=lambda f: f[0].casefold())

    lignes = []
    for affiche, courriel, href in fiches:
        cible = f"{base}/view/contacts/{quote(user)}/{quote(nom)}/{quote(href)}"
        lignes.append(
            "<tr>"
            f"<td><a href='{cible}'>{escape(affiche)}</a></td>"
            f"<td class=muted>{escape(courriel)}</td>"
            "</tr>"
        )

    ici = f"{base}/view/contacts/{quote(user)}/{quote(nom)}/"
    import_form = ""
    if token:
        import_form = (
            "<details class=import><summary>Ajouter des contacts depuis un fichier "
            ".vcf</summary>"
            f"<form method=post action='{ici}import' enctype='multipart/form-data'>"
            f"<input type=hidden name=csrf value='{escape(token)}'>"
            "<input type=file name=fichier accept='.vcf,text/vcard' required>"
            "<button>Importer</button></form>"
            "<p class=muted>Un export de Google Contacts, de Thunderbird ou d'un "
            "téléphone convient. Réimporter le même fichier met à jour les fiches "
            "au lieu de les dupliquer.</p></details>"
        )
    corps = (
        titre(
            "<span class=pastille style='background:"
            f"{escape(carnet_row['color'] or '#3584e4')}'></span>"
            f"<h1>{escape(titre_carnet)}</h1>",
            f"<a href='{ici}?export=vcf' download>Exporter (.vcf)</a>",
        )
        + (f"<div class=flash>{escape(message)}</div>" if message else "")
        + "<main><table class=grille style='table-layout:auto'>"
        "<tr><th>Nom</th><th>Courriel</th></tr>"
        + ("".join(lignes) or "<tr><td colspan=2 class=vide>Ce carnet est vide.</td></tr>")
        + "</table>"
        + import_form
        + "</main>"
        f"<footer>{len(lignes)} contact{'s' if len(lignes) > 1 else ''} · "
        "vue en lecture seule : pour modifier une fiche, utilisez votre client "
        "CardDAV.</footer>"
    )
    return _page(f"{titre_carnet} — contacts", corps, base, admin, connecte)


def rendre_contact(
    config, carnet_row, user: str, row, admin: bool = False, connecte: str = ""
) -> Response:
    """One card in detail: the common properties, then the raw source."""
    base = config.base_path
    nom = carnet_row["name"]
    try:
        card = parse_vcard(row["data"])
    except InvalidCardData:
        card = None

    titre_fiche = (card.fn if card and card.fn else "") or row["summary"] or "(sans nom)"

    lignes: list[tuple[str, str]] = []
    if card is not None:
        for etiquette, prop in (
            ("Courriel", "EMAIL"),
            ("Téléphone", "TEL"),
            ("Adresse", "ADR"),
            ("Organisation", "ORG"),
            ("Fonction", "TITLE"),
            ("Anniversaire", "BDAY"),
            ("Note", "NOTE"),
            ("Site", "URL"),
        ):
            # A card may carry the same property several times (two phone
            # numbers, three addresses): show them all.
            for valeur in card.all(prop):
                texte = valeur.text.replace(";", " ").strip()
                if texte:
                    type_ = valeur.param("TYPE")
                    suffixe = f" ({type_.lower()})" if type_ else ""
                    lignes.append((etiquette + suffixe, texte))
        if card.uid:
            lignes.append(("Identifiant", card.uid))
    lignes.append(("ETag", row["etag"]))

    definitions = "".join(
        f"<dt>{escape(etiquette)}</dt><dd>{escape(valeur)}</dd>" for etiquette, valeur in lignes
    )
    retour = f"{base}/view/contacts/{quote(user)}/{quote(nom)}/"

    corps = (
        titre(
            f"<h1>{escape(titre_fiche)}</h1>",
            f"<a href='{retour}'>← retour au carnet</a>",
        )
        + f"<main><section class=detail><h2>{escape(titre_fiche)}</h2><dl>{definitions}</dl>"
        f"<pre>{escape(row['data'])}</pre></section></main>"
        "<footer>Contenu affiché tel qu'il est stocké : Kalendra ne réécrit jamais "
        "une carte de visite.</footer>"
    )
    return _page(titre_fiche, corps, base, admin, connecte)


def rendre_objet(
    config, calendar_row, user: str, row, tz, admin: bool = False, connecte: str = ""
) -> Response:
    base = config.base_path
    nom = calendar_row["name"]
    try:
        composants = [
            c for c in parse_calendar(row["data"]).children if c.name in {"VEVENT", "VTODO"}
        ]
    except Exception:
        composants = []

    principal = composants[0] if composants else None

    def champ(nom_prop: str) -> str:
        if principal is None:
            return ""
        prop = principal.get(nom_prop)
        return prop.text if prop is not None else ""

    resume = champ("SUMMARY") or "(sans titre)"
    journee = bool(principal is not None and component_window(principal)[2])

    def afficher(valeur: int | None) -> datetime | None:
        if valeur is None:
            return None
        return from_unix(valeur) if journee else _local(valeur, tz)

    debut = afficher(row["dtstart"])
    fin = afficher(row["dtend"])

    if journee and debut is not None:
        dernier = from_unix(max((row["dtend"] or row["dtstart"]) - 1, row["dtstart"]))
        quand = (
            f"le {debut:%d/%m/%Y} (journée entière)"
            if dernier.date() == debut.date()
            else f"du {debut:%d/%m/%Y} au {dernier:%d/%m/%Y} (journées entières)"
        )
    elif debut is None:
        quand = "date inconnue"
    elif fin is None:
        quand = f"à partir du {debut:%d/%m/%Y %H:%M}"
    elif debut.date() == fin.date():
        quand = f"le {debut:%d/%m/%Y} de {debut:%H:%M} à {fin:%H:%M}"
    else:
        quand = f"du {debut:%d/%m/%Y %H:%M} au {fin:%d/%m/%Y %H:%M}"

    lignes = [("Quand", quand)]
    for etiquette, prop in (
        ("Lieu", "LOCATION"),
        ("Description", "DESCRIPTION"),
        ("Organisateur", "ORGANIZER"),
        ("Statut", "STATUS"),
    ):
        valeur = champ(prop)
        if valeur:
            lignes.append((etiquette, valeur))
    if principal is not None and principal.get("RRULE") is not None:
        lignes.append(("Récurrence", principal.get("RRULE").value))
    lignes.append(("Identifiant", row["uid"]))
    lignes.append(("ETag", row["etag"]))

    definitions = "".join(
        f"<dt>{escape(etiquette)}</dt><dd>{escape(str(valeur))}</dd>"
        for etiquette, valeur in lignes
    )
    retour = _lien_mois(
        base,
        user,
        nom,
        (debut or datetime.now(UTC)).year,
        (debut or datetime.now(UTC)).month,
    )

    corps = (
        titre(
            f"<h1>{escape(resume)}</h1>",
            f"<a href='{retour}'>← retour au mois</a>",
        )
        + f"<main><section class=detail><h2>{escape(resume)}</h2><dl>{definitions}</dl>"
        f"<pre>{escape(row['data'])}</pre></section></main>"
        "<footer>Contenu affiché tel qu'il est stocké : Kalendra ne réécrit jamais "
        "un objet calendrier.</footer>"
    )
    return _page(resume, corps, base, admin, connecte)


# ------------------------------------------------------------------ routage


def _importer(
    db, config, request: Request, user_row, admin: bool, segments: list[str], token: str
) -> Response:
    """Receive an uploaded .ics and store it into the target calendar.

    Every user imports into their own calendars; an administrator may do so for
    any account, as everywhere else in this view.
    """
    resource = resolve(db, ["calendars", *segments], "/" + "/".join(segments))
    if resource.kind != Kind.CALENDAR or resource.calendar is None:
        return error(404, "Agenda introuvable.")
    if not admin and resource.user["id"] != user_row["id"]:
        return error(403, "Cet agenda ne vous appartient pas.")

    champs = parse_multipart(request.body, request.header("content-type"))
    fourni = champs.get("csrf", b"").decode("utf-8", "replace")
    if not csrf_valid(db.secret_key(), user_row["username"], fourni):
        return error(403, "Jeton CSRF invalide.")

    brut = champs.get("fichier", b"")
    if not brut:
        return _rediriger(config, segments, "Aucun fichier reçu.")

    rapport = importer(
        db,
        resource.calendar,
        brut.decode("utf-8", "replace"),
        max_taille=config.max_resource_size,
        quota=config.quota_bytes,
    )
    message = rapport.resume()
    if rapport.erreurs:
        # Only the first few are shown: a broken file would produce hundreds,
        # unreadable in a banner.
        apercu = " ".join(rapport.erreurs[:3])
        reste = len(rapport.erreurs) - 3
        message += f" ({apercu}" + (f" … +{reste}" if reste > 0 else "") + ")"
    return _rediriger(config, segments, message)


#: Same character set as `SAFE_HREF` on the DAV side, but stricter: these names
#: become a CalDAV URL segment, and the form's HTML pattern protects nothing
#: against a direct POST.
NOM_AGENDA = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _champs_formulaire(request: Request) -> tuple[dict[str, str], bytes]:
    """Fields of a form, whatever its encoding.

    Returns the text fields and, separately, any uploaded file: that one stays
    as bytes, since an .ics must be stored exactly as it was uploaded.
    """
    type_contenu = request.header("content-type")
    if type_contenu.split(";")[0].strip().lower() == "multipart/form-data":
        brut = parse_multipart(request.body, type_contenu)
        fichier = brut.pop("fichier", b"")
        return {c: v.decode("utf-8", "replace") for c, v in brut.items()}, fichier
    champs = {
        cle: valeurs[0] for cle, valeurs in parse_qs(request.text, keep_blank_values=True).items()
    }
    return champs, b""


def _creer_agenda(db, config, request: Request, user_row, admin: bool, token: str) -> Response:
    """Calendar creation by its owner, from `/view/`."""
    champs, fichier = _champs_formulaire(request)
    if not csrf_valid(db.secret_key(), user_row["username"], champs.get("csrf", "")):
        return error(403, "Jeton CSRF invalide.")

    # An administrator may create for others; an ordinary account only for
    # itself — the same rule as everywhere in this view.
    cible = champs.get("proprietaire", "").strip() or user_row["username"]
    if cible != user_row["username"] and not admin:
        return _rediriger_index(config, "Vous ne pouvez créer un agenda que pour vous-même.")
    proprietaire = db.get_user(cible)
    if proprietaire is None:
        return _rediriger_index(config, "Compte inconnu.")

    nom = champs.get("name", "").strip()
    if not NOM_AGENDA.match(nom):
        return _rediriger_index(
            config, "Nom invalide : lettres, chiffres, point, tiret ou souligné, 64 au plus."
        )
    if db.get_calendar(proprietaire["id"], nom) is not None:
        return _rediriger_index(config, f"Un agenda « {nom} » existe déjà.")

    couleur = champs.get("color", "#3584e4").strip()[:9] or "#3584e4"
    calendar_id = db.create_calendar(
        proprietaire["id"],
        nom,
        display_name=champs.get("display_name", "").strip() or nom,
        color=couleur,
    )

    if not fichier:
        return _rediriger_index(config, f"Agenda « {nom} » créé.")

    # Create then import in one go: that is the usual gesture for an external
    # calendar, which deserves a calendar of its own.
    rapport = importer(
        db,
        db.get_calendar_by_id(calendar_id),
        fichier.decode("utf-8", "replace"),
        max_taille=config.max_resource_size,
        quota=config.quota_bytes,
    )
    if rapport.total == 0:
        # An empty calendar left behind by a failed import has no reason to
        # stay: remove it rather than leave a trace of the failure.
        db.delete_calendar(calendar_id)
        detail = rapport.erreurs[0] if rapport.erreurs else "aucun événement trouvé"
        return _rediriger_index(config, f"Import impossible : {detail}")
    return _rediriger_index(config, f"Agenda « {nom} » créé — {rapport.resume()}")


def _supprimer_agenda(
    db, config, request: Request, user_row, admin: bool, segments: list[str], token: str
) -> Response:
    """Delete a calendar and everything it holds."""
    champs, _ = _champs_formulaire(request)
    if not csrf_valid(db.secret_key(), user_row["username"], champs.get("csrf", "")):
        return error(403, "Jeton CSRF invalide.")

    resource = resolve(db, ["calendars", *segments], "/" + "/".join(segments))
    if resource.kind != Kind.CALENDAR or resource.calendar is None:
        return error(404, "Agenda introuvable.")
    if not admin and resource.user["id"] != user_row["id"]:
        return error(403, "Cet agenda ne vous appartient pas.")

    nom = resource.calendar["display_name"] or resource.calendar["name"]
    compte = db.calendar_stats(resource.calendar["id"])
    db.delete_calendar(resource.calendar["id"])
    return _rediriger_index(config, f"Agenda « {nom} » supprimé ({compte} objet(s) perdus).")


def _telechargement(contenu: str, type_mime: str, nom: str) -> Response:
    """Serve an export as a file to save, named after the collection."""
    corps = contenu.encode("utf-8")
    sur = "".join(c if c.isalnum() or c in "._-" else "_" for c in nom) or "export"
    return Response(
        200,
        corps,
        [
            ("Content-Type", f"{type_mime}; charset=utf-8"),
            ("Content-Length", str(len(corps))),
            ("Content-Disposition", f'attachment; filename="{sur}"'),
            # A snapshot of private data: no shared cache should keep it.
            ("Cache-Control", "private, no-store"),
        ],
    )


def _importer_contacts(
    db, config, request: Request, user_row, admin: bool, segments: list[str]
) -> Response:
    """Receive an uploaded .vcf into one of the account's address books."""
    resource = resolve(db, ["addressbooks", *segments], "/" + "/".join(segments))
    if resource.kind != Kind.CALENDAR or resource.calendar is None:
        return error(404, "Carnet introuvable.")
    if not admin and resource.user["id"] != user_row["id"]:
        return error(403, "Ce carnet ne vous appartient pas.")

    champs, fichier = _champs_formulaire(request)
    if not csrf_valid(db.secret_key(), user_row["username"], champs.get("csrf", "")):
        return error(403, "Jeton CSRF invalide.")

    cible = f"{config.base_path}/view/contacts/{quote(segments[0])}/{quote(segments[1])}/"
    if not fichier:
        message = "Aucun fichier reçu."
    else:
        rapport = importer_cartes(
            db,
            resource.calendar,
            fichier.decode("utf-8", "replace"),
            max_taille=config.max_resource_size,
            quota=config.quota_bytes,
        )
        message = rapport.resume()
        if rapport.erreurs:
            apercu = " ".join(rapport.erreurs[:3])
            reste = len(rapport.erreurs) - 3
            message += f" ({apercu}" + (f" … +{reste}" if reste > 0 else "") + ")"
    return Response(
        303, b"", [("Location", f"{cible}?msg={quote(message)}"), ("Content-Length", "0")]
    )


def _rediriger_index(config, message: str) -> Response:
    cible = f"{config.base_path}/view/?msg={quote(message)}"
    return Response(303, b"", [("Location", cible), ("Content-Length", "0")])


def _rediriger(config, segments: list[str], message: str) -> Response:
    cible = (
        f"{config.base_path}/view/{quote(segments[0])}/{quote(segments[1])}/?msg={quote(message)}"
    )
    return Response(303, b"", [("Location", cible), ("Content-Length", "0")])


def _vue_contacts(
    db, config, request: Request, user_row, admin: bool, segments: list[str], token: str = ""
) -> Response:
    """Routing under `/view/contacts/`: address book, then card."""
    if len(segments) < 2:
        return error(404, "Carnet introuvable.")

    resource = resolve(db, ["addressbooks", *segments], "/" + "/".join(segments))
    if resource.kind not in {Kind.CALENDAR, Kind.OBJECT} or resource.calendar is None:
        return error(404, "Carnet introuvable.")
    if not admin and resource.user["id"] != user_row["id"]:
        return error(403, "Ce carnet ne vous appartient pas.")

    if resource.kind == Kind.OBJECT:
        return rendre_contact(
            config,
            resource.calendar,
            resource.user["username"],
            resource.obj,
            admin,
            user_row["username"],
        )
    if request.query_param("export"):
        return _telechargement(
            export_addressbook(db, resource.calendar),
            "text/vcard",
            f"{resource.calendar['name']}.vcf",
        )
    return rendre_carnet(
        db,
        config,
        resource.calendar,
        resource.user["username"],
        admin,
        user_row["username"],
        token,
        request.query_param("msg"),
    )


def handle_view(db, config, request: Request, segments: list[str], token: str = "") -> Response:
    """Entry point: `segments` excludes the `view` prefix."""
    user_row = request.user
    admin = bool(user_row["is_admin"])
    tz = None  # None = fuseau local du serveur (variable TZ du conteneur)

    # One segment never named anything else (a calendar needs two), so the
    # account page cannot shadow a user called "compte".
    if segments == ["compte"]:
        return handle_account(db, config, request, token)
    if segments == ["changer"]:
        return changer_utilisateur(config, request)

    # Writes, all behind the CSRF token: .ics and .vcf import, calendar
    # creation and deletion, and app passwords on the account page above.
    # Everything else in this view is read-only.
    if request.method == "POST":
        if (
            len(segments) == 4
            and segments[0] == "contacts"
            and segments[3] == "import"
            and db.get_user(segments[1]) is not None
        ):
            return _importer_contacts(db, config, request, user_row, admin, segments[1:3])
        if len(segments) == 3 and segments[2] == "import":
            return _importer(db, config, request, user_row, admin, segments[:2], token)
        if segments == ["agendas", "creer"]:
            return _creer_agenda(db, config, request, user_row, admin, token)
        if len(segments) == 3 and segments[2] == "supprimer":
            return _supprimer_agenda(db, config, request, user_row, admin, segments[:2], token)
        return error(405, "Vue en lecture seule.").header("Allow", "GET, HEAD")

    if request.method not in {"GET", "HEAD"}:
        return error(405, "Vue en lecture seule.").header("Allow", "GET, HEAD, POST")

    # Single segment, like the account page: no calendar is ever named by one.
    if segments == ["recherche"]:
        return rendre_recherche(
            db, config, user_row, admin, request.query_param("q"), user_row["username"]
        )

    if not segments:
        return rendre_index(
            db,
            config,
            user_row,
            admin,
            token,
            request.query_param("msg"),
            user_row["username"],
        )

    # Contacts live under a dedicated prefix: a calendar name and an address
    # book name may be identical, so the URL has to disambiguate. Nothing
    # forbids an account named "contacts", however: this branch is only taken
    # when the next segment names a user, otherwise /view/contacts/<calendar>/
    # would stop reaching that account's calendars.
    if segments[0] == "contacts" and len(segments) > 1 and db.get_user(segments[1]) is not None:
        return _vue_contacts(db, config, request, user_row, admin, segments[1:], token)

    if len(segments) < 2:
        return error(404, "Agenda introuvable.")

    resource = resolve(db, ["calendars", *segments], "/" + "/".join(segments))
    if resource.kind not in {Kind.CALENDAR, Kind.OBJECT} or resource.calendar is None:
        return error(404, "Agenda introuvable.")
    if not admin and resource.user["id"] != user_row["id"]:
        return error(403, "Cet agenda ne vous appartient pas.")

    if resource.kind == Kind.OBJECT:
        return rendre_objet(
            config,
            resource.calendar,
            resource.user["username"],
            resource.obj,
            tz,
            admin,
            user_row["username"],
        )

    if request.query_param("export"):
        return _telechargement(
            export_calendar(db, resource.calendar),
            "text/calendar",
            f"{resource.calendar['name']}.ics",
        )

    aujourdhui = datetime.now(tz).date() if tz else datetime.now().date()
    vue = request.query_param("vue")
    if vue == "taches":
        return rendre_taches(
            db,
            config,
            resource.calendar,
            resource.user["username"],
            tz,
            admin,
            user_row["username"],
        )
    if vue == "semaine":
        semaines = semaine_de(parse_jour(request.query_param("d"), aujourdhui), aujourdhui)
        remplir(db, resource.calendar, semaines, tz)
        return rendre_semaine(
            config,
            resource.calendar,
            resource.user["username"],
            tz,
            semaines[0],
            aujourdhui,
            admin,
            user_row["username"],
        )

    annee, mois = parse_mois(request.query_param("m"), aujourdhui)
    semaines = grille(annee, mois, aujourdhui)
    remplir(db, resource.calendar, semaines, tz)
    if vue == "liste":
        return rendre_liste(
            config,
            resource.calendar,
            resource.user["username"],
            annee,
            mois,
            tz,
            semaines,
            aujourdhui,
            admin,
            user_row["username"],
        )
    return rendre_mois(
        config,
        resource.calendar,
        resource.user["username"],
        annee,
        mois,
        tz,
        semaines,
        aujourdhui,
        admin,
        token,
        request.query_param("msg"),
        user_row["username"],
    )

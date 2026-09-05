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
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from html import escape
from urllib.parse import parse_qs, quote

from .http import Request, Response, error, parse_multipart, text_response
from .ics import (
    Occurrence,
    component_window,
    expand_occurrences,
    from_unix,
    parse_calendar,
    to_unix,
)
from .importics import importer
from .resources import Kind, resolve
from .security import csrf_valid
from .vcard import InvalidCardData, parse_vcard

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
:root { color-scheme: light dark; --bg:#fbfbfd; --fg:#16161d; --muted:#606070;
  --line:#dcdce4; --card:#fff; --accent:#3054c8; --hors:#f2f2f6; --today:#fff8e1; }
@media (prefers-color-scheme: dark) { :root { --bg:#15151a; --fg:#e9e9ef;
  --muted:#a0a0b0; --line:#2c2c36; --card:#1d1d24; --accent:#8aa6ff;
  --hors:#191920; --today:#2a2416; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.5 ui-sans-serif,
  system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
a { color:var(--accent); }
header { padding:18px 24px; border-bottom:1px solid var(--line); display:flex;
  align-items:center; gap:14px; flex-wrap:wrap; }
header h1 { margin:0; font-size:18px; letter-spacing:-.01em; }
header .pastille { width:12px; height:12px; border-radius:3px; display:inline-block; }
header nav { margin-left:auto; display:flex; gap:8px; align-items:center; }
header nav a { text-decoration:none; border:1px solid var(--line); border-radius:8px;
  padding:5px 11px; color:var(--fg); background:var(--card); }
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
.reste { display:block; font-size:12px; color:var(--muted); margin-top:3px; }
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
footer { color:var(--muted); font-size:12.5px; padding:0 24px 32px; }
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


def remplir(
    db, calendar_row, semaines: list[list[Jour]], tz: timezone | None
) -> None:
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
        for occurrence in expand_occurrences(
            row["data"], borne_debut, borne_fin, href=row["href"]
        ):
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


def _page(titre: str, corps: str) -> Response:
    html = (
        "<!doctype html><html lang=fr><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width, initial-scale=1">'
        f"<title>{escape(titre)}</title><style>{STYLE}</style></head><body>{corps}</body></html>"
    )
    return text_response(200, html, "text/html; charset=utf-8")


def _lien_admin(base: str, admin: bool) -> str:
    """Link back to the admin UI, for administrators only.

    A plain user would get a 403 on /admin: better not to show them the door
    than to let them walk into it.
    """
    return f"<a href='{base}/admin'>administration</a>" if admin else ""


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
) -> Response:
    base = config.base_path
    pa, pm = mois_precedent(annee, mois)
    sa, sm = mois_suivant(annee, mois)
    nom = calendar_row["name"]
    titre = calendar_row["display_name"] or nom

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
                puces += f"<span class=reste>+{reste} autre{'s' if reste > 1 else ''}</span>"
            attribut = f" class='{' '.join(classes)}'" if classes else ""
            cellules.append(
                f"<td{attribut}><div class=numero>{jour.jour.day}</div>{puces}</td>"
            )
        lignes.append("<tr>" + "".join(cellules) + "</tr>")

    entetes = "".join(f"<th>{j[:3]}</th>" for j in JOURS)
    total = sum(len(jour.evenements) for semaine in semaines for jour in semaine)

    corps = (
        "<header>"
        "<span class=pastille style='background:"
        f"{escape(calendar_row['color'] or '#3584e4')}'></span>"
        f"<h1>{escape(titre)}</h1>"
        f"<span class=mois>{MOIS[mois - 1]} {annee}</span>"
        "<nav>"
        f"<a href='{_lien_mois(base, user, nom, pa, pm)}'>← précédent</a>"
        f"<a href='{_lien_mois(base, user, nom, aujourdhui.year, aujourdhui.month)}'>"
        "aujourd'hui</a>"
        f"<a href='{_lien_mois(base, user, nom, sa, sm)}'>suivant →</a>"
        f"<a href='{base}/view/'>agendas</a>"
        f"{_lien_admin(base, admin)}"
        "</nav></header>"
        + (f"<div class=flash>{escape(message)}</div>" if message else "")
        + f"<main><table class=grille><tr>{entetes}</tr>{''.join(lignes)}</table>"
        + _formulaire_import(base, user, nom, token)
        + "</main>"
        f"<footer>{total} occurrence{'s' if total > 1 else ''} affichée"
        f"{'s' if total > 1 else ''} · vue en lecture seule : "
        "pour créer ou modifier un événement, utilisez votre client CalDAV.</footer>"
    )
    return _page(f"{titre} — {MOIS[mois - 1]} {annee}", corps)


def rendre_index(
    db, config, user_row, admin: bool, token: str = "", message: str = ""
) -> Response:
    base = config.base_path
    if admin:
        agendas = [(row["username"], row) for row in db.list_all_calendars()]
    else:
        agendas = [(user_row["username"], row) for row in db.list_calendars(user_row["id"])]

    elements = []
    for proprietaire, row in agendas:
        compte = db.calendar_stats(row["id"])
        cible = f"{base}/view/{quote(proprietaire)}/{quote(row['name'])}/"
        elements.append(
            "<li>"
            f"<span class=pastille style='background:{escape(row['color'] or '#3584e4')};"
            "width:12px;height:12px;border-radius:3px;display:inline-block'></span>"
            f"<a href='{cible}'>{escape(row['display_name'] or row['name'])}</a>"
            f"<span style='color:var(--muted);font-size:13px'>{proprietaire} · "
            f"{compte} objet{'s' if compte > 1 else ''}</span></li>"
        )

    if admin:
        carnets = [(row["username"], row) for row in db.list_all_addressbooks()]
    else:
        carnets = [(user_row["username"], row) for row in db.list_addressbooks(user_row["id"])]

    fiches = []
    for proprietaire, row in carnets:
        compte = db.calendar_stats(row["id"])
        cible = f"{base}/view/contacts/{quote(proprietaire)}/{quote(row['name'])}/"
        fiches.append(
            "<li>"
            f"<span class=pastille style='background:{escape(row['color'] or '#3584e4')};"
            "width:12px;height:12px;border-radius:3px;display:inline-block'></span>"
            f"<a href='{cible}'>{escape(row['display_name'] or row['name'])}</a>"
            f"<span style='color:var(--muted);font-size:13px'>{proprietaire} · "
            f"{compte} contact{'s' if compte > 1 else ''}</span></li>"
        )

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
        f"<header><h1>Agendas</h1><nav>{_lien_admin(base, admin)}</nav></header>"
        + (f"<div class=flash>{escape(message)}</div>" if message else "")
        + "<main><ul class=agendas>"
        + ("".join(elements) or "<li class=vide>Aucun agenda.</li>")
        + "</ul>"
        + creation
        + "<h1 style='font-size:18px;margin:26px 0 12px'>Carnets d'adresses</h1>"
        + "<ul class=agendas>"
        + ("".join(fiches) or "<li class=vide>Aucun carnet.</li>")
        + "</ul></main>"
    )
    return _page("Kalendra — agendas et carnets", corps)


def rendre_carnet(db, config, carnet_row, user: str, admin: bool = False) -> Response:
    """List an address book's cards, sorted by display name."""
    base = config.base_path
    nom = carnet_row["name"]
    titre = carnet_row["display_name"] or nom

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

    corps = (
        "<header>"
        "<span class=pastille style='background:"
        f"{escape(carnet_row['color'] or '#3584e4')}'></span>"
        f"<h1>{escape(titre)}</h1>"
        f"<nav><a href='{base}/view/'>agendas</a>"
        f"{_lien_admin(base, admin)}</nav></header>"
        "<main><table class=grille style='table-layout:auto'>"
        "<tr><th>Nom</th><th>Courriel</th></tr>"
        + (
            "".join(lignes)
            or "<tr><td colspan=2 class=vide>Ce carnet est vide.</td></tr>"
        )
        + "</table></main>"
        f"<footer>{len(lignes)} contact{'s' if len(lignes) > 1 else ''} · "
        "vue en lecture seule : pour modifier une fiche, utilisez votre client "
        "CardDAV.</footer>"
    )
    return _page(f"{titre} — contacts", corps)


def rendre_contact(config, carnet_row, user: str, row, admin: bool = False) -> Response:
    """One card in detail: the common properties, then the raw source."""
    base = config.base_path
    nom = carnet_row["name"]
    try:
        card = parse_vcard(row["data"])
    except InvalidCardData:
        card = None

    titre = (card.fn if card and card.fn else "") or row["summary"] or "(sans nom)"

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
        f"<header><h1>{escape(titre)}</h1>"
        f"<nav><a href='{retour}'>← retour au carnet</a>"
        f"<a href='{base}/view/'>agendas</a>"
        f"{_lien_admin(base, admin)}</nav></header>"
        f"<main><section class=detail><h2>{escape(titre)}</h2><dl>{definitions}</dl>"
        f"<pre>{escape(row['data'])}</pre></section></main>"
        "<footer>Contenu affiché tel qu'il est stocké : Kalendra ne réécrit jamais "
        "une carte de visite.</footer>"
    )
    return _page(titre, corps)


def rendre_objet(config, calendar_row, user: str, row, tz, admin: bool = False) -> Response:
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
        base, user, nom, (debut or datetime.now(UTC)).year,
        (debut or datetime.now(UTC)).month,
    )

    corps = (
        f"<header><h1>{escape(resume)}</h1>"
        f"<nav><a href='{retour}'>← retour au mois</a>"
        f"<a href='{base}/view/'>agendas</a>"
        f"{_lien_admin(base, admin)}</nav></header>"
        f"<main><section class=detail><h2>{escape(resume)}</h2><dl>{definitions}</dl>"
        f"<pre>{escape(row['data'])}</pre></section></main>"
        "<footer>Contenu affiché tel qu'il est stocké : Kalendra ne réécrit jamais "
        "un objet calendrier.</footer>"
    )
    return _page(resume, corps)


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
        cle: valeurs[0]
        for cle, valeurs in parse_qs(request.text, keep_blank_values=True).items()
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
    return _rediriger_index(
        config, f"Agenda « {nom} » supprimé ({compte} objet(s) perdus)."
    )


def _rediriger_index(config, message: str) -> Response:
    cible = f"{config.base_path}/view/?msg={quote(message)}"
    return Response(303, b"", [("Location", cible), ("Content-Length", "0")])


def _rediriger(config, segments: list[str], message: str) -> Response:
    cible = (
        f"{config.base_path}/view/{quote(segments[0])}/{quote(segments[1])}/"
        f"?msg={quote(message)}"
    )
    return Response(303, b"", [("Location", cible), ("Content-Length", "0")])


def _vue_contacts(db, config, user_row, admin: bool, segments: list[str]) -> Response:
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
            config, resource.calendar, resource.user["username"], resource.obj, admin
        )
    return rendre_carnet(
        db, config, resource.calendar, resource.user["username"], admin
    )


def handle_view(
    db, config, request: Request, segments: list[str], token: str = ""
) -> Response:
    """Entry point: `segments` excludes the `view` prefix."""
    user_row = request.user
    admin = bool(user_row["is_admin"])
    tz = None  # None = fuseau local du serveur (variable TZ du conteneur)

    # Three actions write: import, calendar creation and calendar deletion.
    # Everything else in this view is read-only.
    if request.method == "POST":
        if len(segments) == 3 and segments[2] == "import":
            return _importer(db, config, request, user_row, admin, segments[:2], token)
        if segments == ["agendas", "creer"]:
            return _creer_agenda(db, config, request, user_row, admin, token)
        if len(segments) == 3 and segments[2] == "supprimer":
            return _supprimer_agenda(
                db, config, request, user_row, admin, segments[:2], token
            )
        return error(405, "Vue en lecture seule.").header("Allow", "GET, HEAD")

    if request.method not in {"GET", "HEAD"}:
        return error(405, "Vue en lecture seule.").header("Allow", "GET, HEAD, POST")

    if not segments:
        return rendre_index(
            db, config, user_row, admin, token, request.query_param("msg")
        )

    # Contacts live under a dedicated prefix: a calendar name and an address
    # book name may be identical, so the URL has to disambiguate. Nothing
    # forbids an account named "contacts", however: this branch is only taken
    # when the next segment names a user, otherwise /view/contacts/<calendar>/
    # would stop reaching that account's calendars.
    if segments[0] == "contacts" and len(segments) > 1 and db.get_user(segments[1]) is not None:
        return _vue_contacts(db, config, user_row, admin, segments[1:])

    if len(segments) < 2:
        return error(404, "Agenda introuvable.")

    resource = resolve(db, ["calendars", *segments], "/" + "/".join(segments))
    if resource.kind not in {Kind.CALENDAR, Kind.OBJECT} or resource.calendar is None:
        return error(404, "Agenda introuvable.")
    if not admin and resource.user["id"] != user_row["id"]:
        return error(403, "Cet agenda ne vous appartient pas.")

    if resource.kind == Kind.OBJECT:
        return rendre_objet(
            config, resource.calendar, resource.user["username"], resource.obj, tz, admin
        )

    aujourdhui = datetime.now(tz).date() if tz else datetime.now().date()
    annee, mois = parse_mois(request.query_param("m"), aujourdhui)
    semaines = grille(annee, mois, aujourdhui)
    remplir(db, resource.calendar, semaines, tz)
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
    )

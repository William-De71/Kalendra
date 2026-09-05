"""Vue mensuelle en lecture seule.

Consulter son agenda depuis un navigateur, sans passer par un client. C'est
volontairement une vue, pas un éditeur : voir la note « Périmètre volontairement
exclu » dans CLAUDE.md. Aucune écriture n'est possible depuis ces pages, et
aucun objet calendrier n'est réécrit.

Le rendu est entièrement fait côté serveur, sans JavaScript ni dépendance :
un tableau HTML de sept colonnes, des liens pour naviguer entre les mois.
"""

from __future__ import annotations

import calendar as _calendar
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from html import escape
from urllib.parse import quote

from .http import Request, Response, error, text_response
from .ics import (
    Occurrence,
    component_window,
    expand_occurrences,
    from_unix,
    parse_calendar,
    to_unix,
)
from .resources import Kind, resolve

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

#: Nombre d'événements affichés dans une case avant le repli « +N autres ».
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
    """Décode le paramètre `m=AAAA-MM`, en repliant sur le mois par défaut."""
    try:
        annee, _, mois = raw.partition("-")
        annee_i, mois_i = int(annee), int(mois)
    except (ValueError, AttributeError):
        return defaut.year, defaut.month
    if not (1 <= mois_i <= 12) or not (1 <= annee_i <= 9999):
        return defaut.year, defaut.month
    return annee_i, mois_i


def grille(annee: int, mois: int, aujourdhui: date) -> list[list[Jour]]:
    """Semaines du mois, commençant le lundi, débords inclus."""
    cal = _calendar.Calendar(firstweekday=0)
    return [
        [
            Jour(jour=j, dans_le_mois=(j.month == mois), aujourdhui=(j == aujourdhui))
            for j in semaine
        ]
        for semaine in cal.monthdatescalendar(annee, mois)
    ]


def _local(moment_unix: int, tz: timezone | None) -> datetime:
    """Convertit un instant UTC vers le fuseau d'affichage du serveur."""
    moment = from_unix(moment_unix)
    return moment.astimezone(tz) if tz is not None else moment.astimezone()


def remplir(
    db, calendar_row, semaines: list[list[Jour]], tz: timezone | None
) -> None:
    """Place les occurrences dans les cases de la grille.

    La fenêtre interrogée déborde d'un jour de chaque côté : un événement
    peut commencer la veille en UTC et tomber dans la grille une fois converti
    dans le fuseau d'affichage.
    """
    if not semaines:
        return
    debut = semaines[0][0].jour - timedelta(days=1)
    fin = semaines[-1][-1].jour + timedelta(days=2)
    borne_debut = to_unix(datetime(debut.year, debut.month, debut.day, tzinfo=timezone.utc))
    borne_fin = to_unix(datetime(fin.year, fin.month, fin.day, tzinfo=timezone.utc))

    cases = {jour.jour: jour for semaine in semaines for jour in semaine}

    for row in db.query_objects(
        calendar_row["id"], components=["VEVENT", "VTODO"], start=borne_debut, end=borne_fin
    ):
        for occurrence in expand_occurrences(
            row["data"], borne_debut, borne_fin, href=row["href"]
        ):
            # Une occurrence sans durée occupe malgré tout son jour de début ;
            # DTEND étant exclusif, on recule d'une seconde pour la dernière case.
            dernier = max(occurrence.end - 1, occurrence.start)
            if occurrence.all_day:
                # Une journée entière n'a pas d'heure : la convertir vers un
                # fuseau à l'ouest de Greenwich la ferait basculer la veille.
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


def rendre_mois(
    config,
    calendar_row,
    user: str,
    annee: int,
    mois: int,
    tz,
    semaines: list[list[Jour]],
    aujourdhui: date,
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
        f"<span class=pastille style='background:{escape(calendar_row['color'] or '#3584e4')}'></span>"
        f"<h1>{escape(titre)}</h1>"
        f"<span class=mois>{MOIS[mois - 1]} {annee}</span>"
        "<nav>"
        f"<a href='{_lien_mois(base, user, nom, pa, pm)}'>← précédent</a>"
        f"<a href='{_lien_mois(base, user, nom, aujourdhui.year, aujourdhui.month)}'>aujourd'hui</a>"
        f"<a href='{_lien_mois(base, user, nom, sa, sm)}'>suivant →</a>"
        f"<a href='{base}/view/'>agendas</a>"
        "</nav></header>"
        f"<main><table class=grille><tr>{entetes}</tr>{''.join(lignes)}</table></main>"
        f"<footer>{total} occurrence{'s' if total > 1 else ''} affichée"
        f"{'s' if total > 1 else ''} · vue en lecture seule : "
        "pour créer ou modifier un événement, utilisez votre client CalDAV.</footer>"
    )
    return _page(f"{titre} — {MOIS[mois - 1]} {annee}", corps)


def rendre_index(db, config, user_row, admin: bool) -> Response:
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

    corps = (
        "<header><h1>Agendas</h1></header><main><ul class=agendas>"
        + ("".join(elements) or "<li class=vide>Aucun agenda.</li>")
        + "</ul></main>"
    )
    return _page("Kalendra — agendas", corps)


def rendre_objet(config, calendar_row, user: str, row, tz) -> Response:
    base = config.base_path
    nom = calendar_row["name"]
    try:
        composants = [
            c for c in parse_calendar(row["data"]).children if c.name in {"VEVENT", "VTODO"}
        ]
    except Exception:  # noqa: BLE001 - un objet illisible reste consultable en brut
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
        base, user, nom, (debut or datetime.now(timezone.utc)).year,
        (debut or datetime.now(timezone.utc)).month,
    )

    corps = (
        f"<header><h1>{escape(resume)}</h1>"
        f"<nav><a href='{retour}'>← retour au mois</a></nav></header>"
        f"<main><section class=detail><h2>{escape(resume)}</h2><dl>{definitions}</dl>"
        f"<pre>{escape(row['data'])}</pre></section></main>"
        "<footer>Contenu affiché tel qu'il est stocké : Kalendra ne réécrit jamais "
        "un objet calendrier.</footer>"
    )
    return _page(resume, corps)


# ------------------------------------------------------------------ routage


def handle_view(db, config, request: Request, segments: list[str]) -> Response:
    """Point d'entrée : `segments` exclut le préfixe `view`."""
    if request.method not in {"GET", "HEAD"}:
        return error(405, "Vue en lecture seule.").header("Allow", "GET, HEAD")

    user_row = request.user
    admin = bool(user_row["is_admin"])
    tz = None  # None = fuseau local du serveur (variable TZ du conteneur)

    if not segments:
        return rendre_index(db, config, user_row, admin)

    if len(segments) < 2:
        return error(404, "Agenda introuvable.")

    resource = resolve(db, ["calendars", *segments], "/" + "/".join(segments))
    if resource.kind not in {Kind.CALENDAR, Kind.OBJECT} or resource.calendar is None:
        return error(404, "Agenda introuvable.")
    if not admin and resource.user["id"] != user_row["id"]:
        return error(403, "Cet agenda ne vous appartient pas.")

    if resource.kind == Kind.OBJECT:
        return rendre_objet(config, resource.calendar, resource.user["username"], resource.obj, tz)

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
    )

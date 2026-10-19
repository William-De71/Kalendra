"""Whole-collection export: one `.ics` per calendar, one `.vcf` per address book.

The counterpart of the import. Objects are copied as stored — the calendar
through the same aggregation as the public feed, the cards end to end, which
RFC 6350 §3.1 allows in a single file — so an export re-imported into Kalendra
or anywhere else loses nothing the client once uploaded.
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import UTC, datetime

from . import __version__
from .ics import build_feed

PRODID = f"-//Kalendra//Kalendra {__version__}//FR"


def export_calendar(db, calendar_row) -> str:
    return build_feed(
        name=calendar_row["display_name"] or calendar_row["name"],
        description=calendar_row["description"],
        color=calendar_row["color"],
        objects=[row["data"] for row in db.list_objects(calendar_row["id"])],
        refresh_minutes=None,
        prodid=PRODID,
    )


def export_addressbook(db, addressbook_row) -> str:
    cartes = []
    for row in db.list_objects(addressbook_row["id"]):
        # Line endings are the only thing normalised: a card stored with bare
        # LFs would otherwise glue its END:VCARD to the next BEGIN:VCARD.
        texte = row["data"].replace("\r\n", "\n").replace("\r", "\n").strip("\n")
        cartes.append(texte.replace("\n", "\r\n") + "\r\n")
    return "".join(cartes)


def _nom_fichier(nom: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", nom) or "sans-nom"


def export_account(db, user_row) -> bytes:
    """Every calendar and address book of an account, in one .zip.

    Deflate from the standard library: a family's worth of events compresses
    tenfold, and the archive opens anywhere without Kalendra. Collection
    names already obey `SAFE_HREF`, but they are filtered again here: they
    become paths inside the archive.
    """
    tampon = io.BytesIO()
    horodatage = datetime.now(UTC).timetuple()[:6]
    with zipfile.ZipFile(tampon, "w", compression=zipfile.ZIP_DEFLATED) as archive:

        def ajouter(chemin: str, contenu: str) -> None:
            info = zipfile.ZipInfo(chemin, date_time=horodatage)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, contenu.encode("utf-8"))

        agendas = db.list_calendars(user_row["id"])
        carnets = db.list_addressbooks(user_row["id"])
        for row in agendas:
            ajouter(f"agendas/{_nom_fichier(row['name'])}.ics", export_calendar(db, row))
        for row in carnets:
            ajouter(f"contacts/{_nom_fichier(row['name'])}.vcf", export_addressbook(db, row))
        ajouter(
            "LISEZMOI.txt",
            f"Export du compte « {user_row['username']} », Kalendra {__version__}, "
            f"{datetime.now(UTC):%Y-%m-%d %H:%M} UTC.\n\n"
            f"agendas/  : {len(agendas)} agenda(s), un fichier .ics chacun.\n"
            f"contacts/ : {len(carnets)} carnet(s), un fichier .vcf chacun.\n\n"
            "Les objets sont recopiés tels qu'ils ont été déposés. Chaque fichier "
            "s'importe dans Kalendra (page de l'agenda ou du carnet) comme dans "
            "n'importe quel logiciel d'agenda ou de contacts.\n",
        )
    return tampon.getvalue()

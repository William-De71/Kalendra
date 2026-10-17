"""Whole-collection export: one `.ics` per calendar, one `.vcf` per address book.

The counterpart of the import. Objects are copied as stored — the calendar
through the same aggregation as the public feed, the cards end to end, which
RFC 6350 §3.1 allows in a single file — so an export re-imported into Kalendra
or anywhere else loses nothing the client once uploaded.
"""

from __future__ import annotations

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

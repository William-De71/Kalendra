"""Mutation fuzzing of everything that reads client-supplied text.

No dependency: a seeded `random.Random` mutates realistic objects (deleted,
duplicated or swapped lines, stray separators, truncation, hostile recurrence
rules) and every entry point must either succeed or raise its own documented
exception. Anything else would surface as a 500 to a CalDAV client, or abort a
whole import over one bad file.

The seed is fixed so a failure reproduces; KALENDRA_FUZZ_SEED and
KALENDRA_FUZZ_ROUNDS widen the search locally:

    KALENDRA_FUZZ_ROUNDS=20000 KALENDRA_FUZZ_SEED=7 \\
        python -m unittest discover -s tests -t tests -p test_fuzz.py
"""

from __future__ import annotations

import contextlib
import os
import random
import unittest
from datetime import UTC, datetime

from helpers import ServerTestCase, make_card
from kalendra.ics import (
    InvalidCalendarData,
    build_feed,
    expand_calendar,
    expand_occurrences,
    limit_recurrence_set,
    overlaps_range,
    parse_object,
    split_calendar,
    text_matches,
)
from kalendra.importics import importer
from kalendra.importvcf import importer_cartes, split_vcards
from kalendra.rrule import UnsupportedRule, iter_occurrences, parse_rrule
from kalendra.vcard import InvalidCardData, parse_vcard
from test_caldav import SERIE
from test_feed import RICHE

SEED = int(os.environ.get("KALENDRA_FUZZ_SEED", "20261017"))
ROUNDS = int(os.environ.get("KALENDRA_FUZZ_ROUNDS", "250"))

GRAINES_ICS = [
    RICHE.format(uid="riche"),
    SERIE,
    (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VTODO\r\nUID:t\r\nDTSTAMP:20260101T000000Z\r\n"
        "DUE;VALUE=DATE:20260320\r\nRRULE:FREQ=MONTHLY;BYDAY=-1FR;COUNT=5\r\n"
        "SUMMARY:Rapport\r\nEND:VTODO\r\nEND:VCALENDAR\r\n"
    ),
    (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:j\r\n"
        "DTSTART;VALUE=DATE:20240229\r\nDURATION:P1D\r\nRRULE:FREQ=YEARLY;BYWEEKNO=9;BYDAY=TH\r\n"
        "RDATE:20260101T120000Z,20260202T120000Z/PT1H\r\nEXDATE:20250101\r\n"
        "SUMMARY:Anniversaire\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    ),
]

GRAINES_VCF = [
    make_card(uid="a", fn="Jean Dupont").decode(),
    (
        "BEGIN:VCARD\r\nVERSION:4.0\r\nN:Curie;Marie;;;\r\nTEL;TYPE=cell:+33 6 00 00 00 00\r\n"
        "ADR;TYPE=home:;;1 rue X;Paris;;75005;France\r\nNOTE:ligne\\, pliée\r\n sur deux\r\n"
        "END:VCARD\r\n"
    ),
]

#: Fragments that tend to break hand-written parsers.
EPICES = [
    ":",
    ";",
    "=",
    ",",
    '"',
    "\\",
    "\r\n",
    "\n",
    "\r",
    " ",
    "\t",
    "\x00",
    "é",
    "☕",
    "\ufeff",
    "BEGIN:VEVENT",
    "END:VEVENT",
    "BEGIN:VALARM",
    "END:VCALENDAR",
    "BEGIN:VCARD",
    "END:VCARD",
    "TZID=",
    "TZID=Nowhere/Land",
    'TZID="/a/b/c"',
    "VALUE=DATE",
    "Z",
    "T",
    "P",
    "-P1W",
    "RRULE:",
    "FREQ=",
    "COUNT=",
    "UNTIL=",
    "BYDAY=",
    "BYSETPOS=",
    "99999999T999999Z",
    "00000000",
    "20261301",
    "20260230T250000",
    "-1",
    "0",
    "53",
    "-53",
    "367",
]

#: Rule parts mixed into RRULE values, valid or not.
MORCEAUX_RRULE = [
    "FREQ=SECONDLY",
    "FREQ=MINUTELY",
    "FREQ=HOURLY",
    "FREQ=DAILY",
    "FREQ=WEEKLY",
    "FREQ=MONTHLY",
    "FREQ=YEARLY",
    "FREQ=",
    "FREQ=NEVER",
    "INTERVAL=0",
    "INTERVAL=-3",
    "INTERVAL=7",
    "COUNT=0",
    "COUNT=-1",
    "COUNT=3",
    "UNTIL=20261231T235959Z",
    "UNTIL=20260101",
    "UNTIL=garbage",
    "BYDAY=MO,WE",
    "BYDAY=1MO",
    "BYDAY=-1SU",
    "BYDAY=99XX",
    "BYDAY=+2TU",
    "BYDAY=",
    "BYMONTH=0",
    "BYMONTH=13",
    "BYMONTH=2",
    "BYMONTHDAY=0",
    "BYMONTHDAY=-31",
    "BYMONTHDAY=31",
    "BYMONTHDAY=32",
    "BYYEARDAY=0",
    "BYYEARDAY=-366",
    "BYYEARDAY=400",
    "BYWEEKNO=0",
    "BYWEEKNO=54",
    "BYWEEKNO=-1",
    "BYHOUR=25",
    "BYHOUR=-1",
    "BYMINUTE=61",
    "BYSETPOS=0",
    "BYSETPOS=-1",
    "BYSETPOS=400",
    "WKST=SU",
    "WKST=XX",
    "BYSECOND=5",
]


def muter(texte: str, rng: random.Random) -> str:
    lignes = texte.split("\r\n")
    for _ in range(rng.randint(1, 4)):
        choix = rng.randrange(8)
        if not lignes:
            lignes = [""]
        i = rng.randrange(len(lignes))
        if choix == 0:
            del lignes[i]
        elif choix == 1:
            lignes.insert(i, lignes[i])
        elif choix == 2:
            j = rng.randrange(len(lignes))
            lignes[i], lignes[j] = lignes[j], lignes[i]
        elif choix == 3:
            ligne = lignes[i]
            k = rng.randint(0, len(ligne))
            lignes[i] = ligne[:k] + rng.choice(EPICES) + ligne[k:]
        elif choix == 4:
            ligne = lignes[i]
            if ligne:
                k = rng.randrange(len(ligne))
                lignes[i] = ligne[:k] + ligne[k + 1 :]
        elif choix == 5:
            regle = ";".join(rng.sample(MORCEAUX_RRULE, rng.randint(1, 4)))
            lignes.insert(i, f"RRULE:{regle}")
        elif choix == 6:
            lignes = lignes[: rng.randint(0, len(lignes))]
        else:
            lignes.insert(i, rng.choice(EPICES) * rng.randint(1, 3))
    return "\r\n".join(lignes)


def _ts(texte: str) -> int:
    return int(datetime.strptime(texte, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC).timestamp())


DEBUT, FIN = _ts("20260101T000000Z"), _ts("20270101T000000Z")


class ParserFuzzTests(unittest.TestCase):
    def test_icalendar(self):
        rng = random.Random(SEED)
        for tour in range(ROUNDS):
            texte = muter(rng.choice(GRAINES_ICS), rng)
            with self.subTest(tour=tour, texte=texte):
                with contextlib.suppress(InvalidCalendarData):
                    parse_object(texte)
                overlaps_range(texte, DEBUT, FIN)
                overlaps_range(texte, None, FIN)
                expand_occurrences(texte, DEBUT, FIN)
                expand_calendar(texte, DEBUT, FIN)
                limit_recurrence_set(texte, DEBUT, FIN)
                text_matches(texte, "VEVENT", "SUMMARY", "a", negate=False)
                split_calendar(texte)
                build_feed(
                    name="x",
                    description="",
                    color="",
                    objects=[texte],
                    refresh_minutes=60,
                    prodid="x",
                )

    def test_vcard(self):
        rng = random.Random(SEED + 1)
        for tour in range(ROUNDS):
            texte = muter(rng.choice(GRAINES_VCF), rng)
            with self.subTest(tour=tour, texte=texte):
                with contextlib.suppress(InvalidCardData):
                    parse_vcard(texte)
                split_vcards(texte)

    def test_regles_de_recurrence(self):
        rng = random.Random(SEED + 2)
        debuts = [
            datetime(2026, 1, 31, 9, tzinfo=UTC),
            datetime(2024, 2, 29, 9),
            datetime(2026, 12, 31, 23, 30, tzinfo=UTC),
        ]
        for tour in range(ROUNDS * 2):
            regle = ";".join(rng.sample(MORCEAUX_RRULE, rng.randint(1, 5)))
            with self.subTest(tour=tour, regle=regle):
                debut = rng.choice(debuts)
                # Same awareness as DTSTART, as `ics.recurrences` guarantees.
                horizon = datetime(2030, 1, 1, tzinfo=debut.tzinfo)
                with contextlib.suppress(UnsupportedRule):
                    for _ in iter_occurrences(debut, parse_rrule(regle), limit=60, horizon=horizon):
                        pass


class ServerFuzzTests(ServerTestCase):
    """Mutated bodies sent through the real dispatch: never a 500."""

    def test_put_icalendar(self):
        rng = random.Random(SEED + 3)
        for tour in range(ROUNDS // 2):
            corps = muter(rng.choice(GRAINES_ICS), rng).encode("utf-8", "surrogatepass")
            with self.subTest(tour=tour, corps=corps):
                result = self.client.put(f"/calendars/will/perso/f{tour}.ics", corps)
                self.assertLess(result.status, 500, result.text)
                self.assertLess(self.client.request("GET", "/view/will/perso/").status, 500)

    def test_put_vcard(self):
        rng = random.Random(SEED + 4)
        self.db.create_addressbook(self.will_id, "amis")
        for tour in range(ROUNDS // 2):
            corps = muter(rng.choice(GRAINES_VCF), rng).encode("utf-8", "surrogatepass")
            with self.subTest(tour=tour, corps=corps):
                result = self.client.request(
                    "PUT",
                    f"/addressbooks/will/amis/f{tour}.vcf",
                    corps,
                    {"Content-Type": "text/vcard"},
                )
                self.assertLess(result.status, 500, result.text)

    def test_corps_xml(self):
        rng = random.Random(SEED + 6)
        graines = [
            (
                '<?xml version="1.0"?><C:calendar-query xmlns:D="DAV:" '
                'xmlns:C="urn:ietf:params:xml:ns:caldav"><D:prop><D:getetag/>'
                '<C:calendar-data><C:expand start="20260101T000000Z" end="20260201T000000Z"/>'
                '</C:calendar-data></D:prop><C:filter><C:comp-filter name="VCALENDAR">'
                '<C:comp-filter name="VEVENT"><C:time-range start="20260101T000000Z"/>'
                '<C:prop-filter name="SUMMARY"><C:text-match>a</C:text-match></C:prop-filter>'
                "</C:comp-filter></C:comp-filter></C:filter></C:calendar-query>"
            ),
            (
                '<?xml version="1.0"?><D:sync-collection xmlns:D="DAV:"><D:sync-token>'
                "urn:kalendra:sync:1:1</D:sync-token><D:prop><D:getetag/></D:prop>"
                "</D:sync-collection>"
            ),
            (
                '<?xml version="1.0"?><D:propfind xmlns:D="DAV:"><D:prop><D:displayname/>'
                "<D:quota-available-bytes/></D:prop></D:propfind>"
            ),
        ]
        self.client.put("/calendars/will/perso/s.ics", SERIE.encode("utf-8"))
        for tour in range(ROUNDS // 2):
            corps = muter(rng.choice(graines), rng).encode("utf-8", "surrogatepass")
            methode = rng.choice(["REPORT", "PROPFIND", "PROPPATCH", "MKCALENDAR"])
            with self.subTest(tour=tour, methode=methode, corps=corps):
                result = self.client.request(
                    methode,
                    "/calendars/will/perso/",
                    corps,
                    {"Content-Type": "application/xml", "Depth": "1"},
                )
                # 501 is the designed answer to an unknown REPORT; 500 is a crash.
                self.assertNotEqual(result.status, 500, result.text)

    def test_imports(self):
        rng = random.Random(SEED + 5)
        calendrier = self.db.get_calendar_by_id(self.perso_id)
        carnet = self.db.get_calendar_by_id(self.db.create_addressbook(self.will_id, "amis"))
        for tour in range(ROUNDS // 5):
            ics = "\r\n".join(muter(rng.choice(GRAINES_ICS), rng) for _ in range(3))
            vcf = "\r\n".join(muter(rng.choice(GRAINES_VCF), rng) for _ in range(3))
            with self.subTest(tour=tour):
                importer(self.db, calendrier, ics, max_taille=10**6)
                importer_cartes(self.db, carnet, vcf, max_taille=10**6)


if __name__ == "__main__":
    unittest.main()

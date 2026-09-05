"""Test d'intégration avec la vraie bibliothèque cliente `caldav`.

Ce module n'est exécuté qu'en intégration continue, contre un serveur déjà
démarré (typiquement le conteneur Docker fraîchement construit) :

    pip install caldav
    KALENDRA_TEST_URL=http://127.0.0.1:5232 \
    KALENDRA_TEST_USER=admin KALENDRA_TEST_PASSWORD=... \
    python -m unittest tests.integration.test_real_client -v

L'intérêt est de valider le serveur avec un client indépendant de notre code,
qui suit le même chemin de découverte qu'Evolution ou Thunderbird.
"""

from __future__ import annotations

import contextlib
import os
import unittest
import uuid
from datetime import UTC, datetime, timedelta

BASE_URL = os.environ.get("KALENDRA_TEST_URL", "")
USERNAME = os.environ.get("KALENDRA_TEST_USER", "admin")
PASSWORD = os.environ.get("KALENDRA_TEST_PASSWORD", "")

EVENT = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Kalendra//integration//FR
BEGIN:VEVENT
UID:{uid}
DTSTAMP:{stamp}
DTSTART:{start}
DTEND:{end}
SUMMARY:{summary}
END:VEVENT
END:VCALENDAR
"""


def _fmt(moment: datetime) -> str:
    return moment.strftime("%Y%m%dT%H%M%SZ")


@unittest.skipUnless(BASE_URL, "KALENDRA_TEST_URL non défini")
class RealClientTests(unittest.TestCase):
    """Parcours complet vu par un client CalDAV tiers."""

    @classmethod
    def setUpClass(cls) -> None:
        import caldav  # importé ici pour que le skip fonctionne sans la dépendance

        cls.client = caldav.DAVClient(url=BASE_URL, username=USERNAME, password=PASSWORD)
        cls.principal = cls.client.principal()
        cls.calendar_name = f"integration-{uuid.uuid4().hex[:8]}"
        cls.calendar = cls.principal.make_calendar(name=cls.calendar_name)

    @classmethod
    def tearDownClass(cls) -> None:
        with contextlib.suppress(Exception):
            cls.calendar.delete()

    def test_decouverte_du_principal(self):
        self.assertTrue(str(self.principal.url).endswith(f"/principals/{USERNAME}/"))

    def test_le_nouvel_agenda_apparait_dans_le_home_set(self):
        names = [str(c.url) for c in self.principal.calendars()]
        self.assertTrue(any(self.calendar_name in name for name in names))

    def test_creation_recherche_et_suppression(self):
        start = datetime(2026, 3, 10, 9, 0, tzinfo=UTC)
        uid = f"int-{uuid.uuid4().hex[:8]}"
        payload = EVENT.format(
            uid=uid,
            stamp=_fmt(datetime.now(UTC)),
            start=_fmt(start),
            end=_fmt(start + timedelta(hours=1)),
            summary="Revue integration",
        )
        event = self.calendar.save_event(payload)
        self.assertIsNotNone(event)

        found = self.calendar.date_search(
            start=start - timedelta(days=1), end=start + timedelta(days=1)
        )
        self.assertTrue(any(uid in item.data for item in found))

        outside = self.calendar.date_search(
            start=start + timedelta(days=30), end=start + timedelta(days=31)
        )
        self.assertFalse(any(uid in item.data for item in outside))

        event.delete()
        remaining = self.calendar.date_search(
            start=start - timedelta(days=1), end=start + timedelta(days=1)
        )
        self.assertFalse(any(uid in item.data for item in remaining))

    def test_evenement_recurrent_visible_hors_premiere_occurrence(self):
        start = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)  # un lundi
        uid = f"rec-{uuid.uuid4().hex[:8]}"
        payload = EVENT.format(
            uid=uid,
            stamp=_fmt(datetime.now(UTC)),
            start=_fmt(start),
            end=_fmt(start + timedelta(minutes=30)),
            summary="Stand-up",
        ).replace("END:VEVENT", "RRULE:FREQ=WEEKLY;BYDAY=MO\nEND:VEVENT")
        self.calendar.save_event(payload)

        far = datetime(2026, 6, 1, tzinfo=UTC)
        found = self.calendar.date_search(start=far, end=far + timedelta(days=7))
        self.assertTrue(any(uid in item.data for item in found))

    def test_les_objets_sont_listables(self):
        self.assertIsInstance(self.calendar.events(), list)


if __name__ == "__main__":
    unittest.main()

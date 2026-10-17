"""The public feed read by an independent iCalendar parser.

Our own unit tests check the feed with rules we wrote ourselves; this one hands
it to the `icalendar` library — installed in CI as a dependency of `caldav` —
the way Google or Proton would consume it. No running server is needed: the
application is driven in-process, so this module only skips when `icalendar`
is missing.

    pip install icalendar
    python -m unittest discover -s tests/integration -t . -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from helpers import ServerTestCase
from test_feed import RICHE

try:
    import icalendar
except ImportError:  # pragma: no cover - depends on the environment
    icalendar = None


@unittest.skipIf(icalendar is None, "bibliothèque icalendar absente")
class FeedConsumerTests(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        for uid in ("r1", "r2"):
            self.client.put(
                f"/calendars/will/perso/{uid}.ics", RICHE.format(uid=uid).encode("utf-8")
            )
        token = self.db.get_calendar_by_id(self.perso_id)["feed_token"]
        self.body = self.anon.request("GET", f"/feed/{token}.ics").body

    def test_le_flux_est_lisible(self):
        calendar = icalendar.Calendar.from_ical(self.body)
        events = list(calendar.walk("VEVENT"))
        self.assertEqual(sorted(str(e["UID"]) for e in events), ["r1", "r2"])
        self.assertEqual(len(list(calendar.walk("VTIMEZONE"))), 1)

    def test_contenu_restitue_sans_perte(self):
        calendar = icalendar.Calendar.from_ical(self.body)
        event = next(iter(calendar.walk("VEVENT")))
        self.assertEqual(str(event["SUMMARY"]), "Réunion de l'équipe — préparation du salon")
        self.assertIn("☕", str(event["DESCRIPTION"]))
        self.assertEqual(len(event.walk("VALARM")), 1)
        start = event.decoded("DTSTART")
        self.assertEqual(str(start.tzinfo), "Europe/Paris")
        self.assertEqual((start.hour, start.minute), (9, 0))

    def test_rien_de_signale_par_le_parseur(self):
        # icalendar collects lines it could not parse instead of raising.
        calendar = icalendar.Calendar.from_ical(self.body)
        for component in calendar.walk():
            self.assertEqual(getattr(component, "errors", []), [], component.name)


if __name__ == "__main__":
    unittest.main()

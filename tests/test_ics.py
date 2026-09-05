"""Tests de l'analyseur iCalendar et de l'expansion des récurrences."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from helpers import ts  # noqa: E402  (ajoute src/ au sys.path)
from kalendra.ics import (
    InvalidCalendarData,
    build_feed,
    fold,
    iter_top_level_blocks,
    overlaps_range,
    parse_calendar,
    parse_content_line,
    parse_datetime_value,
    parse_duration,
    parse_object,
    text_matches,
    to_unix,
    unfold,
)
from kalendra.rrule import iter_occurrences, last_occurrence, parse_rrule

RECURRENT = (
    "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:rec\r\nDTSTART:20260105T090000Z\r\n"
    "DTEND:20260105T100000Z\r\nRRULE:FREQ=WEEKLY;BYDAY=MO\r\n"
    "EXDATE:20260119T090000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
)

SIMPLE = (
    "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:s\r\nDTSTART:20260310T090000Z\r\n"
    "DTEND:20260310T100000Z\r\nSUMMARY:Revue ASIL-D\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
)


class SyntaxTests(unittest.TestCase):
    def test_unfold_rejoint_les_lignes_pliees(self):
        raw = "SUMMARY:Réunion de\r\n  suivi\r\nUID:1\r\n"
        self.assertEqual(unfold(raw), ["SUMMARY:Réunion de suivi", "UID:1"])

    def test_parametres_entre_guillemets(self):
        prop = parse_content_line('DTSTART;TZID="Europe/Paris":20260310T090000')
        self.assertEqual(prop.name, "DTSTART")
        self.assertEqual(prop.param("TZID"), "Europe/Paris")
        self.assertEqual(prop.value, "20260310T090000")

    def test_valeur_text_dechappee(self):
        prop = parse_content_line(r"SUMMARY:Revue\, sprint\; phase\nsuite")
        self.assertEqual(prop.text, "Revue, sprint; phase\nsuite")

    def test_fold_ne_coupe_pas_un_caractere_utf8(self):
        line = "SUMMARY:" + "é" * 80
        folded = fold(line)
        for chunk in folded.split("\r\n "):
            self.assertLessEqual(len(chunk.encode("utf-8")), 76)
        self.assertEqual(folded.replace("\r\n ", ""), line)

    def test_parse_duration(self):
        cases = {"PT1H": 3600, "P1D": 86400, "PT30M": 1800, "P1W": 604800, "-PT15M": -900}
        for raw, seconds in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(parse_duration(raw).total_seconds(), seconds)

    def test_tzid_iana_converti_en_utc(self):
        value = parse_datetime_value("20260310T090000", tzid="Europe/Paris")
        self.assertEqual(to_unix(value), ts("20260310T080000Z"))

    def test_tzid_windows_reconnu(self):
        value = parse_datetime_value("20260310T090000", tzid="Romance Standard Time")
        self.assertEqual(to_unix(value), ts("20260310T080000Z"))

    def test_tzid_prefixe_par_un_chemin(self):
        value = parse_datetime_value(
            "20260310T090000", tzid="/freeassociation.sourceforge.net/Europe/Paris"
        )
        self.assertEqual(to_unix(value), ts("20260310T080000Z"))


class ValidationTests(unittest.TestCase):
    def test_metadonnees_extraites(self):
        meta = parse_object(SIMPLE)
        self.assertEqual(meta.uid, "s")
        self.assertEqual(meta.component, "VEVENT")
        self.assertEqual(meta.start, ts("20260310T090000Z"))
        self.assertEqual(meta.end, ts("20260310T100000Z"))
        self.assertFalse(meta.recurring)
        self.assertEqual(meta.summary, "Revue ASIL-D")

    def test_refus_de_deux_uid(self):
        data = (
            "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:a\r\nDTSTART:20260310T090000Z\r\n"
            "END:VEVENT\r\nBEGIN:VEVENT\r\nUID:b\r\nDTSTART:20260311T090000Z\r\n"
            "END:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        with self.assertRaises(InvalidCalendarData):
            parse_object(data)

    def test_refus_dun_corps_non_calendrier(self):
        with self.assertRaises(InvalidCalendarData):
            parse_object("bonjour")

    def test_refus_dun_composant_non_ferme(self):
        with self.assertRaises(InvalidCalendarData):
            parse_object("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:x\r\nEND:VCALENDAR\r\n")

    def test_journee_entiere_dure_un_jour(self):
        data = (
            "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:j1\r\nDTSTART;VALUE=DATE:20260310\r\n"
            "END:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        meta = parse_object(data)
        self.assertEqual(meta.end - meta.start, 86400)

    def test_duration_remplace_dtend(self):
        data = (
            "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:d1\r\nDTSTART:20260310T090000Z\r\n"
            "DURATION:PT45M\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        meta = parse_object(data)
        self.assertEqual(meta.end - meta.start, 2700)

    def test_recurrence_infinie_sans_borne(self):
        meta = parse_object(RECURRENT)
        self.assertTrue(meta.recurring)
        self.assertIsNone(meta.end)

    def test_recurrence_bornee_par_count(self):
        data = (
            "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:r2\r\nDTSTART:20260105T090000Z\r\n"
            "DTEND:20260105T100000Z\r\nRRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=3\r\n"
            "END:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        self.assertEqual(parse_object(data).end, ts("20260119T100000Z"))


class RecurrenceTests(unittest.TestCase):
    def test_hebdomadaire_multi_jours(self):
        start = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)  # lundi
        rule = parse_rrule("FREQ=WEEKLY;BYDAY=MO,WE;COUNT=4")
        dates = [d.strftime("%Y%m%d") for d in iter_occurrences(start, rule)]
        self.assertEqual(dates, ["20260105", "20260107", "20260112", "20260114"])

    def test_hebdomadaire_avec_intervalle(self):
        start = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
        rule = parse_rrule("FREQ=WEEKLY;INTERVAL=2;BYDAY=MO;COUNT=3")
        dates = [d.strftime("%Y%m%d") for d in iter_occurrences(start, rule)]
        self.assertEqual(dates, ["20260105", "20260119", "20260202"])

    def test_mensuelle_ordinale_negative(self):
        start = datetime(2026, 1, 30, 8, 0, tzinfo=timezone.utc)
        rule = parse_rrule("FREQ=MONTHLY;BYDAY=-1FR;COUNT=3")
        dates = [d.strftime("%Y%m%d") for d in iter_occurrences(start, rule)]
        self.assertEqual(dates, ["20260130", "20260227", "20260327"])

    def test_mensuelle_par_jour_du_mois(self):
        start = datetime(2026, 1, 15, 8, 0, tzinfo=timezone.utc)
        rule = parse_rrule("FREQ=MONTHLY;BYMONTHDAY=15;COUNT=3")
        dates = [d.strftime("%Y%m%d") for d in iter_occurrences(start, rule)]
        self.assertEqual(dates, ["20260115", "20260215", "20260315"])

    def test_annuelle_avec_intervalle(self):
        start = datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc)
        rule = parse_rrule("FREQ=YEARLY;INTERVAL=2;COUNT=3")
        dates = [d.strftime("%Y%m%d") for d in iter_occurrences(start, rule)]
        self.assertEqual(dates, ["20260714", "20280714", "20300714"])

    def test_quotidienne_bornee_par_until(self):
        start = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)
        rule = parse_rrule("FREQ=DAILY;UNTIL=20260305T090000Z")
        self.assertEqual(last_occurrence(start, rule).strftime("%Y%m%d"), "20260305")

    def test_regle_infinie_renvoie_none(self):
        start = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)
        self.assertIsNone(last_occurrence(start, parse_rrule("FREQ=DAILY")))


class TimeRangeTests(unittest.TestCase):
    def test_occurrence_lointaine_trouvee(self):
        self.assertTrue(
            overlaps_range(RECURRENT, ts("20260601T000000Z"), ts("20260608T000000Z"))
        )

    def test_exdate_respecte(self):
        self.assertFalse(
            overlaps_range(RECURRENT, ts("20260119T000000Z"), ts("20260120T000000Z"))
        )

    def test_hors_periode(self):
        self.assertFalse(overlaps_range(SIMPLE, ts("20260401T000000Z"), ts("20260402T000000Z")))
        self.assertTrue(overlaps_range(SIMPLE, ts("20260310T000000Z"), ts("20260311T000000Z")))

    def test_text_match(self):
        self.assertTrue(text_matches(SIMPLE, "VEVENT", "SUMMARY", "asil", negate=False))
        self.assertFalse(text_matches(SIMPLE, "VEVENT", "SUMMARY", "canopen", negate=False))
        self.assertTrue(text_matches(SIMPLE, "VEVENT", "SUMMARY", "canopen", negate=True))


class FeedTests(unittest.TestCase):
    def test_blocs_de_premier_niveau(self):
        data = (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VTIMEZONE\r\nTZID:Europe/Paris\r\n"
            "BEGIN:STANDARD\r\nTZOFFSETTO:+0100\r\nTZOFFSETFROM:+0200\r\n"
            "DTSTART:19701025T030000\r\nEND:STANDARD\r\nEND:VTIMEZONE\r\n"
            "BEGIN:VEVENT\r\nUID:x\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        blocks = iter_top_level_blocks(data)
        self.assertEqual([name for name, _ in blocks], ["VTIMEZONE", "VEVENT"])
        self.assertEqual(blocks[0][1][0], "BEGIN:VTIMEZONE")
        self.assertEqual(blocks[0][1][-1], "END:VTIMEZONE")
        self.assertIn("BEGIN:STANDARD", blocks[0][1])

    def test_fuseaux_dedupliques(self):
        event = (
            "BEGIN:VCALENDAR\r\nBEGIN:VTIMEZONE\r\nTZID:Europe/Paris\r\nEND:VTIMEZONE\r\n"
            "BEGIN:VEVENT\r\nUID:{uid}\r\nDTSTART;TZID=Europe/Paris:20260310T090000\r\n"
            "END:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        feed = build_feed(
            name="Perso",
            description="Agenda perso",
            color="#3584e4",
            objects=[event.format(uid="a"), event.format(uid="b")],
            refresh_minutes=30,
            prodid="-//test//FR",
        )
        self.assertEqual(feed.count("BEGIN:VTIMEZONE"), 1)
        self.assertEqual(feed.count("BEGIN:VEVENT"), 2)
        self.assertIn("X-WR-CALNAME:Perso", feed)
        self.assertIn("REFRESH-INTERVAL;VALUE=DURATION:PT30M", feed)
        self.assertTrue(feed.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertTrue(feed.endswith("END:VCALENDAR\r\n"))
        parse_calendar(feed)  # le flux doit rester ré-analysable


if __name__ == "__main__":
    unittest.main()

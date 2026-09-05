"""Tests for the read-only month view."""

from __future__ import annotations

import re
import unittest
from datetime import UTC, date, datetime

from helpers import ServerTestCase, make_event
from kalendra.calendarview import (
    MAX_PAR_JOUR,
    grille,
    mois_precedent,
    mois_suivant,
    parse_mois,
)
from kalendra.ics import expand_occurrences

PERSO = "/calendars/will/perso/"
VUE = "/view/will/perso/"


def evenements(html: str) -> list[str]:
    """Event chip titles, in document order."""
    return re.findall(r"<a class=evenement [^>]*title='([^']*)'", html)


class GrilleTests(unittest.TestCase):
    def test_navigation_entre_mois(self):
        self.assertEqual(mois_precedent(2026, 1), (2025, 12))
        self.assertEqual(mois_suivant(2026, 12), (2027, 1))
        self.assertEqual(mois_precedent(2026, 3), (2026, 2))
        self.assertEqual(mois_suivant(2026, 3), (2026, 4))

    def test_grille_commence_un_lundi_et_couvre_le_mois(self):
        semaines = grille(2026, 3, date(2026, 3, 15))
        premier = semaines[0][0].jour
        dernier = semaines[-1][-1].jour
        self.assertEqual(premier.weekday(), 0)
        self.assertEqual(dernier.weekday(), 6)
        self.assertLessEqual(premier, date(2026, 3, 1))
        self.assertGreaterEqual(dernier, date(2026, 3, 31))
        for semaine in semaines:
            self.assertEqual(len(semaine), 7)

    def test_les_jours_hors_mois_sont_marques(self):
        semaines = grille(2026, 3, date(2026, 3, 15))
        cases = [jour for semaine in semaines for jour in semaine]
        self.assertTrue(all(j.dans_le_mois for j in cases if j.jour.month == 3))
        self.assertTrue(all(not j.dans_le_mois for j in cases if j.jour.month != 3))

    def test_aujourd_hui_est_signale_une_seule_fois(self):
        semaines = grille(2026, 3, date(2026, 3, 15))
        marques = [j for semaine in semaines for j in semaine if j.aujourdhui]
        self.assertEqual(len(marques), 1)
        self.assertEqual(marques[0].jour, date(2026, 3, 15))

    def test_parse_mois_replie_sur_le_defaut(self):
        defaut = date(2026, 9, 5)
        self.assertEqual(parse_mois("2026-03", defaut), (2026, 3))
        self.assertEqual(parse_mois("", defaut), (2026, 9))
        self.assertEqual(parse_mois("2026-13", defaut), (2026, 9))
        self.assertEqual(parse_mois("n'importe quoi", defaut), (2026, 9))
        self.assertEqual(parse_mois("0000-00", defaut), (2026, 9))


class ExpansionTests(unittest.TestCase):
    """`expand_occurrences` yields instances, where `overlaps_range` answers yes/no."""

    def _unix(self, texte: str) -> int:
        moment = datetime.strptime(texte, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        return int(moment.timestamp())

    def test_evenement_simple(self):
        data = make_event(uid="s").decode()
        found = expand_occurrences(data, self._unix("20260301T000000Z"), self._unix("20260401T000000Z"))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].summary, "Revue archi VCU")
        self.assertFalse(found[0].all_day)

    def test_recurrence_hebdomadaire_avec_exception(self):
        data = make_event(
            uid="r",
            start="20260302T080000Z",
            end="20260302T083000Z",
            summary="Stand-up",
            extra="RRULE:FREQ=WEEKLY;BYDAY=MO\r\nEXDATE:20260316T080000Z",
        ).decode()
        found = expand_occurrences(
            data, self._unix("20260301T000000Z"), self._unix("20260401T000000Z")
        )
        jours = [datetime.fromtimestamp(o.start, UTC).strftime("%Y%m%d") for o in found]
        self.assertEqual(jours, ["20260302", "20260309", "20260323", "20260330"])

    def test_recurrence_id_remplace_une_occurrence(self):
        data = (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
            "BEGIN:VEVENT\r\nUID:o\r\nDTSTAMP:20260101T000000Z\r\n"
            "DTSTART:20260302T080000Z\r\nDTEND:20260302T083000Z\r\n"
            "SUMMARY:Stand-up\r\nRRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=3\r\nEND:VEVENT\r\n"
            "BEGIN:VEVENT\r\nUID:o\r\nDTSTAMP:20260101T000000Z\r\n"
            "RECURRENCE-ID:20260309T080000Z\r\n"
            "DTSTART:20260309T080000Z\r\nDTEND:20260309T090000Z\r\n"
            "SUMMARY:Stand-up (rallonge)\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        found = expand_occurrences(
            data, self._unix("20260301T000000Z"), self._unix("20260401T000000Z")
        )
        titres = [o.summary for o in found]
        self.assertEqual(titres.count("Stand-up (rallonge)"), 1)
        self.assertEqual(titres.count("Stand-up"), 2)

    def test_journee_entiere_detectee(self):
        data = (
            "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:j\r\nDTSTAMP:20260101T000000Z\r\n"
            "DTSTART;VALUE=DATE:20260318\r\nDTEND;VALUE=DATE:20260320\r\n"
            "SUMMARY:Deplacement\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        found = expand_occurrences(
            data, self._unix("20260301T000000Z"), self._unix("20260401T000000Z")
        )
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].all_day)

    def test_objet_illisible_ne_leve_pas(self):
        self.assertEqual(expand_occurrences("pas du ical", 0, 10**10), [])


class VueMensuelleTests(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.client.put(f"{PERSO}a.ics", make_event(uid="a", summary="Revue archi VCU"))
        self.client.put(
            f"{PERSO}b.ics",
            make_event(
                uid="b",
                start="20260302T080000Z",
                end="20260302T083000Z",
                summary="Stand-up",
                extra="RRULE:FREQ=WEEKLY;BYDAY=MO\r\nEXDATE:20260316T080000Z",
            ),
        )
        self.client.put(
            f"{PERSO}c.ics",
            (
                b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:c\r\n"
                b"DTSTAMP:20260101T000000Z\r\nDTSTART;VALUE=DATE:20260318\r\n"
                b"DTEND;VALUE=DATE:20260320\r\nSUMMARY:Deplacement Stuttgart\r\n"
                b"END:VEVENT\r\nEND:VCALENDAR\r\n"
            ),
        )

    def mois(self, m: str = "2026-03"):
        return self.client.request("GET", f"{VUE}?m={m}", headers={"Accept": "text/html"})

    def test_page_rendue(self):
        result = self.mois()
        self.assertStatus(result, 200)
        self.assertTrue(result.header("content-type").startswith("text/html"))
        self.assertIn("<title>Personnel — mars 2026</title>", result.text)
        self.assertIn("<th>lun</th>", result.text)
        self.assertIn("<th>dim</th>", result.text)

    def test_evenement_simple_present(self):
        self.assertIn("Revue archi VCU", evenements(self.mois().text))

    def test_recurrence_developpee_et_exception_respectee(self):
        html = self.mois().text
        self.assertEqual(evenements(html).count("Stand-up"), 4)  # 2, 9, 23, 30 mars

    def test_journee_entiere_sur_deux_cases(self):
        # DTEND is exclusive: the 18th and 19th, not the 20th.
        self.assertEqual(evenements(self.mois().text).count("Deplacement Stuttgart"), 2)

    def test_mois_vide_ne_montre_rien(self):
        self.assertEqual(evenements(self.mois("2025-01").text), [])

    def test_navigation_presente_dans_la_page(self):
        html = self.mois().text
        self.assertIn("m=2026-02", html)
        self.assertIn("m=2026-04", html)

    def test_mois_invalide_ne_casse_pas(self):
        for valeur in ("2026-13", "abc", "", "99999-01"):
            with self.subTest(valeur=valeur):
                self.assertStatus(self.mois(valeur), 200)

    def test_repli_au_dela_du_maximum_par_jour(self):
        for index in range(MAX_PAR_JOUR + 3):
            self.client.put(
                f"{PERSO}charge-{index}.ics",
                make_event(
                    uid=f"charge-{index}",
                    start=f"20260310T{9 + index:02d}0000Z",
                    end=f"20260310T{10 + index:02d}0000Z",
                    summary=f"Charge {index}",
                ),
            )
        # The label agrees in number and the grouping depends on the display
        # time zone: check that the fold is present, not how it is worded.
        self.assertIn("class=reste", self.mois().text)

    def test_lien_vers_le_detail(self):
        self.assertIn(f"{VUE}a.ics", self.mois().text)


class DetailTests(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.client.put(
            f"{PERSO}a.ics",
            make_event(uid="a", summary="Revue archi VCU", extra="LOCATION:Salle banc"),
        )

    def test_detail_affiche_les_champs(self):
        result = self.client.request("GET", f"{VUE}a.ics", headers={"Accept": "text/html"})
        self.assertStatus(result, 200)
        self.assertIn("Revue archi VCU", result.text)
        self.assertIn("Salle banc", result.text)
        self.assertIn("10/03/2026", result.text)

    def test_detail_montre_la_source_telle_quelle(self):
        result = self.client.request("GET", f"{VUE}a.ics", headers={"Accept": "text/html"})
        self.assertIn("UID:a", result.text)

    def test_objet_inconnu(self):
        result = self.client.request("GET", f"{VUE}absent.ics", headers={"Accept": "text/html"})
        self.assertStatus(result, 404)


class AccesTests(ServerTestCase):
    def test_index_liste_ses_propres_agendas(self):
        result = self.alice.request("GET", "/view/", headers={"Accept": "text/html"})
        self.assertStatus(result, 200)
        self.assertIn("Boulot", result.text)
        self.assertNotIn("Personnel", result.text)

    def test_ladministrateur_voit_tous_les_agendas(self):
        result = self.client.request("GET", "/view/", headers={"Accept": "text/html"})
        self.assertIn("Personnel", result.text)
        self.assertIn("Boulot", result.text)

    def test_un_utilisateur_ne_voit_pas_lagenda_dun_autre(self):
        self.assertStatus(self.alice.request("GET", VUE), 403)

    def test_ladministrateur_accede_a_tout(self):
        result = self.client.request("GET", "/view/alice/boulot/")
        self.assertStatus(result, 200)

    def test_authentification_requise(self):
        self.assertStatus(self.anon.request("GET", VUE), 401)

    def test_agenda_inconnu(self):
        self.assertStatus(self.client.request("GET", "/view/will/absent/"), 404)

    def test_la_vue_est_en_lecture_seule(self):
        for methode in ("POST", "PUT", "DELETE"):
            with self.subTest(methode=methode):
                result = self.client.request(methode, VUE)
                self.assertStatus(result, 405)
                self.assertIn("GET", result.header("allow"))

    def test_desactivable_avec_linterface_admin(self):
        self.config.admin_ui = False
        self.assertStatus(self.client.request("GET", VUE), 404)

    def test_lien_depuis_ladministration(self):
        result = self.client.request("GET", "/admin", headers={"Accept": "text/html"})
        self.assertIn("/view/", result.text)


class FuseauTests(ServerTestCase):
    """An all-day event must never slip by a day depending on the time zone."""

    def test_journee_entiere_reste_sur_son_jour(self):
        self.client.put(
            f"{PERSO}j.ics",
            (
                b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:j\r\n"
                b"DTSTAMP:20260101T000000Z\r\nDTSTART;VALUE=DATE:20260301\r\n"
                b"DTEND;VALUE=DATE:20260302\r\nSUMMARY:Ferie\r\n"
                b"END:VEVENT\r\nEND:VCALENDAR\r\n"
            ),
        )
        html = self.client.request(
            "GET", f"{VUE}?m=2026-03", headers={"Accept": "text/html"}
        ).text
        # 1 March 2026 is a Sunday: the last cell of the first week.
        premiere_semaine = html.split("</tr>")[1]
        self.assertIn("Ferie", premiere_semaine)
        self.assertEqual(evenements(html).count("Ferie"), 1)

    def test_occurrence_du_dernier_jour_du_mois_visible(self):
        self.client.put(
            f"{PERSO}fin.ics",
            make_event(
                uid="fin", start="20260331T220000Z", end="20260331T230000Z", summary="Nuit"
            ),
        )
        html = self.client.request(
            "GET", f"{VUE}?m=2026-03", headers={"Accept": "text/html"}
        ).text
        self.assertIn("Nuit", evenements(html))


if __name__ == "__main__":
    unittest.main()


class ImportTests(ServerTestCase):
    """Importing an .ics from the web view, available to any account."""

    FICHIER = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//tests//FR\r\n"
        "BEGIN:VEVENT\r\nUID:imp-1\r\nDTSTAMP:20260101T090000Z\r\n"
        "DTSTART:20260601T090000Z\r\nDTEND:20260601T100000Z\r\n"
        "SUMMARY:Premier\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:imp-2\r\nDTSTAMP:20260101T090000Z\r\n"
        "DTSTART:20260602T090000Z\r\nDTEND:20260602T100000Z\r\n"
        "SUMMARY:Second\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )

    def _jeton(self, client, chemin: str) -> str:
        page = client.request("GET", chemin, headers={"Accept": "text/html"}).text
        marqueur = "name=csrf value='"
        debut = page.index(marqueur) + len(marqueur)
        return page[debut : page.index("'", debut)]

    def _televerser(self, client, chemin: str, contenu: str, csrf: str):
        limite = "----KalendraTest"
        corps = (
            f"--{limite}\r\n"
            'Content-Disposition: form-data; name="csrf"\r\n\r\n'
            f"{csrf}\r\n"
            f"--{limite}\r\n"
            'Content-Disposition: form-data; name="fichier"; filename="a.ics"\r\n'
            "Content-Type: text/calendar\r\n\r\n"
            f"{contenu}\r\n"
            f"--{limite}--\r\n"
        ).encode()
        return client.request(
            "POST",
            chemin,
            corps,
            {"Content-Type": f"multipart/form-data; boundary={limite}"},
        )

    def test_un_compte_ordinaire_peut_importer(self):
        csrf = self._jeton(self.alice, "/view/alice/boulot/")
        result = self._televerser(
            self.alice, "/view/alice/boulot/import", self.FICHIER, csrf
        )
        self.assertStatus(result, 303)
        self.assertIn("2", result.header("location"))
        calendrier = self.db.get_calendar(self.alice_id, "boulot")
        self.assertEqual(self.db.calendar_stats(calendrier["id"]), 2)

    def test_le_reimport_met_a_jour_sans_dupliquer(self):
        csrf = self._jeton(self.alice, "/view/alice/boulot/")
        self._televerser(self.alice, "/view/alice/boulot/import", self.FICHIER, csrf)
        self._televerser(self.alice, "/view/alice/boulot/import", self.FICHIER, csrf)
        calendrier = self.db.get_calendar(self.alice_id, "boulot")
        self.assertEqual(self.db.calendar_stats(calendrier["id"]), 2)

    def test_un_evenement_invalide_nempeche_pas_les_autres(self):
        casse = self.FICHIER.replace(
            "BEGIN:VEVENT\r\nUID:imp-2", "BEGIN:VEVENT\r\nSUMMARY:sans uid\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:imp-2"
        )
        csrf = self._jeton(self.alice, "/view/alice/boulot/")
        result = self._televerser(self.alice, "/view/alice/boulot/import", casse, csrf)
        self.assertStatus(result, 303)
        calendrier = self.db.get_calendar(self.alice_id, "boulot")
        self.assertEqual(self.db.calendar_stats(calendrier["id"]), 2)

    def test_jeton_csrf_obligatoire(self):
        result = self._televerser(
            self.alice, "/view/alice/boulot/import", self.FICHIER, "faux"
        )
        self.assertStatus(result, 403)

    def test_import_dans_lagenda_dun_autre_refuse(self):
        csrf = self._jeton(self.alice, "/view/alice/boulot/")
        result = self._televerser(
            self.alice, "/view/will/perso/import", self.FICHIER, csrf
        )
        self.assertStatus(result, 403)

    def test_agenda_inconnu(self):
        csrf = self._jeton(self.alice, "/view/alice/boulot/")
        result = self._televerser(
            self.alice, "/view/alice/zzz/import", self.FICHIER, csrf
        )
        self.assertStatus(result, 404)

    def test_la_vue_reste_en_lecture_seule_ailleurs(self):
        self.assertStatus(self.alice.request("POST", "/view/alice/boulot/"), 405)


class GestionAgendaTests(ServerTestCase):
    """Calendar creation and deletion by their owner, from /view/."""

    FICHIER = ImportTests.FICHIER

    def _jeton(self, client) -> str:
        page = client.request("GET", "/view/", headers={"Accept": "text/html"}).text
        marqueur = "name=csrf value='"
        debut = page.index(marqueur) + len(marqueur)
        return page[debut : page.index("'", debut)]

    def _creer(self, client, champs: dict[str, str], fichier: str | None = None):
        limite = "----KalendraTest"
        morceaux = []
        for nom, valeur in champs.items():
            morceaux.append(
                f"--{limite}\r\n"
                f'Content-Disposition: form-data; name="{nom}"\r\n\r\n{valeur}\r\n'
            )
        if fichier is not None:
            morceaux.append(
                f"--{limite}\r\n"
                'Content-Disposition: form-data; name="fichier"; filename="a.ics"\r\n'
                f"Content-Type: text/calendar\r\n\r\n{fichier}\r\n"
            )
        corps = ("".join(morceaux) + f"--{limite}--\r\n").encode("utf-8")
        return client.request(
            "POST",
            "/view/agendas/creer",
            corps,
            {"Content-Type": f"multipart/form-data; boundary={limite}"},
        )

    def test_un_compte_ordinaire_cree_son_agenda(self):
        csrf = self._jeton(self.alice)
        result = self._creer(self.alice, {"csrf": csrf, "name": "sport"})
        self.assertStatus(result, 303)
        self.assertIsNotNone(self.db.get_calendar(self.alice_id, "sport"))

    def test_creer_et_importer_en_une_fois(self):
        csrf = self._jeton(self.alice)
        result = self._creer(
            self.alice, {"csrf": csrf, "name": "vacances"}, fichier=self.FICHIER
        )
        self.assertStatus(result, 303)
        calendrier = self.db.get_calendar(self.alice_id, "vacances")
        self.assertIsNotNone(calendrier)
        self.assertEqual(self.db.calendar_stats(calendrier["id"]), 2)

    def test_un_import_rate_ne_laisse_pas_dagenda_vide(self):
        csrf = self._jeton(self.alice)
        result = self._creer(
            self.alice, {"csrf": csrf, "name": "rate"}, fichier="pas du iCalendar"
        )
        self.assertStatus(result, 303)
        self.assertIsNone(self.db.get_calendar(self.alice_id, "rate"))

    def test_nom_invalide_refuse(self):
        csrf = self._jeton(self.alice)
        result = self._creer(self.alice, {"csrf": csrf, "name": "../evasion"})
        self.assertStatus(result, 303)
        self.assertIn("invalide", result.header("location"))

    def test_creer_pour_autrui_refuse(self):
        csrf = self._jeton(self.alice)
        result = self._creer(
            self.alice, {"csrf": csrf, "name": "pirate", "proprietaire": "will"}
        )
        self.assertStatus(result, 303)
        self.assertIsNone(self.db.get_calendar(self.will_id, "pirate"))

    def test_ladministrateur_cree_pour_autrui(self):
        csrf = self._jeton(self.client)
        result = self._creer(
            self.client, {"csrf": csrf, "name": "delegue", "proprietaire": "alice"}
        )
        self.assertStatus(result, 303)
        self.assertIsNotNone(self.db.get_calendar(self.alice_id, "delegue"))

    def test_csrf_obligatoire_a_la_creation(self):
        result = self._creer(self.alice, {"csrf": "faux", "name": "x"})
        self.assertStatus(result, 403)

    def test_suppression_par_le_proprietaire(self):
        csrf = self._jeton(self.alice)
        calendrier = self.db.get_calendar(self.alice_id, "boulot")
        result = self.alice.request(
            "POST",
            "/view/alice/boulot/supprimer",
            f"csrf={csrf}".encode(),
            {"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertStatus(result, 303)
        self.assertIsNone(self.db.get_calendar(self.alice_id, "boulot"))
        self.assertIsNotNone(calendrier)

    def test_suppression_de_lagenda_dun_autre_refusee(self):
        csrf = self._jeton(self.alice)
        result = self.alice.request(
            "POST",
            "/view/will/perso/supprimer",
            f"csrf={csrf}".encode(),
            {"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertStatus(result, 403)
        self.assertIsNotNone(self.db.get_calendar(self.will_id, "perso"))

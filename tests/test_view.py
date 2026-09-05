"""Tests de la vue mensuelle en lecture seule."""

from __future__ import annotations

import re
import unittest
from datetime import date, datetime, timezone

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
    """Titres des puces d'événement, dans l'ordre du document."""
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
    """`expand_occurrences` produit les instances, là où `overlaps_range` répond oui/non."""

    def _unix(self, texte: str) -> int:
        moment = datetime.strptime(texte, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
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
        jours = [datetime.fromtimestamp(o.start, timezone.utc).strftime("%Y%m%d") for o in found]
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
                "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:c\r\n"
                "DTSTAMP:20260101T000000Z\r\nDTSTART;VALUE=DATE:20260318\r\n"
                "DTEND;VALUE=DATE:20260320\r\nSUMMARY:Deplacement Stuttgart\r\n"
                "END:VEVENT\r\nEND:VCALENDAR\r\n"
            ).encode("utf-8"),
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
        # DTEND est exclusif : le 18 et le 19, pas le 20.
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
        # Le libellé s'accorde en nombre et le regroupement dépend du fuseau
        # d'affichage : on vérifie la présence du repli, pas sa formulation.
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
    """Une journée entière ne doit jamais glisser d'un jour selon le fuseau."""

    def test_journee_entiere_reste_sur_son_jour(self):
        self.client.put(
            f"{PERSO}j.ics",
            (
                "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:j\r\n"
                "DTSTAMP:20260101T000000Z\r\nDTSTART;VALUE=DATE:20260301\r\n"
                "DTEND;VALUE=DATE:20260302\r\nSUMMARY:Ferie\r\n"
                "END:VEVENT\r\nEND:VCALENDAR\r\n"
            ).encode("utf-8"),
        )
        html = self.client.request(
            "GET", f"{VUE}?m=2026-03", headers={"Accept": "text/html"}
        ).text
        # Le 1er mars 2026 est un dimanche : dernière case de la première semaine.
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

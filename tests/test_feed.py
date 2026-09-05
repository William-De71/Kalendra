"""Public ICS feed tests (Google Calendar / Proton Calendar integration)."""

from __future__ import annotations

import unittest

from helpers import ServerTestCase, make_event

PERSO = "/calendars/will/perso/"


class FeedTests(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.client.put(f"{PERSO}a.ics", make_event(uid="a", summary="Revue VCU"))
        self.client.put(
            f"{PERSO}b.ics",
            make_event(uid="b", start="20260311T140000Z", end="20260311T150000Z", summary="Bench CAN"),
        )
        self.token = self.db.get_calendar_by_id(self.perso_id)["feed_token"]

    def test_le_flux_est_accessible_sans_authentification(self):
        result = self.anon.request("GET", f"/feed/{self.token}.ics")
        self.assertStatus(result, 200)
        self.assertTrue(result.header("content-type").startswith("text/calendar"))
        self.assertIn("BEGIN:VCALENDAR", result.text)
        self.assertIn("UID:a", result.text)
        self.assertIn("UID:b", result.text)

    def test_le_flux_annonce_son_nom_et_sa_frequence(self):
        text = self.anon.request("GET", f"/feed/{self.token}.ics").text
        self.assertIn("X-WR-CALNAME:Personnel", text)
        self.assertIn("REFRESH-INTERVAL;VALUE=DURATION:PT", text)
        self.assertIn("X-PUBLISHED-TTL:PT", text)
        self.assertIn("METHOD:PUBLISH", text)

    def test_jeton_inconnu_renvoie_404(self):
        self.assertStatus(self.anon.request("GET", "/feed/inexistant.ics"), 404)

    def test_le_flux_suit_les_modifications(self):
        first = self.anon.request("GET", f"/feed/{self.token}.ics")
        self.client.request("DELETE", f"{PERSO}a.ics")
        second = self.anon.request("GET", f"/feed/{self.token}.ics")
        self.assertNotIn("UID:a", second.text)
        self.assertNotEqual(first.header("etag"), second.header("etag"))

    def test_etag_permet_une_reponse_304(self):
        first = self.anon.request("GET", f"/feed/{self.token}.ics")
        second = self.anon.request(
            "GET", f"/feed/{self.token}.ics", headers={"If-None-Match": first.header("etag")}
        )
        self.assertStatus(second, 304)
        self.assertEqual(second.body, b"")

    def test_rotation_du_jeton_invalide_lancienne_url(self):
        nouveau = self.db.rotate_feed_token(self.perso_id)
        self.assertStatus(self.anon.request("GET", f"/feed/{self.token}.ics"), 404)
        self.assertStatus(self.anon.request("GET", f"/feed/{nouveau}.ics"), 200)

    def test_flux_desactive_renvoie_404(self):
        self.db.update_calendar(self.perso_id, feed_enabled=0)
        self.assertStatus(self.anon.request("GET", f"/feed/{self.token}.ics"), 404)

    def test_flux_globalement_desactive(self):
        self.config.feeds_enabled = False
        self.assertStatus(self.anon.request("GET", f"/feed/{self.token}.ics"), 404)

    def test_head_ne_renvoie_pas_de_corps(self):
        result = self.anon.request("HEAD", f"/feed/{self.token}.ics")
        self.assertStatus(result, 200)
        self.assertEqual(result.body, b"")
        self.assertNotEqual(result.header("content-length"), "0")

    def test_les_agendas_sont_cloisonnes(self):
        autre = self.db.get_calendar(self.alice_id, "boulot")["feed_token"]
        text = self.anon.request("GET", f"/feed/{autre}.ics").text
        self.assertNotIn("UID:a", text)


class HealthTests(ServerTestCase):
    def test_endpoint_de_sante(self):
        result = self.anon.request("GET", "/health")
        self.assertStatus(result, 200)
        self.assertIn('"status": "ok"', result.text)


if __name__ == "__main__":
    unittest.main()

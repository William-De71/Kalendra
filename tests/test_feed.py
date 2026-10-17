"""Public ICS feed tests (Google Calendar / Proton Calendar integration)."""

from __future__ import annotations

import re
import unittest

from helpers import ServerTestCase, make_event

PERSO = "/calendars/will/perso/"

#: An event as a real client writes one: time zone, alarm, attendees, a long
#: accented description and vendor properties — everything a strict consumer
#: such as Google trips over when the feed mangles it.
RICHE = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Mozilla.org/NONSGML Mozilla Calendar V1.1//EN\r\n"
    "BEGIN:VTIMEZONE\r\nTZID:Europe/Paris\r\nBEGIN:DAYLIGHT\r\nTZOFFSETFROM:+0100\r\n"
    "TZOFFSETTO:+0200\r\nTZNAME:CEST\r\nDTSTART:19700329T020000\r\n"
    "RRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=3\r\nEND:DAYLIGHT\r\nBEGIN:STANDARD\r\n"
    "TZOFFSETFROM:+0200\r\nTZOFFSETTO:+0100\r\nTZNAME:CET\r\nDTSTART:19701025T030000\r\n"
    "RRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=10\r\nEND:STANDARD\r\nEND:VTIMEZONE\r\n"
    "BEGIN:VEVENT\r\nUID:{uid}\r\nDTSTAMP:20260101T090000Z\r\n"
    "DTSTART;TZID=Europe/Paris:20260310T090000\r\nDTEND;TZID=Europe/Paris:20260310T100000\r\n"
    "SUMMARY:Réunion de l'équipe — préparation du salon\r\n"
    "DESCRIPTION:Ordre du jour : écarts de planning\\, état des achats\\, revue d\r\n"
    " es risques\\nPrévoir le café ☕ et les croissants pour tout le monde\\, merci\r\n"
    "ATTENDEE;CN=\"Dupont, Jean\";ROLE=REQ-PARTICIPANT:mailto:jean@example.org\r\n"
    "X-MOZ-GENERATION:3\r\nBEGIN:VALARM\r\nACTION:DISPLAY\r\nTRIGGER:-PT15M\r\n"
    "DESCRIPTION:Rappel\r\nEND:VALARM\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
)


def defauts_de_conformite(flux: bytes) -> list[str]:
    """Check what strict iCalendar consumers enforce (RFC 5545 §3.1, §3.6)."""
    problemes = []
    if b"\n" in flux.replace(b"\r\n", b"") or b"\r" in flux.replace(b"\r\n", b""):
        problemes.append("fin de ligne autre que CRLF")
    if not flux.endswith(b"\r\n"):
        problemes.append("dernière ligne sans CRLF")
    physiques = flux.split(b"\r\n")[:-1]
    for numero, ligne in enumerate(physiques, start=1):
        if len(ligne) > 75:
            problemes.append(f"ligne {numero} de {len(ligne)} octets")
        try:
            ligne.decode("utf-8")
        except UnicodeDecodeError:
            problemes.append(f"ligne {numero} coupée au milieu d'un caractère")
    logiques: list[str] = []
    for ligne in flux.decode("utf-8", "replace").split("\r\n")[:-1]:
        if ligne[:1] in (" ", "\t") and logiques:
            logiques[-1] += ligne[1:]
        else:
            logiques.append(ligne)
    pile: list[str] = []
    proprietes: dict[str, set[str]] = {}
    tzids_definis, tzids_utilises = set(), set()
    for ligne in logiques:
        nom = re.split(r"[;:]", ligne, maxsplit=1)[0].upper()
        valeur = ligne.split(":", 1)[1] if ":" in ligne else ""
        if nom == "BEGIN":
            pile.append(valeur.upper())
            proprietes[valeur.upper()] = set()
        elif nom == "END":
            if not pile or pile[-1] != valeur.upper():
                problemes.append(f"END:{valeur} sans BEGIN correspondant")
                break
            composant = pile.pop()
            vues = proprietes.get(composant, set())
            if composant == "VEVENT":
                for requise in ("UID", "DTSTAMP", "DTSTART"):
                    if requise not in vues:
                        problemes.append(f"VEVENT sans {requise}")
            if composant == "VCALENDAR":
                for requise in ("VERSION", "PRODID"):
                    if requise not in vues:
                        problemes.append(f"VCALENDAR sans {requise}")
        elif pile:
            proprietes[pile[-1]].add(nom)
            if nom == "TZID" and pile[-1] == "VTIMEZONE":
                tzids_definis.add(valeur)
            tzids_utilises.update(re.findall(r";TZID=([^;:]+)", ligne.split(":", 1)[0]))
    if pile:
        problemes.append(f"composants non fermés : {pile}")
    for tzid in sorted(tzids_utilises - tzids_definis):
        problemes.append(f"TZID {tzid} sans VTIMEZONE")
    return problemes


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


class FeedConformanceTests(ServerTestCase):
    """The feed as Google and Proton parse it, not as our own parser does."""

    def flux(self) -> bytes:
        token = self.db.get_calendar_by_id(self.perso_id)["feed_token"]
        return self.anon.request("GET", f"/feed/{token}.ics").body

    def test_evenement_riche(self):
        self.assertStatus(
            self.client.put(f"{PERSO}r.ics", RICHE.format(uid="r").encode("utf-8")), 201
        )
        self.assertEqual(defauts_de_conformite(self.flux()), [])

    def test_les_rappels_restent_fermes(self):
        self.client.put(f"{PERSO}r.ics", RICHE.format(uid="r").encode("utf-8"))
        texte = self.flux().decode("utf-8")
        self.assertEqual(texte.count("BEGIN:VALARM"), texte.count("END:VALARM"))
        self.assertEqual(texte.count("BEGIN:VALARM"), 1)

    def test_fuseau_dedoublonne(self):
        for uid in ("r1", "r2", "r3"):
            self.client.put(f"{PERSO}{uid}.ics", RICHE.format(uid=uid).encode("utf-8"))
        texte = self.flux().decode("utf-8")
        self.assertEqual(texte.count("BEGIN:VTIMEZONE"), 1)
        self.assertEqual(defauts_de_conformite(self.flux()), [])

    def test_objet_depose_avec_des_fins_de_ligne_unix(self):
        brut = RICHE.format(uid="lf").replace("\r\n", "\n").encode("utf-8")
        self.assertStatus(self.client.put(f"{PERSO}lf.ics", brut), 201)
        self.assertEqual(defauts_de_conformite(self.flux()), [])

    def test_proprietes_conservees(self):
        self.client.put(f"{PERSO}r.ics", RICHE.format(uid="r").encode("utf-8"))
        texte = self.flux().decode("utf-8").replace("\r\n ", "")
        for attendu in ("X-MOZ-GENERATION:3", 'CN="Dupont, Jean"', "TRIGGER:-PT15M", "☕"):
            self.assertIn(attendu, texte)

    def test_agenda_vide(self):
        self.client.request("DELETE", f"{PERSO}a.ics")
        self.client.request("DELETE", f"{PERSO}b.ics")
        self.assertEqual(defauts_de_conformite(self.flux()), [])


class HealthTests(ServerTestCase):
    def test_endpoint_de_sante(self):
        result = self.anon.request("GET", "/health")
        self.assertStatus(result, 200)
        self.assertIn('"status": "ok"', result.text)


if __name__ == "__main__":
    unittest.main()

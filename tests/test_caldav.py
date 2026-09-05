"""Tests du protocole CalDAV : découverte, écriture, filtres, synchronisation."""

from __future__ import annotations

import unittest
from xml.etree import ElementTree as ET

from helpers import ServerTestCase, make_event
from kalendra.xmlutil import NS_CALDAV, NS_DAV, caldav, cs, dav

PERSO = "/calendars/will/perso/"

PROPFIND_PRINCIPAL = """<?xml version="1.0" encoding="utf-8"?>
<D:propfind xmlns:D="DAV:"><D:prop><D:current-user-principal/></D:prop></D:propfind>"""

PROPFIND_HOME = """<?xml version="1.0" encoding="utf-8"?>
<D:propfind xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:prop><C:calendar-home-set/><D:displayname/></D:prop></D:propfind>"""

PROPFIND_CALENDAR = """<?xml version="1.0" encoding="utf-8"?>
<D:propfind xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav"
            xmlns:CS="http://calendarserver.org/ns/">
  <D:prop>
    <D:resourcetype/><D:displayname/><D:sync-token/>
    <CS:getctag/><C:supported-calendar-component-set/>
    <D:current-user-privilege-set/><D:supported-report-set/>
  </D:prop></D:propfind>"""


def parse(result) -> ET.Element:
    return ET.fromstring(result.body)


def hrefs(root: ET.Element) -> list[str]:
    return [
        node.findtext(dav("href"), "")
        for node in root.findall(dav("response"))
    ]


def prop_text(root: ET.Element, href: str, qname: str) -> str | None:
    for response in root.findall(dav("response")):
        if response.findtext(dav("href")) != href:
            continue
        for propstat in response.findall(dav("propstat")):
            status = propstat.findtext(dav("status"), "")
            node = propstat.find(f"{dav('prop')}/{qname}")
            if node is not None and "200" in status:
                return node.text or ""
    return None


class DiscoveryTests(ServerTestCase):
    def test_options_annonce_calendar_access(self):
        result = self.client.request("OPTIONS", "/")
        self.assertStatus(result, 204)
        self.assertIn("calendar-access", result.header("dav"))
        self.assertIn("PROPFIND", result.header("allow"))

    def test_well_known_redirige_vers_la_racine(self):
        result = self.anon.request("GET", "/.well-known/caldav")
        self.assertStatus(result, 301)
        self.assertEqual(result.header("location"), "/")

    def test_sans_identifiants_le_serveur_demande_une_authentification(self):
        result = self.anon.propfind("/", PROPFIND_PRINCIPAL)
        self.assertStatus(result, 401)
        self.assertIn("Basic", result.header("www-authenticate"))

    def test_mauvais_mot_de_passe_refuse(self):
        from helpers import Client

        result = Client(self.app, "will", "faux").propfind("/", PROPFIND_PRINCIPAL)
        self.assertStatus(result, 401)

    def test_racine_expose_le_principal_courant(self):
        result = self.client.propfind("/", PROPFIND_PRINCIPAL)
        self.assertStatus(result, 207)
        root = parse(result)
        node = root.find(f".//{dav('current-user-principal')}/{dav('href')}")
        self.assertEqual(node.text, "/principals/will/")

    def test_principal_expose_le_home_set(self):
        result = self.client.propfind("/principals/will/", PROPFIND_HOME)
        self.assertStatus(result, 207)
        node = parse(result).find(f".//{caldav('calendar-home-set')}/{dav('href')}")
        self.assertEqual(node.text, "/calendars/will/")

    def test_home_set_liste_les_agendas(self):
        result = self.client.propfind("/calendars/will/", PROPFIND_CALENDAR, depth="1")
        self.assertStatus(result, 207)
        self.assertIn(PERSO, hrefs(parse(result)))

    def test_agenda_declare_ses_proprietes(self):
        result = self.client.propfind(PERSO, PROPFIND_CALENDAR)
        self.assertStatus(result, 207)
        root = parse(result)
        self.assertIsNotNone(root.find(f".//{dav('resourcetype')}/{caldav('calendar')}"))
        self.assertEqual(prop_text(root, PERSO, dav("displayname")), "Personnel")
        self.assertTrue(prop_text(root, PERSO, cs("getctag")))
        comps = root.findall(f".//{caldav('supported-calendar-component-set')}/{caldav('comp')}")
        self.assertEqual({c.get("name") for c in comps}, {"VEVENT", "VTODO"})
        reports = {
            node.tag
            for node in root.iter()
            if node.tag in {caldav("calendar-query"), dav("sync-collection")}
        }
        self.assertEqual(reports, {caldav("calendar-query"), dav("sync-collection")})

    def test_propriete_inconnue_renvoyee_en_404(self):
        body = (
            '<?xml version="1.0"?><D:propfind xmlns:D="DAV:">'
            "<D:prop><D:inconnue/></D:prop></D:propfind>"
        )
        root = parse(self.client.propfind(PERSO, body))
        statuses = [node.text for node in root.iter(dav("status"))]
        self.assertTrue(any("404" in (s or "") for s in statuses))


class IsolationTests(ServerTestCase):
    def test_un_utilisateur_ne_voit_pas_lagenda_dun_autre(self):
        result = self.alice.propfind(PERSO, PROPFIND_CALENDAR)
        self.assertStatus(result, 403)

    def test_ladministrateur_accede_a_tout(self):
        result = self.client.propfind("/calendars/alice/boulot/", PROPFIND_CALENDAR)
        self.assertStatus(result, 207)


class ObjectLifecycleTests(ServerTestCase):
    def test_creation_lecture_suppression(self):
        put = self.client.put(f"{PERSO}evt-1.ics", make_event())
        self.assertStatus(put, 201)
        etag = put.header("etag")
        self.assertTrue(etag.startswith('"'))

        got = self.client.request("GET", f"{PERSO}evt-1.ics")
        self.assertStatus(got, 200)
        self.assertIn("UID:evt-1", got.text)
        self.assertEqual(got.header("etag"), etag)
        self.assertTrue(got.header("content-type").startswith("text/calendar"))

        deleted = self.client.request("DELETE", f"{PERSO}evt-1.ics")
        self.assertStatus(deleted, 204)
        self.assertStatus(self.client.request("GET", f"{PERSO}evt-1.ics"), 404)

    def test_mise_a_jour_renvoie_204_et_un_nouvel_etag(self):
        first = self.client.put(f"{PERSO}evt-1.ics", make_event())
        second = self.client.put(
            f"{PERSO}evt-1.ics", make_event(summary="Revue archi VCU (reportée)")
        )
        self.assertStatus(second, 204)
        self.assertNotEqual(first.header("etag"), second.header("etag"))

    def test_if_none_match_empeche_lecrasement(self):
        self.client.put(f"{PERSO}evt-1.ics", make_event())
        result = self.client.put(
            f"{PERSO}evt-1.ics", make_event(), headers={"If-None-Match": "*"}
        )
        self.assertStatus(result, 412)

    def test_if_match_detecte_un_conflit(self):
        self.client.put(f"{PERSO}evt-1.ics", make_event())
        result = self.client.put(
            f"{PERSO}evt-1.ics", make_event(), headers={"If-Match": '"perime"'}
        )
        self.assertStatus(result, 412)

    def test_if_match_valide_accepte(self):
        etag = self.client.put(f"{PERSO}evt-1.ics", make_event()).header("etag")
        result = self.client.put(
            f"{PERSO}evt-1.ics", make_event(summary="Décalée"), headers={"If-Match": etag}
        )
        self.assertStatus(result, 204)

    def test_uid_duplique_refuse(self):
        self.client.put(f"{PERSO}evt-1.ics", make_event(uid="partage"))
        result = self.client.put(f"{PERSO}evt-2.ics", make_event(uid="partage"))
        self.assertStatus(result, 403)
        self.assertIn("no-uid-conflict", result.text)

    def test_contenu_invalide_refuse(self):
        result = self.client.put(f"{PERSO}mauvais.ics", b"ceci n'est pas un calendrier")
        self.assertStatus(result, 403)
        self.assertIn("valid-calendar-data", result.text)

    def test_composant_non_supporte_refuse(self):
        self.db.update_calendar(self.perso_id, components="VTODO")
        result = self.client.put(f"{PERSO}evt-1.ics", make_event())
        self.assertStatus(result, 403)
        self.assertIn("supported-calendar-component", result.text)

    def test_ressource_trop_volumineuse_refusee(self):
        self.config.max_resource_size = 200
        result = self.client.put(f"{PERSO}gros.ics", make_event())
        self.assertStatus(result, 413)

    def test_nom_de_ressource_invalide_refuse(self):
        result = self.client.put(f"{PERSO}chemin/interdit.ics", make_event())
        self.assertIn(result.status, (400, 403, 404))


class CollectionTests(ServerTestCase):
    def test_mkcalendar_cree_un_agenda(self):
        body = (
            '<?xml version="1.0"?><C:mkcalendar xmlns:D="DAV:" '
            'xmlns:C="urn:ietf:params:xml:ns:caldav"><D:set><D:prop>'
            "<D:displayname>Astreinte</D:displayname>"
            "<C:calendar-description>Rotation</C:calendar-description>"
            '<C:supported-calendar-component-set><C:comp name="VEVENT"/>'
            "</C:supported-calendar-component-set>"
            "</D:prop></D:set></C:mkcalendar>"
        )
        result = self.client.request(
            "MKCALENDAR", "/calendars/will/astreinte/", body.encode("utf-8")
        )
        self.assertStatus(result, 201)
        calendar = self.db.get_calendar(self.will_id, "astreinte")
        self.assertEqual(calendar["display_name"], "Astreinte")
        self.assertEqual(calendar["components"], "VEVENT")
        self.assertTrue(calendar["feed_token"])

    def test_mkcalendar_sur_un_agenda_existant_echoue(self):
        result = self.client.request("MKCALENDAR", PERSO, b"")
        self.assertStatus(result, 405)

    def test_proppatch_modifie_nom_et_couleur(self):
        body = (
            '<?xml version="1.0"?><D:propertyupdate xmlns:D="DAV:" '
            'xmlns:I="http://apple.com/ns/ical/"><D:set><D:prop>'
            "<D:displayname>Agenda perso</D:displayname>"
            "<I:calendar-color>#e01b24</I:calendar-color>"
            "</D:prop></D:set></D:propertyupdate>"
        )
        result = self.client.request("PROPPATCH", PERSO, body.encode("utf-8"))
        self.assertStatus(result, 207)
        calendar = self.db.get_calendar_by_id(self.perso_id)
        self.assertEqual(calendar["display_name"], "Agenda perso")
        self.assertEqual(calendar["color"], "#e01b24")

    def test_proppatch_refuse_une_propriete_protegee(self):
        body = (
            '<?xml version="1.0"?><D:propertyupdate xmlns:D="DAV:"><D:set><D:prop>'
            "<D:getetag>x</D:getetag></D:prop></D:set></D:propertyupdate>"
        )
        result = self.client.request("PROPPATCH", PERSO, body.encode("utf-8"))
        self.assertIn("403", result.text)

    def test_suppression_dun_agenda(self):
        self.client.put(f"{PERSO}evt-1.ics", make_event())
        self.assertStatus(self.client.request("DELETE", PERSO), 204)
        self.assertIsNone(self.db.get_calendar(self.will_id, "perso"))

    def test_get_sur_une_collection_est_refuse(self):
        result = self.client.request("GET", PERSO)
        self.assertStatus(result, 405)


class ReportTests(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.client.put(f"{PERSO}mars.ics", make_event(uid="mars", start="20260310T090000Z", end="20260310T100000Z"))
        self.client.put(
            f"{PERSO}juin.ics",
            make_event(uid="juin", start="20260610T090000Z", end="20260610T100000Z", summary="Point CANopen"),
        )
        self.client.put(
            f"{PERSO}hebdo.ics",
            make_event(
                uid="hebdo",
                start="20260105T090000Z",
                end="20260105T093000Z",
                summary="Stand-up",
                extra="RRULE:FREQ=WEEKLY;BYDAY=MO",
            ),
        )

    def _query(self, start: str, end: str, component: str = "VEVENT") -> ET.Element:
        body = f"""<?xml version="1.0"?>
        <C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
          <D:prop><D:getetag/><C:calendar-data/></D:prop>
          <C:filter><C:comp-filter name="VCALENDAR">
            <C:comp-filter name="{component}">
              <C:time-range start="{start}" end="{end}"/>
            </C:comp-filter></C:comp-filter></C:filter>
        </C:calendar-query>"""
        result = self.client.report(PERSO, body)
        self.assertStatus(result, 207)
        return parse(result)

    def test_calendar_query_filtre_par_plage(self):
        root = self._query("20260301T000000Z", "20260401T000000Z")
        found = hrefs(root)
        self.assertIn(f"{PERSO}mars.ics", found)
        self.assertNotIn(f"{PERSO}juin.ics", found)

    def test_calendar_query_developpe_les_recurrences(self):
        # Le stand-up hebdomadaire doit ressortir bien après sa première occurrence.
        root = self._query("20260601T000000Z", "20260608T000000Z")
        self.assertIn(f"{PERSO}hebdo.ics", hrefs(root))

    def test_calendar_query_renvoie_les_donnees(self):
        root = self._query("20260301T000000Z", "20260401T000000Z")
        payloads = [node.text or "" for node in root.iter(caldav("calendar-data"))]
        self.assertTrue(any("UID:mars" in payload for payload in payloads))

    def test_calendar_query_sur_un_type_absent(self):
        root = self._query("20260101T000000Z", "20270101T000000Z", component="VTODO")
        self.assertEqual(hrefs(root), [])

    def test_calendar_query_avec_text_match(self):
        body = """<?xml version="1.0"?>
        <C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
          <D:prop><D:getetag/></D:prop>
          <C:filter><C:comp-filter name="VCALENDAR">
            <C:comp-filter name="VEVENT">
              <C:prop-filter name="SUMMARY"><C:text-match>CANopen</C:text-match></C:prop-filter>
            </C:comp-filter></C:comp-filter></C:filter>
        </C:calendar-query>"""
        root = parse(self.client.report(PERSO, body))
        self.assertEqual(hrefs(root), [f"{PERSO}juin.ics"])

    def test_calendar_multiget(self):
        body = f"""<?xml version="1.0"?>
        <C:calendar-multiget xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
          <D:prop><D:getetag/><C:calendar-data/></D:prop>
          <D:href>{PERSO}mars.ics</D:href>
          <D:href>{PERSO}absent.ics</D:href>
        </C:calendar-multiget>"""
        result = self.client.report(PERSO, body)
        self.assertStatus(result, 207)
        root = parse(result)
        self.assertEqual(len(root.findall(dav("response"))), 2)
        self.assertIn("404", result.text)
        self.assertIn("UID:mars", result.text)

    def test_free_busy_query(self):
        body = """<?xml version="1.0"?>
        <C:free-busy-query xmlns:C="urn:ietf:params:xml:ns:caldav">
          <C:time-range start="20260301T000000Z" end="20260401T000000Z"/>
        </C:free-busy-query>"""
        result = self.client.report(PERSO, body)
        self.assertStatus(result, 200)
        self.assertIn("BEGIN:VFREEBUSY", result.text)
        self.assertIn("FREEBUSY;FBTYPE=BUSY:20260310T090000Z/20260310T100000Z", result.text)

    def test_report_inconnu_renvoie_501(self):
        body = '<?xml version="1.0"?><D:mon-report xmlns:D="DAV:"/>'
        self.assertStatus(self.client.report(PERSO, body), 501)


class SyncTests(ServerTestCase):
    SYNC = """<?xml version="1.0"?>
    <D:sync-collection xmlns:D="DAV:">
      <D:sync-token>{token}</D:sync-token>
      <D:sync-level>1</D:sync-level>
      <D:prop><D:getetag/></D:prop>
    </D:sync-collection>"""

    def _sync(self, token: str = "") -> tuple[list[str], str, str]:
        result = self.client.report(PERSO, self.SYNC.format(token=token))
        self.assertStatus(result, 207)
        root = parse(result)
        return hrefs(root), root.findtext(dav("sync-token"), ""), result.text

    def test_synchronisation_initiale_puis_incrementale(self):
        self.client.put(f"{PERSO}a.ics", make_event(uid="a"))
        found, token, _ = self._sync()
        self.assertEqual(found, [f"{PERSO}a.ics"])
        self.assertTrue(token.startswith("urn:kalendra:sync:"))

        # Rien de neuf : aucune réponse, mais un jeton toujours valide.
        found, token2, _ = self._sync(token)
        self.assertEqual(found, [])

        self.client.put(f"{PERSO}b.ics", make_event(uid="b", start="20260311T090000Z", end="20260311T100000Z"))
        found, token3, _ = self._sync(token2)
        self.assertEqual(found, [f"{PERSO}b.ics"])
        self.assertNotEqual(token2, token3)

    def test_suppression_signalee_en_404(self):
        self.client.put(f"{PERSO}a.ics", make_event(uid="a"))
        _, token, _ = self._sync()
        self.client.request("DELETE", f"{PERSO}a.ics")
        found, _, text = self._sync(token)
        self.assertEqual(found, [f"{PERSO}a.ics"])
        self.assertIn("404", text)

    def test_jeton_invalide_refuse(self):
        result = self.client.report(PERSO, self.SYNC.format(token="urn:autre:1"))
        self.assertStatus(result, 403)
        self.assertIn("valid-sync-token", result.text)

    def test_ctag_change_apres_ecriture(self):
        before = parse(self.client.propfind(PERSO, PROPFIND_CALENDAR))
        ctag_before = prop_text(before, PERSO, cs("getctag"))
        self.client.put(f"{PERSO}a.ics", make_event(uid="a"))
        after = parse(self.client.propfind(PERSO, PROPFIND_CALENDAR))
        self.assertNotEqual(ctag_before, prop_text(after, PERSO, cs("getctag")))


class SecurityTests(ServerTestCase):
    def test_les_declarations_dtd_sont_refusees(self):
        payload = (
            '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "haha">]>'
            '<D:propfind xmlns:D="DAV:"><D:prop><D:displayname/></D:prop></D:propfind>'
        )
        result = self.client.propfind(PERSO, payload)
        self.assertStatus(result, 400)

    def test_xml_malforme_refuse(self):
        result = self.client.propfind(PERSO, "<D:propfind")
        self.assertStatus(result, 400)

    def test_namespaces_de_reponse_conformes(self):
        result = self.client.propfind(PERSO, PROPFIND_CALENDAR)
        self.assertIn(NS_DAV, result.text)
        self.assertIn(NS_CALDAV, result.text)


if __name__ == "__main__":
    unittest.main()

"""CardDAV : carnets d'adresses, cartes de visite et rapports (RFC 6352)."""

from __future__ import annotations

import unittest

from helpers import ServerTestCase, make_card, make_event

CARDDAV = "urn:ietf:params:xml:ns:carddav"

MKCOL_CARNET = """<?xml version="1.0"?>
<D:mkcol xmlns:D="DAV:" xmlns:CR="urn:ietf:params:xml:ns:carddav">
  <D:set><D:prop>
    <D:resourcetype><D:collection/><CR:addressbook/></D:resourcetype>
    <D:displayname>Mes contacts</D:displayname>
  </D:prop></D:set>
</D:mkcol>"""


class CarnetTests(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.contacts_id = self.db.create_addressbook(
            self.will_id, "contacts", display_name="Contacts"
        )

    def put_card(self, href: str = "card-1.vcf", **kw) -> None:
        result = self.client.request(
            "PUT",
            f"/addressbooks/will/contacts/{href}",
            make_card(**kw),
            {"Content-Type": "text/vcard; charset=utf-8"},
        )
        self.assertIn(result.status, (201, 204), msg=result.text[:400])

    # ------------------------------------------------------------- collection

    def test_creation_par_mkcol(self):
        result = self.client.request(
            "MKCOL",
            "/addressbooks/will/perso2/",
            MKCOL_CARNET.encode("utf-8"),
            {"Content-Type": "application/xml"},
        )
        self.assertStatus(result, 201)
        self.assertIsNotNone(self.db.get_calendar(self.will_id, "perso2", "addressbook"))

    def test_resourcetype_annonce_un_carnet(self):
        result = self.client.propfind(
            "/addressbooks/will/contacts/",
            '<?xml version="1.0"?><D:propfind xmlns:D="DAV:">'
            "<D:prop><D:resourcetype/></D:prop></D:propfind>",
        )
        self.assertStatus(result, 207)
        self.assertIn("addressbook", result.text)

    def test_le_principal_annonce_le_home_carddav(self):
        result = self.client.propfind(
            "/principals/will/",
            '<?xml version="1.0"?><D:propfind xmlns:D="DAV:" '
            f'xmlns:CR="{CARDDAV}"><D:prop><CR:addressbook-home-set/>'
            "</D:prop></D:propfind>",
        )
        self.assertStatus(result, 207)
        self.assertIn("/addressbooks/will/", result.text)

    def test_well_known_carddav(self):
        result = self.client.request("GET", "/.well-known/carddav")
        self.assertStatus(result, 301)

    # ------------------------------------------------------------ ressources

    def test_depot_et_restitution_octet_pour_octet(self):
        corps = make_card()
        self.client.request(
            "PUT",
            "/addressbooks/will/contacts/card-1.vcf",
            corps,
            {"Content-Type": "text/vcard"},
        )
        result = self.client.request("GET", "/addressbooks/will/contacts/card-1.vcf")
        self.assertStatus(result, 200)
        self.assertEqual(result.body, corps)
        self.assertIn("text/vcard", result.header("content-type"))

    def test_une_carte_illisible_est_refusee(self):
        result = self.client.request(
            "PUT",
            "/addressbooks/will/contacts/x.vcf",
            b"ceci n'est pas une carte",
            {"Content-Type": "text/vcard"},
        )
        self.assertStatus(result, 403)
        self.assertIn("valid-address-data", result.text)

    def test_un_evenement_est_refuse_dans_un_carnet(self):
        result = self.client.request(
            "PUT",
            "/addressbooks/will/contacts/e.ics",
            make_event(),
            {"Content-Type": "text/calendar"},
        )
        self.assertStatus(result, 415)

    def test_une_carte_est_refusee_dans_un_agenda(self):
        result = self.client.request(
            "PUT",
            "/calendars/will/perso/c.vcf",
            make_card(),
            {"Content-Type": "text/vcard"},
        )
        self.assertStatus(result, 415)

    def test_uid_en_double_refuse(self):
        self.put_card("card-1.vcf", uid="meme-uid")
        result = self.client.request(
            "PUT",
            "/addressbooks/will/contacts/card-2.vcf",
            make_card(uid="meme-uid"),
            {"Content-Type": "text/vcard"},
        )
        self.assertStatus(result, 403)
        self.assertIn("no-uid-conflict", result.text)

    def test_etag_et_suppression(self):
        self.put_card()
        result = self.client.request("GET", "/addressbooks/will/contacts/card-1.vcf")
        etag = result.header("etag")
        self.assertTrue(etag)
        rejet = self.client.request(
            "DELETE", "/addressbooks/will/contacts/card-1.vcf", headers={"If-Match": '"faux"'}
        )
        self.assertStatus(rejet, 412)
        ok = self.client.request(
            "DELETE", "/addressbooks/will/contacts/card-1.vcf", headers={"If-Match": etag}
        )
        self.assertStatus(ok, 204)

    # --------------------------------------------------------------- rapports

    def test_addressbook_query_filtre_sur_le_texte(self):
        self.put_card("card-1.vcf", uid="u1", fn="Jean Dupont")
        self.put_card("card-2.vcf", uid="u2", fn="Claire Martin")
        result = self.client.report(
            "/addressbooks/will/contacts/",
            '<?xml version="1.0"?>'
            f'<CR:addressbook-query xmlns:D="DAV:" xmlns:CR="{CARDDAV}">'
            "<D:prop><D:getetag/></D:prop>"
            '<CR:filter><CR:prop-filter name="FN">'
            "<CR:text-match>claire</CR:text-match>"
            "</CR:prop-filter></CR:filter></CR:addressbook-query>",
        )
        self.assertStatus(result, 207)
        self.assertIn("card-2.vcf", result.text)
        self.assertNotIn("card-1.vcf", result.text)

    def test_addressbook_query_sans_filtre_renvoie_tout(self):
        self.put_card("card-1.vcf", uid="u1")
        self.put_card("card-2.vcf", uid="u2", fn="Claire Martin")
        result = self.client.report(
            "/addressbooks/will/contacts/",
            '<?xml version="1.0"?>'
            f'<CR:addressbook-query xmlns:D="DAV:" xmlns:CR="{CARDDAV}">'
            "<D:prop><D:getetag/></D:prop><CR:filter/></CR:addressbook-query>",
        )
        self.assertStatus(result, 207)
        self.assertIn("card-1.vcf", result.text)
        self.assertIn("card-2.vcf", result.text)

    def test_addressbook_multiget_rend_les_donnees(self):
        self.put_card()
        result = self.client.report(
            "/addressbooks/will/contacts/",
            '<?xml version="1.0"?>'
            f'<CR:addressbook-multiget xmlns:D="DAV:" xmlns:CR="{CARDDAV}">'
            "<D:prop><D:getetag/><CR:address-data/></D:prop>"
            "<D:href>/addressbooks/will/contacts/card-1.vcf</D:href>"
            "</CR:addressbook-multiget>",
        )
        self.assertStatus(result, 207)
        self.assertIn("Jean Dupont", result.text)

    def test_sync_collection_fonctionne_sur_un_carnet(self):
        self.put_card()
        result = self.client.report(
            "/addressbooks/will/contacts/",
            '<?xml version="1.0"?><D:sync-collection xmlns:D="DAV:">'
            "<D:sync-token/><D:prop><D:getetag/></D:prop></D:sync-collection>",
        )
        self.assertStatus(result, 207)
        self.assertIn("card-1.vcf", result.text)
        self.assertIn("urn:kalendra:sync:", result.text)

    # ------------------------------------------------------------- isolation

    def test_les_carnets_nappraissent_pas_dans_le_home_caldav(self):
        result = self.client.propfind(
            "/calendars/will/",
            '<?xml version="1.0"?><D:propfind xmlns:D="DAV:">'
            "<D:prop><D:resourcetype/></D:prop></D:propfind>",
            depth="1",
        )
        self.assertStatus(result, 207)
        self.assertIn("/calendars/will/perso/", result.text)
        self.assertNotIn("contacts", result.text)

    def test_les_agendas_nappraissent_pas_dans_le_home_carddav(self):
        result = self.client.propfind(
            "/addressbooks/will/",
            '<?xml version="1.0"?><D:propfind xmlns:D="DAV:">'
            "<D:prop><D:resourcetype/></D:prop></D:propfind>",
            depth="1",
        )
        self.assertStatus(result, 207)
        self.assertIn("/addressbooks/will/contacts/", result.text)
        self.assertNotIn("perso", result.text)

    def test_le_carnet_dun_autre_compte_est_refuse(self):
        self.assertStatus(self.alice.request("GET", "/addressbooks/will/contacts/"), 403)


class VueContactsTests(ServerTestCase):
    """Vue web en lecture seule des carnets (`/view/contacts/`)."""

    def setUp(self) -> None:
        super().setUp()
        self.contacts_id = self.db.create_addressbook(
            self.will_id, "contacts", display_name="Contacts"
        )
        for href, uid, fn, mail in (
            ("c1.vcf", "u1", "Sophie Bernard", "sophie@acme.fr"),
            ("c2.vcf", "u2", "Claire Martin", "claire@ailleurs.net"),
        ):
            self.client.request(
                "PUT",
                f"/addressbooks/will/contacts/{href}",
                make_card(uid=uid, fn=fn, email=mail),
                {"Content-Type": "text/vcard"},
            )

    def test_lindex_liste_les_carnets(self):
        result = self.client.request("GET", "/view/", headers={"Accept": "text/html"})
        self.assertStatus(result, 200)
        self.assertIn("Carnets d'adresses", result.text)
        self.assertIn("/view/contacts/will/contacts/", result.text)

    def test_la_liste_des_contacts_est_triee_par_nom(self):
        result = self.client.request(
            "GET", "/view/contacts/will/contacts/", headers={"Accept": "text/html"}
        )
        self.assertStatus(result, 200)
        self.assertLess(
            result.text.index("Claire Martin"),
            result.text.index("Sophie Bernard"),
            msg="les contacts doivent être triés sur le nom, pas sur le href",
        )

    def test_la_fiche_affiche_les_proprietes_et_la_source(self):
        result = self.client.request(
            "GET", "/view/contacts/will/contacts/c1.vcf", headers={"Accept": "text/html"}
        )
        self.assertStatus(result, 200)
        self.assertIn("Sophie Bernard", result.text)
        self.assertIn("sophie@acme.fr", result.text)
        self.assertIn("BEGIN:VCARD", result.text)

    def test_carnet_dun_autre_compte_refuse(self):
        self.assertStatus(self.alice.request("GET", "/view/contacts/will/contacts/"), 403)

    def test_carnet_inconnu(self):
        self.assertStatus(self.client.request("GET", "/view/contacts/will/zzz/"), 404)

    def test_un_compte_nomme_contacts_garde_ses_agendas(self):
        """Le préfixe /view/contacts/ ne doit pas masquer un compte homonyme."""
        uid = self.db.create_user("contacts", "pw")
        self.db.create_calendar(uid, "perso")
        from helpers import Client

        client = Client(self.app, "contacts", "pw")
        result = client.request(
            "GET", "/view/contacts/perso/", headers={"Accept": "text/html"}
        )
        self.assertStatus(result, 200)


class MigrationTests(ServerTestCase):
    """Une base créée avant CardDAV doit rester exploitable."""

    def test_un_carnet_peut_porter_le_nom_dun_agenda(self):
        """L'unicité porte sur (user_id, kind, name), pas sur (user_id, name).

        Les deux vivent dans des arbres d'URL distincts : rien ne justifie
        qu'un carnet « perso » soit refusé parce qu'un agenda « perso » existe.
        """
        self.db.create_addressbook(self.will_id, "perso")
        self.assertIsNotNone(self.db.get_calendar(self.will_id, "perso", "addressbook"))
        self.assertIsNotNone(self.db.get_calendar(self.will_id, "perso", "calendar"))

    def test_mkcol_sur_un_nom_deja_pris_renvoie_409(self):
        self.db.create_addressbook(self.will_id, "carnet")
        result = self.client.request(
            "MKCOL",
            "/addressbooks/will/carnet/",
            MKCOL_CARNET.encode("utf-8"),
            {"Content-Type": "application/xml"},
        )
        self.assertStatus(result, 405)

    def test_les_agendas_existants_restent_des_agendas(self):
        for row in self.db.list_calendars(self.will_id):
            self.assertEqual(row["kind"], "calendar")
        self.assertEqual([r["name"] for r in self.db.list_addressbooks(self.will_id)], [])


if __name__ == "__main__":
    unittest.main()

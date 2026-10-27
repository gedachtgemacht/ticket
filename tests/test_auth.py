import sqlite3
import unittest

from helpers import AppTest, Client, PW, server


class SetupTest(AppTest):
    def test_status_ohne_nutzer_ist_setup(self):
        c = self.client()
        s, d = c.get("/api/status")
        self.assertEqual((s, d["setup"], d["angemeldet"]), (200, True, False))

    def test_setup_braucht_code_und_legt_admin_an(self):
        c = self.client()
        s, d = c.post("/api/setup", {"code": "falsch", "name": "A", "kurz": "AD", "passwort": PW}, csrf=False)
        self.assertEqual(s, 403)
        s, d = c.post("/api/setup", {"code": "test-setup-code", "name": "Alex", "kurz": "ad", "passwort": "kurz"}, csrf=False)
        self.assertEqual(s, 400)   # Passwort zu kurz
        s, d = c.post("/api/setup", {"code": "test-setup-code", "name": "Alex", "kurz": "ad", "passwort": PW}, csrf=False)
        self.assertEqual(s, 201)
        s, d = c.laden()
        self.assertEqual((d["ich"]["kurz"], d["ich"]["rolle"]), ("AD", "admin"))
        # danach ist das Setup zu
        s, d = self.client().post("/api/setup", {"code": "test-setup-code", "name": "X", "kurz": "XX", "passwort": PW}, csrf=False)
        self.assertEqual(s, 409)
        self.assertFalse(self.client().get("/api/status")[1]["setup"])

    def test_passwort_wird_mit_scrypt_gespeichert(self):
        self.admin()
        pw = self.db().execute("SELECT pw FROM nutzer").fetchone()[0]
        self.assertTrue(pw.startswith("scrypt$"))
        self.assertNotIn(PW, pw)


class LoginTest(AppTest):
    def setUp(self):
        super().setUp()
        self.a = self.admin()

    def test_ohne_login_kein_zugriff(self):
        c = self.client()
        for pfad in ("/api/stand", "/api/tickets/TKT-1", "/api/export.csv", "/api/kennzahlen", "/bericht"):
            s, _ = c.get(pfad)
            self.assertEqual(s, 401, pfad)
        s, _ = c.post("/api/tickets", {"titel": "x"}, csrf=False)
        self.assertEqual(s, 401)

    def test_login_logout_cookie(self):
        c = self.client()
        s, d = c.post("/api/login", {"kurz": "ad", "passwort": "falsch-falsch"}, csrf=False)
        self.assertEqual(s, 401)
        s, d = c.post("/api/login", {"kurz": "ad", "passwort": PW}, csrf=False)
        self.assertEqual(s, 200)
        sc = [v for k, v in c.rohe_header if k.lower() == "set-cookie"][0]
        self.assertIn("HttpOnly", sc)
        self.assertIn("SameSite=Lax", sc)
        self.assertIn(f"Max-Age={90 * 86400}", sc)
        self.assertNotIn("Secure", sc)   # lokal ohne TLS
        c.laden()
        self.assertEqual(c.get("/api/stand")[0], 200)
        self.assertEqual(c.post("/api/logout")[0], 200)
        self.assertEqual(c.get("/api/stand")[0], 401)

    def test_secure_flag_hinter_https_proxy(self):
        c = self.client()
        c.post("/api/login", {"kurz": "AD", "passwort": PW}, csrf=False, headers={"X-Forwarded-Proto": "https"})
        sc = [v for k, v in c.rohe_header if k.lower() == "set-cookie"][0]
        self.assertIn("Secure", sc)

    def test_session_token_nur_als_hash_in_db(self):
        token = self.a.cookies["ticket_session"]
        rows = self.db().execute("SELECT token FROM sitzung").fetchall()
        self.assertNotIn(token, [r[0] for r in rows])

    def test_fehlversuche_werden_gedrosselt(self):
        c = self.client()
        codes = [c.post("/api/login", {"kurz": "AD", "passwort": "falsch-falsch"}, csrf=False)[0] for _ in range(9)]
        self.assertEqual(codes[:8], [401] * 8)
        self.assertEqual(codes[8], 429)
        # auch das richtige Passwort wird in der Sperre abgewiesen
        self.assertEqual(c.post("/api/login", {"kurz": "AD", "passwort": PW}, csrf=False)[0], 429)

    def test_csrf_pflicht_fuer_schreibende_requests(self):
        a = self.a
        s, d = a.post("/api/tickets", {"titel": "ohne Token"}, csrf=False)
        self.assertEqual(s, 403)
        s, d = a.post("/api/tickets", {"titel": "falsches Token"}, csrf=False, headers={"X-CSRF-Token": "x" * 20})
        self.assertEqual(s, 403)
        s, d = a.post("/api/tickets", {"titel": "mit Token"})
        self.assertEqual(s, 201)
        # Lesen braucht kein Token
        self.assertEqual(a.get("/api/stand")[0], 200)

    def test_fremde_herkunft_und_formulare_abgelehnt(self):
        a = self.a
        s, _ = a.post("/api/tickets", {"titel": "x"}, headers={"Origin": "https://boese.example"})
        self.assertEqual(s, 403)
        s, _ = a.post("/api/tickets", {"titel": "x"}, headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(s, 201)
        s, _ = a.req("POST", "/api/tickets", roh=b"titel=x", ctype="application/x-www-form-urlencoded")
        self.assertEqual(s, 415)   # kein JSON -> kein einfacher Cross-Site-Request

    def test_csrf_gilt_je_sitzung(self):
        b = self.client()
        b.login("AD")
        self.assertNotEqual(b.csrf, self.a.csrf)
        s, _ = b.post("/api/tickets", {"titel": "x"}, headers={"X-CSRF-Token": self.a.csrf})
        self.assertEqual(s, 403)

    def test_passwort_aendern_beendet_andere_sitzungen(self):
        zweit = self.client()
        zweit.login("AD")
        s, d = self.a.post("/api/passwort", {"alt": "falsch", "neu": "ein-ganz-neues-pw"})
        self.assertEqual(s, 403)
        s, d = self.a.post("/api/passwort", {"alt": PW, "neu": "ein-ganz-neues-pw"})
        self.assertEqual(s, 200)
        self.assertEqual(zweit.get("/api/stand")[0], 401)
        self.assertEqual(self.a.get("/api/stand")[0], 200)
        self.assertEqual(self.client().login("AD", PW)[0], 401)
        self.assertEqual(self.client().login("AD", "ein-ganz-neues-pw")[0], 200)


class EinladungTest(AppTest):
    def setUp(self):
        super().setUp()
        self.a = self.admin()

    def test_nur_admin_laedt_ein(self):
        m = self.mitglied(self.a, "BE")
        s, _ = m.post("/api/einladungen", {"rolle": "mitglied"})
        self.assertEqual(s, 403)

    def test_einladung_einmalig_mit_ablauf(self):
        s, d = self.a.post("/api/einladungen", {"rolle": "mitglied"})
        self.assertTrue(d["pfad"].startswith("/#/einladung/"))
        token = d["token"]
        c = self.client()
        self.assertEqual(c.get("/api/einladung/" + token)[1], {"gueltig": True, "nutzer": None})
        s, _ = c.post("/api/einladung/" + token, {"name": "Bea", "kurz": "BE", "passwort": "kurz"}, csrf=False)
        self.assertEqual(s, 400)
        s, _ = c.post("/api/einladung/" + token, {"name": "Bea", "kurz": "B", "passwort": PW}, csrf=False)
        self.assertEqual(s, 400)
        s, _ = c.post("/api/einladung/" + token, {"name": "Bea", "kurz": "be", "passwort": PW}, csrf=False)
        self.assertEqual(s, 201)
        c.laden()
        self.assertEqual((c.get("/api/stand")[1]["ich"]["kurz"], c.get("/api/stand")[1]["ich"]["rolle"]), ("BE", "mitglied"))
        # zweite Nutzung schlaegt fehl
        self.assertEqual(self.client().get("/api/einladung/" + token)[0], 404)
        self.assertEqual(self.client().post("/api/einladung/" + token, {"name": "X", "kurz": "XX", "passwort": PW}, csrf=False)[0], 404)
        # in der DB steht nur der Hash
        self.assertNotIn(token, [r[0] for r in self.db().execute("SELECT token FROM einladung")])

    def test_abgelaufene_einladung(self):
        s, d = self.a.post("/api/einladungen", {"rolle": "mitglied"})
        with self.db() as c:
            c.execute("UPDATE einladung SET ablauf='2000-01-01T00:00:00'")
        self.assertEqual(self.client().get("/api/einladung/" + d["token"])[0], 404)

    def test_doppeltes_kuerzel(self):
        self.mitglied(self.a, "BE")
        s, d = self.a.post("/api/einladungen", {"rolle": "mitglied"})
        s, _ = self.client().post("/api/einladung/" + d["token"], {"name": "Bert", "kurz": "BE", "passwort": PW}, csrf=False)
        self.assertEqual(s, 409)

    def test_einladung_fuer_vorhandene_person_ohne_passwort(self):
        with self.db() as c:
            c.execute("INSERT INTO nutzer(kurz,name,angelegt) VALUES('CH','Chris','2026-01-01T00:00:00')")
        s, d = self.a.post("/api/einladungen", {"kurz": "CH"})
        c = self.client()
        self.assertEqual(c.get("/api/einladung/" + d["token"])[1]["nutzer"], {"kurz": "CH", "name": "Chris"})
        s, _ = c.post("/api/einladung/" + d["token"], {"name": "Chris C", "kurz": "ZZ", "passwort": PW}, csrf=False)
        self.assertEqual(s, 201)
        c.laden()
        self.assertEqual(c.get("/api/stand")[1]["ich"]["kurz"], "CH")   # Kuerzel bleibt das alte

    def test_deaktivieren_beendet_sitzung_und_login(self):
        m = self.mitglied(self.a, "BE")
        s, _ = self.a.patch("/api/nutzer/BE", {"aktiv": False})
        self.assertEqual(s, 200)
        self.assertEqual(m.get("/api/stand")[0], 401)
        self.assertEqual(self.client().login("BE")[0], 401)

    def test_letzter_admin_bleibt(self):
        s, d = self.a.patch("/api/nutzer/AD", {"rolle": "mitglied"})
        self.assertEqual(s, 409)
        s, d = self.a.patch("/api/nutzer/AD", {"aktiv": False})
        self.assertEqual(s, 409)


class TokenTest(AppTest):
    def test_einstellungen_nur_admin_und_token_rotation(self):
        a = self.admin()
        m = self.mitglied(a, "BE")
        self.assertEqual(m.get("/api/einstellungen")[0], 403)
        s, d = a.get("/api/einstellungen")
        self.assertEqual((d["wip_arbeit"], d["wip_review"], d["hook_token"], d["bericht_token"]), (3, 5, None, None))
        s, d1 = a.post("/api/einstellungen/token/hook")
        s, d2 = a.post("/api/einstellungen/token/hook")
        self.assertNotEqual(d1["hook_token"], d2["hook_token"])
        self.assertEqual(a.get("/api/einstellungen")[1]["hook_token"], d2["hook_token"])
        s, d = a.patch("/api/einstellungen", {"wip_arbeit": 0})
        self.assertEqual(s, 400)
        s, d = a.patch("/api/einstellungen", {"wip_arbeit": 2, "stillstand_tage": 5})
        self.assertEqual((d["wip_arbeit"], d["stillstand_tage"]), (2, 5))


if __name__ == "__main__":
    unittest.main()

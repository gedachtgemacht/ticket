"""Regressionstests aus der fachlichen und technischen Pruefung vor der Veroeffentlichung.

Jeder Test hier hat vorher einen echten Fehler gezeigt (siehe Docstrings).
"""
import socket
import sqlite3
import threading
import time
import unittest

from helpers import AppTest, PW, server


def roh_anfrage(port, daten, warten=3.0):
    """Schickt rohe Bytes an den Server und gibt die Statuszeile zurueck."""
    s = socket.create_connection(("127.0.0.1", port), timeout=warten)
    try:
        s.sendall(daten)
        out = b""
        try:
            while True:
                x = s.recv(65536)
                if not x:
                    break
                out += x
        except socket.timeout:
            return None
        return out.split(b"\r\n")[0].decode("latin-1")
    finally:
        s.close()


class VertraulichLeseTest(AppTest):
    """Vertraulich-Sichtbarkeit auf Nebenpfaden (Kundenliste, Meldungszaehler, Sichtverlust)."""

    def setUp(self):
        super().setUp()
        self.a = self.admin()
        self.e = self.mitglied(self.a, "ER", "Erika")
        self.f = self.mitglied(self.a, "FR", "Frieda")
        self.b = self.mitglied(self.a, "BO", "Bob")

    def test_kundenliste_verraet_keine_vertraulichen_kunden(self):
        """Der Kunde eines vertraulichen Tickets stand in 'kunden' (Vorschlagsliste) fuer alle."""
        self.ticket(self.e, "Insolvenz melden", kunde="Mueller Insolvenz GmbH", vertraulich=True)
        self.assertIn("Mueller Insolvenz GmbH", self.e.get("/api/stand")[1]["kunden"])
        self.assertIn("Mueller Insolvenz GmbH", self.a.get("/api/stand")[1]["kunden"])
        self.assertNotIn("Mueller Insolvenz GmbH", self.f.get("/api/stand")[1]["kunden"])
        # kein Kunde aus dem Titel ...
        self.assertIsNone(self.ticket(self.f, "Mueller Insolvenz GmbH: Anruf")["kunde"])
        # ... und kein Orakel ueber die kanonische Schreibweise
        d = self.ticket(self.f, "x", kunde="mueller insolvenz gmbh")
        self.assertEqual(d["kunde"], "mueller insolvenz gmbh")
        # wird das Ticket offen, taucht der Kunde auf
        self.e.patch("/api/tickets/TKT-1", {"vertraulich": False})
        self.assertIn("Mueller Insolvenz GmbH", self.f.get("/api/stand")[1]["kunden"])

    def test_meldungszaehler_zaehlt_nur_sichtbare_tickets(self):
        """Zaehler an der Glocke zaehlte Meldungen zu geloeschten/unsichtbaren Tickets mit, die Liste nicht."""
        self.ticket(self.e, "Eins", wer="FR")
        self.ticket(self.e, "Zwei", wer="FR")
        self.assertEqual(self.f.get("/api/stand")[1]["ungelesen_meldungen"], 2)
        self.a.delete("/api/tickets/TKT-2")                                    # geloescht
        self.e.patch("/api/tickets/TKT-1", {"wer": "BO", "vertraulich": True})  # FR verliert die Sicht
        s, d = self.f.get("/api/stand")
        self.assertEqual(d["ungelesen_meldungen"], 0)
        self.assertEqual(self.f.get("/api/benachrichtigungen?nur=ungelesen")[1]["meldungen"], [])
        self.assertEqual(self.f.post("/api/benachrichtigungen/lesen", {"id": 1})[1]["ungelesen"], 0)

    def test_sichtverlust_beim_aendern_gibt_klare_meldung(self):
        """Wer sich durch die Aenderung selbst aussperrt, bekam ein irrefuehrendes 404 'gibt es nicht'."""
        self.ticket(self.e, "Heikel", wer="FR", vertraulich=True)
        s, d = self.f.patch("/api/tickets/TKT-1", {"wer": "BO"})
        self.assertEqual(s, 409, d)
        self.assertIn("Beobachter", d["fehler"])
        self.assertEqual(self.a.get("/api/tickets/TKT-1")[1]["wer"], "FR")   # nichts geaendert
        # mit Beobachter-Eintrag in derselben Anfrage geht es
        s, d = self.f.patch("/api/tickets/TKT-1", {"wer": "BO", "beobachter": ["FR"]})
        self.assertEqual((s, d["wer"]), (200, "BO"))
        # Unbeteiligte koennen ein offenes Ticket nicht vertraulich stellen und sich dabei aussperren
        self.ticket(self.e, "Offen")
        s, d = self.f.patch("/api/tickets/TKT-2", {"vertraulich": True})
        self.assertEqual(s, 409, d)
        self.assertEqual(self.a.get("/api/tickets/TKT-2")[1]["vertraulich"], False)


class SitzungDrosselTest(AppTest):
    def setUp(self):
        super().setUp()
        self.a = self.admin()

    def test_zugang_zuruecksetzen_beendet_alte_sitzungen(self):
        """Ueber einen Einladungslink fuer eine vorhandene Person blieb die alte Sitzung (und Angreifer) gueltig."""
        m = self.mitglied(self.a, "BE")
        zweit = self.client()
        zweit.login("BE")
        s, d = self.a.post("/api/einladungen", {"kurz": "BE"})
        neu = self.client()
        self.assertEqual(neu.post("/api/einladung/" + d["token"], {"name": "Bea", "passwort": "ganz-neues-pw-1"}, csrf=False)[0], 201)
        self.assertEqual(m.get("/api/stand")[0], 401)
        self.assertEqual(zweit.get("/api/stand")[0], 401)
        self.assertEqual(neu.laden()[0], 200)                       # die neue Sitzung gilt
        self.assertEqual(self.client().login("BE", PW)[0], 401)     # altes Passwort ist weg

    def test_x_real_ip_zaehlt_nur_mit_proxy_konfiguration(self):
        """X-Real-IP wurde immer geglaubt: wer den Header selbst setzen konnte, umging die Login-Drossel."""
        self.assertFalse(server.TRUST_PROXY)
        c = self.client()
        codes = [c.post("/api/login", {"kurz": "AD", "passwort": "falsch-falsch"}, csrf=False,
                        headers={"X-Real-IP": f"10.0.0.{i}"})[0] for i in range(10)]
        self.assertEqual(codes[:8], [401] * 8)
        self.assertEqual(codes[8:], [429, 429])
        # hinter dem eigenen Proxy (TICKET_TRUST_PROXY=1) gilt der Header
        server.TRUST_PROXY = True
        try:
            server.zustand_zuruecksetzen()
            codes = [c.post("/api/login", {"kurz": "AD", "passwort": "falsch-falsch"}, csrf=False,
                            headers={"X-Real-IP": f"10.0.0.{i % 2}"})[0] for i in range(10)]
            self.assertEqual(codes[:2], [401, 401])
            self.assertEqual(codes[-1], 401)       # zwei verschiedene Adressen, je nur 5 Fehlversuche
        finally:
            server.TRUST_PROXY = False
            server.zustand_zuruecksetzen()

    def test_drossel_speicher_waechst_nicht_unbegrenzt(self):
        """Jeder lange, immer neue Kuerzel-Wert blieb als Schluessel (bis 64 KB) fuer immer im Speicher."""
        c = self.client()
        c.post("/api/login", {"kurz": "X" * 30000, "passwort": "x"}, csrf=False)
        self.assertTrue(all(len(k) <= 300 for k in server.LOGIN_DROSSEL.d))
        d = server.Drossel(3, 0.05)
        d.max_schluessel = 100
        for i in range(6000):
            d.fehlschlag(f"k{i}")
            if i % 500 == 499:
                time.sleep(0.06)    # alles bisher ist abgelaufen
        self.assertLess(len(d.d), 700)


class RobustheitTest(AppTest):
    """Kaputte Eingaben duerfen nie zu 500 fuehren."""

    def setUp(self):
        super().setUp()
        self.a = self.admin()

    def test_nicht_ascii_in_csrf_kopf_und_setup_code(self):
        """hmac.compare_digest warf bei Nicht-ASCII einen TypeError (500, Stacktrace im Log)."""
        s, d = self.a.post("/api/tickets", {"titel": "x"}, headers={"X-CSRF-Token": "äöü"})
        self.assertEqual(s, 403, d)
        # frische Datenbank ohne Admin fuer den Setup-Code
        tmp = self.tmp / "zweit.db"
        alt = server.DB
        server.DB = tmp
        try:
            server.init_db()
            c = self.client()
            s, d = c.post("/api/setup", {"code": "äöü", "name": "A", "kurz": "AD", "passwort": PW}, csrf=False)
            self.assertEqual(s, 403, d)
        finally:
            server.DB = alt

    def test_content_length_kaputt(self):
        """Nicht-numerische Content-Length gab 500, negative wurde nicht sauber abgewiesen."""
        for cl in ("abc", "1e3", "-5"):
            roh = (f"POST /api/login HTTP/1.0\r\nHost: x\r\nContent-Type: application/json\r\n"
                   f"Content-Length: {cl}\r\n\r\n{{}}").encode()
            self.assertEqual(roh_anfrage(self.port, roh), "HTTP/1.0 400 Bad Request", cl)
        # chunked wuerde sonst als leerer Body durchgehen (Webhook: stiller Erfolg ohne Wirkung)
        roh = (b"POST /api/login HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
               b"Transfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n")
        self.assertEqual(roh_anfrage(self.port, roh), "HTTP/1.0 411 Length Required")

    def test_ungueltige_zeichen_und_zahlen_sind_400_nie_500(self):
        """Lone Surrogates, riesige Ganzzahlen, 5000 Ziffern in der Suche, Datum ausserhalb des Bereichs."""
        c = self.client()
        s, d = c.req("POST", "/api/login", roh=b'{"kurz":"\\ud800","passwort":"x"}', csrf=False)
        self.assertEqual(s, 400, d)
        self.assertEqual(self.a.req("POST", "/api/tickets", roh=b'{"titel":"\\ud800"}')[0], 400)
        self.assertEqual(self.a.req("POST", "/api/tickets", roh=b'{"titel":"ok","kunde":"\\ud800"}')[0], 400)
        self.ticket(self.a, "T")
        self.assertEqual(self.a.get("/api/woche?von=9999-12-31")[0], 400)
        self.assertEqual(self.a.get("/api/tickets/TKT-99999999999999999999")[0], 400)
        self.assertEqual(self.a.get("/api/suche?projekt=99999999999999999999")[0], 400)
        self.assertEqual(self.a.patch("/api/tickets/TKT-1", {"vor": 10 ** 30})[0], 400)
        self.assertEqual(self.a.patch("/api/tickets/TKT-1", {"projekt": 10 ** 30})[0], 400)
        self.assertEqual(self.a.get("/api/suche?q=" + "9" * 5000)[0], 200)
        # tief verschachteltes JSON
        self.assertEqual(self.a.req("POST", "/api/tickets", roh=b"[" * 30000 + b"]" * 30000)[0], 400)

    def test_stockender_body_haelt_das_schreibschloss_nicht(self):
        """Der Body wurde unter dem globalen Schreibschloss gelesen: ein Client, der nach den Kopfzeilen schweigt,
        fror alle Schreibzugriffe ein (ohne puffernden Proxy)."""
        s = socket.create_connection(("127.0.0.1", self.port))
        try:
            s.sendall(b"POST /api/login HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                      b"Content-Length: 100\r\n\r\n{")
            time.sleep(0.3)
            c = self.client()
            c.cookies = dict(self.a.cookies)
            c.csrf = self.a.csrf
            t0 = time.time()
            erg = []
            th = threading.Thread(target=lambda: erg.append(c.post("/api/tickets", {"titel": "geht"})[0]), daemon=True)
            th.start()
            th.join(4)
            self.assertEqual(erg, [201], "Schreibzugriff blockiert")
            self.assertLess(time.time() - t0, 4)
        finally:
            s.close()

    def test_server_kopf_ohne_python_version(self):
        c = self.client()
        self.assertEqual(c.get("/api/status")[0], 200)
        kopf = {k.lower(): v for k, v in c.rohe_header}
        self.assertEqual(kopf["server"], "ticket")

    def test_fehlertext_ohne_sql_details(self):
        """IntegrityError-Texte (Tabellen-/Spaltennamen) gingen im Klartext an den Client."""
        def kaputt(ctx):
            ctx.c.execute("INSERT INTO nutzer(kurz,name,angelegt) VALUES('ZZ','x','2020-01-01T00:00:00')")
            ctx.c.execute("INSERT INTO nutzer(kurz,name,angelegt) VALUES('ZZ','x','2020-01-01T00:00:00')")
        server.ROUTEN.append(("POST", server.re.compile("^/api/_test_integritaet$"), kaputt, "nutzer", True))
        try:
            s, d = self.a.post("/api/_test_integritaet")
        finally:
            server.ROUTEN.pop()
        self.assertEqual(s, 409)
        self.assertNotIn("nutzer", d["fehler"])
        self.assertNotIn("UNIQUE", d["fehler"])


class ZustandPruefungTest(AppTest):
    def setUp(self):
        super().setUp()
        self.a = self.admin()
        self.m = self.mitglied(self.a, "BE", "Bea")

    def test_praesenz_modus_nicht_ausgeliefert_wenn_abgeschaltet(self):
        """'modus' (abwesend/nicht stoeren) ging trotz abgeschalteter Praesenz an alle."""
        self.m.patch("/api/ich/status", {"praesenz": "nicht_stoeren"})
        lookup = lambda c, k: [n for n in c.get("/api/stand")[1]["nutzer"] if n["kurz"] == k][0]
        self.assertEqual(lookup(self.a, "BE")["modus"], "nicht_stoeren")
        self.a.patch("/api/einstellungen", {"praesenz_anzeigen": False})
        self.assertEqual(lookup(self.a, "BE")["modus"], "auto")
        self.assertIsNone(lookup(self.a, "BE")["praesenz"])
        self.assertEqual(self.m.get("/api/stand")[1]["ich"]["modus"], "nicht_stoeren")   # die eigene Wahl sieht man weiter
        self.assertEqual(self.m.patch("/api/ich/status", {"praesenz": "abwesend"})[1]["modus"], "abwesend")

    def test_blockiert_mit_leerem_grund_hebt_auf(self):
        """Ein Grund nur aus Leerzeichen setzte 'blockiert' mit leerem Text (und ist:blockiert fand es)."""
        self.ticket(self.m, "T")
        s, d = self.m.patch("/api/tickets/TKT-1", {"blockiert_grund": "   "})
        self.assertEqual(s, 200)
        self.assertIsNone(d["blockiert"])
        self.assertIsNone(d["blockiert_seit"])
        self.assertEqual(self.m.get("/api/suche?q=ist:blockiert")[1]["gesamt"], 0)
        s, d = self.m.patch("/api/tickets/TKT-1", {"blockiert_grund": " wartet auf Lager "})
        self.assertEqual(d["blockiert"], "wartet auf Lager")
        s, d = self.m.patch("/api/tickets/TKT-1", {"blockiert_grund": "  "})
        self.assertIsNone(d["blockiert"])

    def test_wiederherstellen_in_geloeschtem_projekt_landet_in_allgemein(self):
        """Das Ticket blieb in einem geloeschten Projekt haengen und fehlte in jeder Projektliste."""
        s, p = self.a.post("/api/projekte", {"titel": "Altlast", "key": "ALT"})
        t = self.ticket(self.a, "T", projekt=p["id"])
        self.a.delete("/api/tickets/TKT-1")
        self.assertEqual(self.a.delete(f"/api/projekte/{p['id']}")[0], 200)
        s, d = self.a.post("/api/tickets/TKT-1/wiederherstellen")
        self.assertEqual(s, 200)
        self.assertEqual(d["projekt_titel"], "Allgemein")
        projekte = {x["id"] for x in self.a.get("/api/stand")[1]["projekte"]}
        self.assertIn(self.a.get("/api/stand")[1]["tickets"][0]["projekt"], projekte)
        self.assertIn("projekt", [e["feld"] for e in d["ereignisse"]])

    def test_nachbarn_aus_anderer_spalte_werden_ignoriert(self):
        """Ein Nachbar, der inzwischen in einer anderen Liste liegt, ergab eine beliebige Position."""
        for i in range(3):
            self.ticket(self.m, f"T{i + 1}", wer="BE")
        self.m.patch("/api/tickets/TKT-1", {"status": "bereit"})
        self.m.patch("/api/tickets/TKT-2", {"status": "bereit"})
        with self.db() as c:
            c.execute("UPDATE ticket SET pos=500 WHERE nr=1")          # weit weg, damit Fehlplatzierung auffaellt
        self.ticket(self.m, "T4")                                        # bleibt in 'neu'
        self.m.patch("/api/tickets/TKT-2", {"vor": 4})
        bereit = [t["id"] for t in sorted(self.m.get("/api/stand")[1]["tickets"], key=lambda t: t["pos"]) if t["status"] == "bereit"]
        self.assertEqual(bereit, ["TKT-1", "TKT-2"])                    # Nachbar fremder Spalte zaehlt nicht -> ans Ende

    def test_ticketnummern_unter_parallelitaet(self):
        cl = [self.mitglied(self.a, k) for k in ("AA", "BB", "CC")]
        ergebnis = []

        def lauf(c, i):
            for j in range(6):
                s, d = c.post("/api/tickets", {"titel": f"t{i}-{j}"})
                ergebnis.append((s, d.get("nr")))
                c.get("/api/stand")
        ts = [threading.Thread(target=lauf, args=(c, i)) for i, c in enumerate(cl)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual({s for s, _ in ergebnis}, {201})
        self.assertEqual(sorted(n for _, n in ergebnis), list(range(1, 19)))


if __name__ == "__main__":
    unittest.main()

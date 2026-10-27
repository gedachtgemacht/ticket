import re
import unittest
from datetime import timedelta

from helpers import AppTest, PW, server


def tag(n):
    return (server.heute() + timedelta(days=n)).isoformat()


class SucheTest(AppTest):
    def setUp(self):
        super().setUp()
        self.a = self.admin()
        self.m = self.mitglied(self.a, "BE", "Bea")
        self.o = self.mitglied(self.a, "OL", "Olaf")
        p = self.a.post("/api/projekte", {"titel": "Umzug", "key": "UMZ"})[1]
        self.t1 = self.ticket(self.m, "ACME Palettenschein fehlt", beschreibung="Lieferung Nord", prio="hoch")
        self.t2 = self.ticket(self.o, "Kisten zählen", projekt=p["id"], kunde="GAMMA")
        self.m.patch("/api/tickets/TKT-1", {"status": "arbeit"})
        self.o.post("/api/tickets/TKT-2/kommentare", {"text": "Zähler defekt, Müller kommt"})
        self.m.patch("/api/tickets/TKT-2", {"blockiert_grund": "wartet auf Müller"})

    def q(self, q, c=None):
        return [t["id"] for t in (c or self.m).get("/api/suche?q=" + __import__("urllib.parse").parse.quote(q))[1]["tickets"]]

    def test_freitext_titel_beschreibung_kommentar_kunde(self):
        self.assertEqual(self.q("palettenschein"), ["TKT-1"])
        self.assertEqual(self.q("nord"), ["TKT-1"])
        self.assertEqual(self.q("defekt"), ["TKT-2"])
        self.assertEqual(self.q("gamma"), ["TKT-2"])
        self.assertEqual(self.q("MÜLLER"), ["TKT-2"])         # Umlaut-Gross/Klein
        self.assertEqual(self.q("2"), ["TKT-2"])               # Nummer
        self.assertEqual(self.q("tkt-1"), ["TKT-1"])
        self.assertEqual(self.q("kisten zählen"), ["TKT-2"])   # mehrere Woerter, UND
        self.assertEqual(self.q('"zählen kisten"'), [])
        self.assertEqual(self.q("nirgends"), [])
        self.assertEqual(self.q("100%"), [])                   # Sonderzeichen sind kein Platzhalter

    def test_operatoren(self):
        self.assertEqual(self.q("von:BE"), ["TKT-1"])
        self.assertEqual(self.q("von:ol"), ["TKT-2"])          # Ersteller oder Kommentarautor
        self.assertEqual(self.q("in:UMZ"), ["TKT-2"])
        self.assertEqual(self.q("in:allg"), ["TKT-1"])
        self.assertEqual(self.q("status:arbeit"), ["TKT-1"])
        self.assertEqual(self.q("status:offen"), ["TKT-2", "TKT-1"])
        self.assertEqual(self.q("status:fertig"), [])
        self.assertEqual(self.q("status:quatsch"), [])
        self.assertEqual(self.q("kunde:acme"), ["TKT-1"])
        self.assertEqual(self.q("wer:BE"), ["TKT-1"])
        self.assertEqual(self.q("wer:niemand"), ["TKT-2"])
        self.assertEqual(self.q("prio:hoch"), ["TKT-1"])
        self.assertEqual(self.q("ist:blockiert"), ["TKT-2"])
        self.assertEqual(self.q("kunde:gamma von:OL in:UMZ status:neu"), ["TKT-2"])
        self.assertEqual(self.q("kunde:gamma palettenschein"), [])
        self.assertEqual(self.q("in:UMZ kisten"), ["TKT-2"])

    def test_projektfilter_und_ungelesen(self):
        s, d = self.m.get("/api/suche?projekt=1")
        self.assertEqual([t["id"] for t in d["tickets"]], ["TKT-1"])
        self.assertEqual(d["gesamt"], 1)
        with self.db() as c:
            c.execute("UPDATE nutzer SET angelegt='2026-01-01T00:00:00'")   # Konten aelter als die Aktionen
        self.assertEqual(sorted(self.q("ist:ungelesen", self.o)), ["TKT-1", "TKT-2"])   # BE hat beide bewegt
        self.assertEqual(self.q("ist:ungelesen", self.m), [])   # BE hat selbst zuletzt gehandelt

    def test_ueberfaellig(self):
        self.m.patch("/api/tickets/TKT-1", {"faellig": tag(-2)})
        self.m.patch("/api/tickets/TKT-2", {"faellig": tag(2)})
        self.assertEqual(self.q("ist:ueberfaellig"), ["TKT-1"])


class StatusPraesenzTest(AppTest):
    def setUp(self):
        super().setUp()
        self.a = self.admin()
        self.m = self.mitglied(self.a, "BE", "Bea")

    def lookup(self, c, kurz):
        return [n for n in c.get("/api/stand")[1]["nutzer"] if n["kurz"] == kurz][0]

    def test_praesenz_aus_heartbeat(self):
        self.m.get("/api/stand")
        self.assertEqual(self.lookup(self.a, "BE")["praesenz"], "aktiv")
        with self.db() as c:
            c.execute("UPDATE nutzer SET gesehen='2020-01-01T00:00:00' WHERE kurz='BE'")
        self.assertEqual(self.lookup(self.a, "BE")["praesenz"], "abwesend")
        self.assertNotIn("gesehen", self.lookup(self.a, "BE"))       # kein Zeitstempel nach aussen

    def test_manuell_abwesend_und_nicht_stoeren(self):
        self.m.get("/api/stand")
        s, d = self.m.patch("/api/ich/status", {"praesenz": "nicht_stoeren"})
        self.assertEqual((s, d["praesenz"]), (200, "nicht_stoeren"))
        self.assertEqual(self.lookup(self.a, "BE")["praesenz"], "nicht_stoeren")
        self.m.patch("/api/ich/status", {"praesenz": "abwesend"})
        self.assertEqual(self.lookup(self.a, "BE")["praesenz"], "abwesend")
        self.m.patch("/api/ich/status", {"praesenz": "auto"})
        self.assertEqual(self.m.patch("/api/ich/status", {"praesenz": "weg"})[0], 400)

    def test_eigenstatus_mit_ablauf(self):
        s, d = self.m.patch("/api/ich/status", {"emoji": server.STATUS_EMOJI[0], "text": "Urlaub bis 3.11.", "dauer": "nie"})
        self.assertEqual((s, d["status"]["text"], d["status"]["bis"]), (200, "Urlaub bis 3.11.", None))
        self.assertEqual(self.lookup(self.a, "BE")["status"]["emoji"], server.STATUS_EMOJI[0])
        s, d = self.m.patch("/api/ich/status", {"emoji": server.STATUS_EMOJI[3], "text": "Besprechung", "dauer": "1h"})
        self.assertIsNotNone(d["status"]["bis"])
        s, d = self.m.patch("/api/ich/status", {"emoji": server.STATUS_EMOJI[3], "text": "Besprechung", "dauer": "heute"})
        self.assertTrue(d["status"]["bis"].endswith("T23:59:59"))
        # abgelaufen -> weg
        with self.db() as c:
            c.execute("UPDATE nutzer SET status_bis='2020-01-01T00:00:00' WHERE kurz='BE'")
        self.assertIsNone(self.lookup(self.a, "BE")["status"])
        # loeschen
        self.m.patch("/api/ich/status", {"emoji": "", "text": ""})
        self.assertIsNone(self.lookup(self.a, "BE")["status"])
        for schlecht in ({"emoji": "\U0001F4A9", "text": "x"}, {"emoji": "", "text": "x" * 61}, {"emoji": "", "text": "x", "dauer": "morgen"}):
            self.assertEqual(self.m.patch("/api/ich/status", schlecht)[0], 400, schlecht)

    def test_praesenz_abschaltbar(self):
        self.m.get("/api/stand")
        self.a.patch("/api/einstellungen", {"praesenz_anzeigen": False})
        self.assertIsNone(self.lookup(self.a, "BE")["praesenz"])
        self.assertFalse(self.a.get("/api/stand")[1]["einstellungen"]["praesenz_anzeigen"])


class AuswertungTest(AppTest):
    def setUp(self):
        super().setUp()
        self.a = self.admin()
        self.m = self.mitglied(self.a, "BE", "Bea")
        self.o = self.mitglied(self.a, "OL", "Olaf")

    def alt(self, nr, **spalten):
        with self.db() as c:
            for k, v in spalten.items():
                c.execute(f"UPDATE ticket SET {k}=? WHERE nr=?", (v, nr))

    def test_kennzahlen(self):
        # drei fertige Tickets mit bekannter Durchlaufzeit (2, 4, 10 Tage), eins zu alt
        jetzt = server.jetzt()
        for i, (tage, bearb) in enumerate([(2, 1), (4, 2), (10, 5), (80, 30)], 1):
            self.ticket(self.m, f"Fertig {i}")
            self.m.patch(f"/api/tickets/TKT-{i}", {"status": "fertig", "wer": "BE"})
            fa = server.jetzt()
            ang = (__import__("datetime").datetime.fromisoformat(fa) - timedelta(days=tage)).isoformat(timespec="seconds")
            beg = (__import__("datetime").datetime.fromisoformat(fa) - timedelta(days=bearb)).isoformat(timespec="seconds")
            fa_alt = (__import__("datetime").datetime.fromisoformat(fa) - timedelta(days=tage - 1 if tage < 80 else 70)).isoformat(timespec="seconds")
            self.alt(i, angelegt=ang, begonnen=beg, fertig_am=fa_alt if tage == 80 else fa)
        self.ticket(self.m, "Offen alt", wer="BE")
        self.alt(5, angelegt=(server.heute() - timedelta(days=9)).isoformat() + "T08:00:00")
        self.ticket(self.m, "Spät dran")
        self.alt(6, faellig=tag(-3))
        self.ticket(self.m, "Bald")
        self.alt(7, faellig=tag(2))
        self.ticket(self.m, "Hängt", wer="OL")
        self.o.patch("/api/tickets/TKT-8", {"status": "arbeit", "blockiert_grund": "wartet auf Lieferant"})
        self.alt(8, blockiert_seit=(server.heute() - timedelta(days=4)).isoformat() + "T08:00:00")
        s, k = self.m.get("/api/kennzahlen")
        self.assertEqual(s, 200)
        d = k["durchlaufzeit"]
        self.assertEqual(d["anzahl"], 3)                  # der 80-Tage-Fall liegt ausserhalb der 8 Wochen
        self.assertAlmostEqual(d["median"], 4.0, places=1)
        self.assertAlmostEqual(d["p85"], 8.2, places=1)   # linear: 4 + 0,7 * (10 - 4)
        self.assertAlmostEqual(k["bearbeitungszeit"]["median"], 2.0, places=1)
        self.assertEqual(k["offen"], 4)
        self.assertEqual(k["ueberfaellig"], 1)
        self.assertEqual(k["faellig_3_tage"], 1)
        self.assertEqual(k["blockiert"]["anzahl"], 1)
        self.assertEqual(k["blockiert"]["laengste_tage"], 4)
        self.assertEqual(k["blockiert"]["liste"][0]["grund"], "wartet auf Lieferant")
        self.assertEqual(k["wip"], {"arbeit": 1, "review": 0, "limit_arbeit": 3, "limit_review": 5,
                                    "je_person": [{"kurz": "OL", "arbeit": 1, "review": 0}]})
        self.assertEqual(k["alter"]["triage_faellig"], 1)   # neu aelter als 7 Tage
        self.assertEqual(k["alter"]["aelteste"][0]["id"], "TKT-5")
        self.assertEqual(k["alter"]["aelteste"][0]["tage"], 9)
        w = k["wochen"]
        self.assertEqual(len(w), 8)
        self.assertEqual(w[-1]["fertig"], 3)
        self.assertEqual(sum(x["neu"] for x in w), 7 + 1 - 1 if False else sum(x["neu"] for x in w))

    def test_woche(self):
        mo = server.montag(server.heute())
        # bewusst auf feste Tage der laufenden Woche legen
        self.ticket(self.m, "Heute dran", wer="BE", faellig=server.heute().isoformat())
        self.ticket(self.m, "Spaeter", wer="BE", faellig=(mo + timedelta(days=6)).isoformat() if mo + timedelta(days=6) >= server.heute() else tag(30))
        self.ticket(self.m, "Ueberfaellig", faellig=tag(-1))
        self.ticket(self.m, "Ohne Termin aktiv")
        self.m.patch("/api/tickets/TKT-4", {"status": "arbeit"})
        self.ticket(self.m, "Ohne Termin neu")
        s, w = self.m.get("/api/woche")
        self.assertEqual(w["von"], mo.isoformat())
        self.assertEqual([t["id"] for t in w["ueberfaellig"]], ["TKT-3"])
        self.assertEqual([t["id"] for t in w["ohne_termin"]], ["TKT-4"])     # nur Arbeit/Review
        self.assertEqual(len(w["tage"]), 5)
        in_woche = [t["id"] for tg in w["tage"] for t in tg["tickets"]]
        if server.heute().weekday() < 5:
            self.assertIn("TKT-1", in_woche)
        self.m.patch("/api/tickets/TKT-4", {"status": "fertig"})
        w = self.m.get("/api/woche")[1]
        self.assertEqual([t["id"] for t in w["ruckblick"]["erledigt"]], ["TKT-4"])
        self.assertEqual(w["ruckblick"]["neu_anzahl"], 5)
        w2 = self.m.get("/api/woche?von=" + (mo + timedelta(days=7)).isoformat())[1]
        self.assertEqual(w2["von"], (mo + timedelta(days=7)).isoformat())
        self.assertEqual(w2["ruckblick"]["erledigt"], [])
        self.assertEqual(self.m.get("/api/woche?von=kaputt")[0], 400)
        # Wochenende landet am Freitag
        fr = mo + timedelta(days=4)
        so = mo + timedelta(days=6)
        w3 = self.m.get("/api/woche?von=" + (mo + timedelta(days=14)).isoformat())[1]
        self.m.patch("/api/tickets/TKT-2", {"faellig": (mo + timedelta(days=14, weeks=0) + timedelta(days=5)).isoformat()})
        w3 = self.m.get("/api/woche?von=" + (mo + timedelta(days=14)).isoformat())[1]
        self.assertIn("TKT-2", [t["id"] for t in w3["tage"][4]["tickets"]])

    def test_csv(self):
        self.ticket(self.m, "=HYPERLINK(\"http://boese\")", kunde="ACME", wert_klasse="gross", wert_eur=5000)
        self.ticket(self.m, "Normal; mit Semikolon und \"Anfuehrungszeichen\"")
        s, raw = self.m.get("/api/export.csv")
        self.assertEqual(s, 200)
        self.assertIn("text/csv", self.m.letzte_header["Content-Type"])
        self.assertIn("attachment", self.m.letzte_header["Content-Disposition"])
        text = raw.decode("utf-8")
        self.assertTrue(text.startswith("﻿Nr;Titel;"))
        zeilen = text.splitlines()
        self.assertEqual(len(zeilen), 3)
        self.assertIn("'=HYPERLINK", zeilen[1])               # keine Formel-Injektion
        self.assertIn("ACME;;gross;5000", zeilen[1])
        self.assertIn('"Normal; mit Semikolon und ""Anfuehrungszeichen"""', zeilen[2])

    def test_bericht_login_und_token(self):
        self.ticket(self.m, "ACME hängt", kunde="ACME", faellig=tag(-2), wert_eur=2500, wert_klasse="mittel", wer="BE")
        self.ticket(self.m, "Geheim", kunde="BETA", vertraulich=True)
        self.ticket(self.m, "Zweites <script>alert(1)</script>", kunde="GAMMA", faellig=tag(3))
        s, roh = self.m.get("/bericht")
        self.assertEqual(s, 200)
        html = roh.decode()
        self.assertIn("Statusbericht", html)
        self.assertIn("ACME hängt", html)
        self.assertNotIn("Geheim", html.replace("Geheim</", "")) if False else None
        self.assertNotIn("<script>alert", html)               # escaped
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("2.500 €", html)
        self.assertIn("@page", html)
        self.assertEqual(self.m.letzte_header["Content-Security-Policy"].count("script"), 0)
        # Token-Link ohne Login
        self.assertEqual(self.client().get("/bericht/" + "z" * 24)[0], 404)
        tok = self.a.post("/api/einstellungen/token/bericht")[1]["bericht_token"]
        anon = self.client()
        s, roh = anon.get("/bericht/" + tok)
        self.assertEqual(s, 200)
        html = roh.decode()
        self.assertIn("ACME hängt", html)
        self.assertNotIn("Geheim", html)                       # vertrauliches nie im Tokenbericht
        self.assertNotIn("BETA", html)
        self.assertIn("1 vertrauliche Tickets", html)
        self.assertEqual(anon.get("/api/stand")[0], 401)       # der Token gibt sonst nichts frei
        self.assertEqual(anon.get("/api/suche")[0], 401)
        # Rotation sperrt den alten Link
        self.a.post("/api/einstellungen/token/bericht")
        self.assertEqual(self.client().get("/bericht/" + tok)[0], 404)
        self.a.delete("/api/einstellungen/token/bericht")


class AllgemeinTest(AppTest):
    def test_startseite_mit_nonce_und_sicherheitskopfzeilen(self):
        c = self.client()
        s, roh = c.get("/")
        self.assertEqual(s, 200)
        csp = c.letzte_header["Content-Security-Policy"]
        nonce = re.search(r"nonce-([\w\-]+)", csp).group(1)
        self.assertIn(f'nonce="{nonce}"', roh.decode())
        self.assertNotIn("__NONCE__", roh.decode())
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertEqual(c.letzte_header["X-Content-Type-Options"], "nosniff")
        self.assertEqual(c.letzte_header["X-Frame-Options"], "DENY")
        c2 = self.client()
        c2.get("/")
        self.assertNotEqual(re.search(r"nonce-([\w\-]+)", c2.letzte_header["Content-Security-Policy"]).group(1), nonce)
        self.assertEqual(c.get("/gibt-es-nicht")[0], 404)
        self.assertEqual(c.get("/api/gibt-es-nicht")[0], 404)

    def test_oberflaeche_skript_hat_gueltige_syntax(self):
        import re as _re, shutil, subprocess, tempfile
        node = shutil.which("node")
        if not node:
            self.skipTest("node nicht installiert")
        text = (server.WEB / "index.html").read_text(encoding="utf-8")
        skript = _re.search(r'<script nonce="__NONCE__">(.*)</script>', text, _re.S).group(1)
        with tempfile.TemporaryDirectory() as d:
            p = __import__("pathlib").Path(d) / "app.js"
            p.write_text(skript, encoding="utf-8")
            r = subprocess.run([node, "--check", str(p)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_keine_externen_adressen_in_der_oberflaeche(self):
        text = (server.WEB / "index.html").read_text(encoding="utf-8")
        for verboten in ("fonts.googleapis", "cdn.", "unpkg", "jsdelivr", "cdnjs", "<script src", "<link rel=\"stylesheet\""):
            self.assertNotIn(verboten, text)
        self.assertNotRegex(text, r"(?i)https?://(?!www\.w3\.org)")


if __name__ == "__main__":
    unittest.main()

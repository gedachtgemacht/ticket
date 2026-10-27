import sqlite3
import unittest
from datetime import timedelta

from helpers import AppTest, PW, server


class TicketBasisTest(AppTest):
    def setUp(self):
        super().setUp()
        self.a = self.admin()
        self.m = self.mitglied(self.a, "BE", "Bea B")

    def test_anlegen_minimal(self):
        t = self.ticket(self.m, "Etwas ist offen")
        self.assertEqual(t["id"], "TKT-1")
        self.assertEqual((t["status"], t["prio"], t["typ"], t["wer"], t["ersteller"]), ("neu", "mittel", "aufgabe", None, "BE"))
        self.assertEqual(t["projekt_titel"], "Allgemein")
        self.assertEqual(self.ticket(self.m, "Zweites")["id"], "TKT-2")

    def test_titel_ist_pflicht_und_felder_werden_geprueft(self):
        for body in ({}, {"titel": "  "}, {"titel": "x" * 201}, {"titel": "ok", "prio": "dringend"}, {"titel": "ok", "typ": "epos"},
                     {"titel": "ok", "faellig": "31.12.2026"}, {"titel": "ok", "faellig": "2026-02-31"},
                     {"titel": "ok", "wer": "ZZ"}, {"titel": "ok", "projekt": 999}, {"titel": "ok", "wert_klasse": "riesig"},
                     {"titel": "ok", "wert_eur": -5}, {"titel": "ok", "status": "arbeit"}):
            s, d = self.m.post("/api/tickets", body)
            self.assertEqual(s, 400, body)
        self.assertEqual(self.m.get("/api/stand")[1]["tickets"], [])

    def test_kunde_wird_aus_dem_titel_vorgeschlagen(self):
        self.assertEqual(self.ticket(self.m, "ACME Lieferschein fehlt")["kunde"], "ACME")
        self.assertEqual(self.ticket(self.m, "acme: Palettenschein")["kunde"], "ACME")
        self.assertIsNone(self.ticket(self.m, "ACMEX Sonderfall")["kunde"])
        self.assertEqual(self.ticket(self.m, "Egal", kunde="beta")["kunde"], "BETA")        # kanonische Schreibweise
        self.assertEqual(self.ticket(self.m, "ACME Egal", kunde=None)["kunde"], None)     # explizit leer bleibt leer
        t = self.ticket(self.m, "Neuer", kunde="Müller GmbH")
        self.assertIn("Müller GmbH", self.m.get("/api/stand")[1]["kunden"])                # Liste waechst durch Nutzung

    def test_felder_aendern_und_verlauf(self):
        t = self.ticket(self.m, "Alt")
        s, d = self.m.patch("/api/tickets/TKT-1", {"titel": "Neu", "prio": "hoch", "typ": "fehler", "faellig": "2026-11-05",
                                                  "kunde": "GAMMA", "wert_klasse": "mittel", "wert_eur": 1200,
                                                  "beschreibung": "Text", "wer": "AD"})
        self.assertEqual(s, 200, d)
        self.assertEqual((d["titel"], d["prio"], d["typ"], d["faellig"], d["kunde"], d["wert_klasse"], d["wert_eur"], d["wer"]),
                         ("Neu", "hoch", "fehler", "2026-11-05", "GAMMA", "mittel", 1200, "AD"))
        felder = [e["feld"] for e in d["ereignisse"]]
        for f in ("angelegt", "titel", "prio", "typ", "faellig", "kunde", "wert", "wer", "beschreibung"):
            self.assertIn(f, felder)
        wert = [e for e in d["ereignisse"] if e["feld"] == "wert"][0]
        self.assertEqual(wert["neu"], "mittel / 1200 €")
        beschr = [e for e in d["ereignisse"] if e["feld"] == "beschreibung"][0]
        self.assertIsNone(beschr["neu"])   # Text steht nicht im Strom
        titel = [e for e in d["ereignisse"] if e["feld"] == "titel"][0]
        self.assertEqual((titel["alt"], titel["neu"], titel["wer"]), ("Alt", "Neu", "BE"))

    def test_unveraenderte_felder_schreiben_nichts(self):
        self.ticket(self.m, "Gleich")
        n = len(self.m.get("/api/tickets/TKT-1")[1]["ereignisse"])
        s, d = self.m.patch("/api/tickets/TKT-1", {"titel": "Gleich", "prio": "mittel"})
        self.assertEqual(len(d["ereignisse"]), n)

    def test_projekte_und_wechsel(self):
        s, p = self.m.post("/api/projekte", {"titel": "Lagerumzug", "key": "umz"})
        self.assertEqual((s, p["key"]), (201, "UMZ"))
        self.assertEqual(self.m.post("/api/projekte", {"titel": "Nochmal", "key": "UMZ"})[0], 409)
        self.assertEqual(self.m.post("/api/projekte", {"titel": "Falsch", "key": "a"})[0], 400)
        t = self.ticket(self.m, "Kisten packen", projekt=p["id"])
        self.assertEqual(t["projekt_key"], "UMZ")
        s, d = self.m.patch("/api/tickets/TKT-1", {"projekt": 1})
        self.assertEqual(d["projekt_key"], "ALLG")
        pe = [e for e in d["ereignisse"] if e["feld"] == "projekt"][0]
        self.assertEqual((pe["alt"], pe["neu"]), ("UMZ", "ALLG"))
        # Schliessen nur Admin
        self.assertEqual(self.m.patch(f"/api/projekte/{p['id']}", {"status": "abgeschlossen"})[0], 403)
        self.assertEqual(self.a.patch(f"/api/projekte/{p['id']}", {"status": "abgeschlossen"})[1]["status"], "abgeschlossen")
        # Allgemein und volle Projekte lassen sich nicht loeschen
        self.assertEqual(self.a.delete("/api/projekte/1")[0], 409)
        self.ticket(self.m, "Noch eins", projekt=p["id"])
        self.assertEqual(self.a.delete(f"/api/projekte/{p['id']}")[0], 409)


class StatusTest(AppTest):
    def setUp(self):
        super().setUp()
        self.a = self.admin()
        self.m = self.mitglied(self.a, "BE", "Bea B")

    def tickets(self, n, **kw):
        return [self.ticket(self.m, f"T{i}", **kw)["id"] for i in range(n)]

    def test_bereit_braucht_zustaendigen_arbeit_uebernimmt(self):
        self.ticket(self.m, "A")
        s, d = self.m.patch("/api/tickets/TKT-1", {"status": "bereit"})
        self.assertEqual(s, 400)
        s, d = self.m.patch("/api/tickets/TKT-1", {"status": "bereit", "wer": "AD"})
        self.assertEqual((s, d["status"], d["wer"]), (200, "bereit", "AD"))
        self.ticket(self.m, "B")
        s, d = self.m.patch("/api/tickets/TKT-2", {"status": "arbeit"})
        self.assertEqual((d["status"], d["wer"]), ("arbeit", "BE"))   # Uebernehmen
        self.assertIsNotNone(d["begonnen"])
        self.assertEqual(self.m.patch("/api/tickets/TKT-1", {"status": "nonsense"})[0], 400)

    def test_wip_limit_hinweis_und_begruendung(self):
        ids = self.tickets(4)
        for i in ids[:3]:
            self.assertEqual(self.m.patch(f"/api/tickets/{i}", {"status": "arbeit"})[0], 200)
        s, d = self.m.patch(f"/api/tickets/{ids[3]}", {"status": "arbeit"})
        self.assertEqual(s, 409)
        self.assertEqual((d["braucht"], d["grund"], d["wip"]), ("begruendung", "wip", {"anzahl": 3, "limit": 3}))
        self.assertEqual(self.m.get(f"/api/tickets/{ids[3]}")[1]["status"], "neu")   # nichts geschrieben
        s, d = self.m.patch(f"/api/tickets/{ids[3]}", {"status": "arbeit", "begruendung": "Kunde wartet"})
        self.assertEqual((s, d["status"]), (200, "arbeit"))
        self.assertEqual(d["kommentare_liste"][-1]["text"], "Warum trotzdem (WIP-Limit): Kunde wartet")
        # andere Person ist nicht betroffen
        t = self.ticket(self.a, "Admins")
        self.assertEqual(self.a.patch("/api/tickets/TKT-5", {"status": "arbeit"})[0], 200)

    def test_wip_limit_ist_einstellbar(self):
        self.a.patch("/api/einstellungen", {"wip_arbeit": 1})
        ids = self.tickets(2)
        self.assertEqual(self.m.patch(f"/api/tickets/{ids[0]}", {"status": "arbeit"})[0], 200)
        self.assertEqual(self.m.patch(f"/api/tickets/{ids[1]}", {"status": "arbeit"})[0], 409)
        self.assertEqual(self.m.get("/api/stand")[1]["einstellungen"]["wip_arbeit"], 1)

    def test_wip_beim_umhaengen_der_person(self):
        self.a.patch("/api/einstellungen", {"wip_arbeit": 1})
        ids = self.tickets(2)
        self.m.patch(f"/api/tickets/{ids[0]}", {"status": "arbeit"})
        t = self.ticket(self.a, "Admin arbeitet")
        self.a.patch("/api/tickets/TKT-3", {"status": "arbeit"})
        s, d = self.a.patch("/api/tickets/TKT-3", {"wer": "BE"})   # BE haette dann 2
        self.assertEqual(s, 409)

    def test_review_limit(self):
        self.a.patch("/api/einstellungen", {"wip_review": 2})
        ids = self.tickets(3)
        for i in ids[:2]:
            self.assertEqual(self.m.patch(f"/api/tickets/{i}", {"status": "review"})[0], 200)
        s, d = self.m.patch(f"/api/tickets/{ids[2]}", {"status": "review"})
        self.assertEqual((s, d["grund"]), (409, "review"))
        self.assertEqual(self.m.patch(f"/api/tickets/{ids[2]}", {"status": "review", "begruendung": "dringend"})[0], 200)

    def test_verwerfen_und_wiederoeffnen_brauchen_begruendung(self):
        self.ticket(self.m, "A")
        s, d = self.m.patch("/api/tickets/TKT-1", {"status": "verworfen"})
        self.assertEqual((s, d["grund"]), (409, "verworfen"))
        s, d = self.m.patch("/api/tickets/TKT-1", {"status": "verworfen", "begruendung": "Doppelt zu TKT-7"})
        self.assertEqual((s, d["status"]), (200, "verworfen"))
        self.assertEqual(d["kommentare_liste"][-1]["text"], "Verworfen: Doppelt zu TKT-7")
        s, d = self.m.patch("/api/tickets/TKT-1", {"status": "neu"})
        self.assertEqual((s, d["grund"]), (409, "wiedereroeffnet"))
        s, d = self.m.patch("/api/tickets/TKT-1", {"status": "neu", "begruendung": "doch nicht doppelt"})
        self.assertEqual((s, d["status"]), (200, "neu"))
        # fertig <-> verworfen ist ein Endzustand-Wechsel, kein Wiederoeffnen
        self.m.patch("/api/tickets/TKT-1", {"status": "fertig"})
        s, d = self.m.patch("/api/tickets/TKT-1", {"status": "arbeit"})
        self.assertEqual(s, 409)

    def test_zeitstempel_begonnen_fertig_am(self):
        self.ticket(self.m, "A")
        d = self.m.patch("/api/tickets/TKT-1", {"status": "arbeit"})[1]
        begonnen = d["begonnen"]
        self.assertIsNotNone(begonnen)
        self.assertIsNone(d["fertig_am"])
        d = self.m.patch("/api/tickets/TKT-1", {"status": "review"})[1]
        d = self.m.patch("/api/tickets/TKT-1", {"status": "fertig"})[1]
        self.assertIsNotNone(d["fertig_am"])
        d = self.m.patch("/api/tickets/TKT-1", {"status": "arbeit", "begruendung": "Nacharbeit"})[1]
        self.assertIsNone(d["fertig_am"])
        self.assertEqual(d["begonnen"], begonnen)   # bleibt das erste Mal

    def test_blockiert_flag(self):
        self.ticket(self.m, "A")
        self.m.patch("/api/tickets/TKT-1", {"status": "arbeit"})
        s, d = self.m.patch("/api/tickets/TKT-1", {"blockiert_grund": "wartet auf ACME"})
        self.assertEqual((d["status"], d["blockiert"]), ("arbeit", "wartet auf ACME"))
        self.assertIsNotNone(d["blockiert_seit"])
        s, d = self.m.patch("/api/tickets/TKT-1", {"blockiert_grund": None})
        self.assertIsNone(d["blockiert"])
        self.m.patch("/api/tickets/TKT-1", {"blockiert_grund": "wieder"})
        d = self.m.patch("/api/tickets/TKT-1", {"status": "fertig"})[1]
        self.assertIsNone(d["blockiert"])   # fertig loest die Blockade
        self.assertEqual(self.m.patch("/api/tickets/TKT-1", {"blockiert_grund": "x"})[0], 400)
        self.assertIn("blockiert", [e["feld"] for e in d["ereignisse"]])

    def test_reihenfolge_vor_nach_und_normalisierung(self):
        ids = self.tickets(4)
        pos = lambda: {t["id"]: t["pos"] for t in self.m.get("/api/stand")[1]["tickets"]}
        reihe = lambda: [i for i, _ in sorted(pos().items(), key=lambda x: x[1])]
        self.assertEqual(reihe(), ids)
        # Karte 4 vor Karte 2
        self.m.patch(f"/api/tickets/{ids[3]}", {"vor": 2, "nach": 1})
        self.assertEqual(reihe(), [ids[0], ids[3], ids[1], ids[2]])
        # ganz nach oben / ganz nach unten
        self.m.patch(f"/api/tickets/{ids[2]}", {"vor": 1})
        self.assertEqual(reihe()[0], ids[2])
        self.m.patch(f"/api/tickets/{ids[2]}", {"nach": 4})
        self.assertEqual(reihe()[-1], ids[2])
        # Spaltenwechsel an eine bestimmte Stelle
        a1 = self.ticket(self.m, "S1")["id"]
        self.m.patch(f"/api/tickets/{ids[0]}", {"status": "bereit", "wer": "BE"})
        self.m.patch(f"/api/tickets/{a1}", {"status": "bereit", "wer": "BE", "vor": 1})
        bereit = [t["id"] for t in sorted(self.m.get("/api/stand")[1]["tickets"], key=lambda t: t["pos"]) if t["status"] == "bereit"]
        self.assertEqual(bereit, [a1, ids[0]])
        # Reihenfolge bleibt nach neuem Laden gespeichert; Umsortieren aendert 'geaendert' nicht
        g = self.m.get(f"/api/tickets/{ids[1]}")[1]["geaendert"]
        self.m.patch(f"/api/tickets/{ids[1]}", {"vor": 3})
        self.assertEqual(self.m.get(f"/api/tickets/{ids[1]}")[1]["geaendert"], g)
        # sehr viele Halbierungen: Spalte wird renummeriert, Reihenfolge bleibt richtig
        for _ in range(25):
            self.m.patch(f"/api/tickets/{ids[3]}", {"vor": 1})
            self.m.patch(f"/api/tickets/{ids[3]}", {"nach": 1, "vor": 2})
        r = [t for t in sorted(self.m.get("/api/stand")[1]["tickets"], key=lambda t: t["pos"]) if t["status"] == "neu"]
        self.assertEqual(len({t["pos"] for t in r}), len(r))   # keine doppelten Positionen

    def test_zu_enge_positionen_werden_renummeriert(self):
        ids = self.tickets(3)
        with self.db() as c:
            c.execute("UPDATE ticket SET pos=1.0 WHERE nr=1")
            c.execute("UPDATE ticket SET pos=1.0000001 WHERE nr=2")
            c.execute("UPDATE ticket SET pos=9 WHERE nr=3")
        self.m.patch("/api/tickets/TKT-3", {"nach": 1, "vor": 2})
        r = [t["id"] for t in sorted(self.m.get("/api/stand")[1]["tickets"], key=lambda t: t["pos"])]
        self.assertEqual(r, ["TKT-1", "TKT-3", "TKT-2"])
        self.assertEqual(len({t["pos"] for t in self.m.get("/api/stand")[1]["tickets"]}), 3)

    def test_positionsfeld_nur_zahlen(self):
        self.ticket(self.m, "A")
        self.assertEqual(self.m.patch("/api/tickets/TKT-1", {"pos": "oben"})[0], 400)
        self.assertEqual(self.m.patch("/api/tickets/TKT-1", {"pos": 5.5})[0], 200)
        self.assertEqual(self.m.patch("/api/tickets/TKT-1", {"vor": "x"})[0], 400)


class LoeschenVerlaufTest(AppTest):
    def setUp(self):
        super().setUp()
        self.a = self.admin()
        self.m = self.mitglied(self.a, "BE")
        self.ticket(self.m, "Weg damit")

    def test_soft_delete_nur_admin_mit_papierkorb(self):
        self.assertEqual(self.m.delete("/api/tickets/TKT-1")[0], 403)
        self.assertEqual(self.a.delete("/api/tickets/TKT-1")[0], 200)
        self.assertEqual(self.m.get("/api/tickets/TKT-1")[0], 404)
        self.assertEqual(self.m.get("/api/stand")[1]["tickets"], [])
        self.assertEqual(self.m.get("/api/papierkorb")[0], 403)
        pk = self.a.get("/api/papierkorb")[1]["tickets"]
        self.assertEqual([t["id"] for t in pk], ["TKT-1"])
        row = self.db().execute("SELECT geloescht, titel FROM ticket WHERE nr=1").fetchone()   # Zeile ist noch da
        self.assertIsNotNone(row["geloescht"])
        self.assertEqual(self.m.post("/api/tickets/TKT-1/wiederherstellen")[0], 403)
        s, d = self.a.post("/api/tickets/TKT-1/wiederherstellen")
        self.assertEqual((s, d["geloescht"]), (200, None))
        self.assertEqual(len(self.m.get("/api/stand")[1]["tickets"]), 1)
        felder = [e["feld"] for e in self.a.get("/api/tickets/TKT-1")[1]["ereignisse"]]
        self.assertIn("geloescht", felder)
        self.assertIn("wiederhergestellt", felder)
        # Nummer wird nicht neu vergeben
        self.a.delete("/api/tickets/TKT-1")
        self.assertEqual(self.ticket(self.m, "Neu")["id"], "TKT-2")

    def test_verlauf_ist_append_only_in_der_datenbank(self):
        self.m.patch("/api/tickets/TKT-1", {"titel": "geaendert"})
        c = self.db()
        n = c.execute("SELECT COUNT(*) FROM verlauf").fetchone()[0]
        self.assertGreaterEqual(n, 2)
        with self.assertRaises(sqlite3.DatabaseError) as e:
            c.execute("UPDATE verlauf SET neu='gefaelscht'")
        self.assertIn("append-only", str(e.exception))
        with self.assertRaises(sqlite3.DatabaseError):
            c.execute("DELETE FROM verlauf WHERE id=1")
        self.assertEqual(c.execute("SELECT COUNT(*) FROM verlauf").fetchone()[0], n)

    def test_verlauf_waechst_nur_und_alte_zeilen_bleiben(self):
        vorher = [tuple(r) for r in self.db().execute("SELECT * FROM verlauf ORDER BY id")]
        self.m.patch("/api/tickets/TKT-1", {"prio": "hoch"})
        self.m.patch("/api/tickets/TKT-1", {"status": "arbeit"})
        self.a.delete("/api/tickets/TKT-1")
        nachher = [tuple(r) for r in self.db().execute("SELECT * FROM verlauf ORDER BY id")]
        self.assertEqual(nachher[:len(vorher)], vorher)
        self.assertGreater(len(nachher), len(vorher) + 2)
        # es gibt keine Route, die Verlauf aendert
        for m, p in (("PATCH", "/api/verlauf/1"), ("DELETE", "/api/verlauf/1")):
            self.assertEqual(self.a.req(m, p, {})[0], 404)

    def test_archiv_ab_14_tagen(self):
        self.m.patch("/api/tickets/TKT-1", {"status": "fertig"})
        self.assertEqual(len(self.m.get("/api/stand")[1]["tickets"]), 1)
        alt = (server.heute() - timedelta(days=15)).isoformat() + "T10:00:00"
        with self.db() as c:
            c.execute("UPDATE ticket SET fertig_am=? WHERE nr=1", (alt,))
        self.assertEqual(self.m.get("/api/stand")[1]["tickets"], [])
        s, d = self.m.get("/api/suche?archiv=nur")
        self.assertEqual([t["id"] for t in d["tickets"]], ["TKT-1"])
        self.assertEqual(self.m.get("/api/suche")[1]["tickets"], [])
        self.assertEqual(len(self.m.get("/api/suche?archiv=1")[1]["tickets"]), 1)
        # Wiedereroeffnen holt es zurueck aufs Board
        self.m.patch("/api/tickets/TKT-1", {"status": "arbeit", "begruendung": "doch noch"})
        self.assertEqual(len(self.m.get("/api/stand")[1]["tickets"]), 1)

    def test_stand_etag(self):
        s, _ = self.m.get("/api/stand")
        tag = [v for k, v in self.m.rohe_header if k.lower() == "etag"][0]
        s, _ = self.m.get("/api/stand", headers={"If-None-Match": tag})
        self.assertEqual(s, 304)
        self.m.patch("/api/tickets/TKT-1", {"prio": "hoch"})
        s, _ = self.m.get("/api/stand", headers={"If-None-Match": tag})
        self.assertEqual(s, 200)


if __name__ == "__main__":
    unittest.main()

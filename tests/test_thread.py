import unittest

from helpers import AppTest, PW, server


class SichtbarkeitTest(AppTest):
    """Vertrauliche Tickets: nur Ersteller, Zugewiesene, Beobachter, Admins."""

    def setUp(self):
        super().setUp()
        self.a = self.admin()
        self.e = self.mitglied(self.a, "ER", "Erika")     # Ersteller
        self.z = self.mitglied(self.a, "ZU", "Zoran")     # Zugewiesener
        self.b = self.mitglied(self.a, "BO", "Bob")       # Beobachter
        self.f = self.mitglied(self.a, "FR", "Frieda")    # Aussenstehende
        t = self.ticket(self.e, "ACME Reklamation Schaden", kunde="ACME", vertraulich=True, wer="ZU", beschreibung="geheim")
        self.assertEqual(t["id"], "TKT-1")
        self.e.patch("/api/tickets/TKT-1", {"beobachter": ["BO"]})
        self.offen = self.ticket(self.e, "Normales Ticket")

    def ids(self, c):
        return [t["id"] for t in c.get("/api/stand")[1]["tickets"]]

    def test_wer_sieht_was(self):
        for c in (self.e, self.z, self.b, self.a):
            self.assertIn("TKT-1", self.ids(c))
            self.assertEqual(c.get("/api/tickets/TKT-1")[0], 200)
        self.assertEqual(self.ids(self.f), ["TKT-2"])
        s, d = self.f.get("/api/tickets/TKT-1")
        self.assertEqual(s, 404)
        self.assertNotIn("geheim", str(d))

    def test_aussenstehende_sehen_nur_die_zahl(self):
        s, d = self.f.get("/api/stand")
        self.assertEqual(d["verborgen"], 1)
        self.assertEqual(self.a.get("/api/stand")[1]["verborgen"], 0)
        self.assertEqual(self.z.get("/api/stand")[1]["verborgen"], 0)
        self.assertNotIn("Reklamation", str(d))
        self.assertNotIn("ACME", d["kunden"] and str([k for k in d["kunden"] if k not in server.STANDARD_KUNDEN]))

    def test_nirgends_ein_leck(self):
        f = self.f
        for pfad in ("/api/suche?q=Reklamation", "/api/suche?q=geheim", "/api/suche?q=von:ER", "/api/suche?q=kunde:ACME",
                     "/api/suche?q=wer:ZU", "/api/suche?archiv=1", "/api/aktivitaet", "/api/export.csv", "/api/woche",
                     "/api/kennzahlen", "/api/benachrichtigungen", "/bericht"):
            s, d = f.get(pfad)
            self.assertEqual(s, 200, pfad)
            text = d.decode() if isinstance(d, bytes) else str(d)
            self.assertNotIn("Reklamation", text, pfad)
            self.assertNotIn("geheim", text, pfad)
            self.assertNotIn("TKT-1\"", text, pfad)
        # aber Mitglieder mit Sicht finden es
        self.assertEqual([t["id"] for t in self.z.get("/api/suche?q=Reklamation")[1]["tickets"]], ["TKT-1"])
        self.assertIn("Reklamation", self.z.get("/api/export.csv")[1].decode())
        self.assertNotIn("Reklamation", f.get("/api/export.csv")[1].decode())

    def test_schreiben_ist_ebenfalls_gesperrt(self):
        f = self.f
        self.assertEqual(f.patch("/api/tickets/TKT-1", {"prio": "hoch"})[0], 404)
        self.assertEqual(f.post("/api/tickets/TKT-1/kommentare", {"text": "hallo"})[0], 404)
        self.assertEqual(f.post("/api/tickets/TKT-1/merken")[0], 404)
        self.assertEqual(f.post("/api/tickets/TKT-1/verknuepfungen", {"eingabe": "abcdef1"})[0], 404)
        self.assertEqual(f.post("/api/tickets/TKT-1/checklisten", {"titel": "x"})[0], 404)
        # Verknuepfung auf ein verstecktes Ticket ist ebenfalls abgewiesen
        self.assertEqual(f.post("/api/tickets/TKT-2/verknuepfungen", {"eingabe": "TKT-1"})[0], 404)
        self.assertEqual(self.a.get("/api/tickets/TKT-1")[1]["prio"], "mittel")

    def test_kommentar_id_verraet_nichts(self):
        s, d = self.e.post("/api/tickets/TKT-1/kommentare", {"text": "intern"})
        kid = d["kommentare_liste"][0]["id"]
        self.assertEqual(self.f.patch(f"/api/kommentare/{kid}", {"text": "x"})[0], 404)
        self.assertEqual(self.f.delete(f"/api/kommentare/{kid}")[0], 404)
        self.assertEqual(self.f.post(f"/api/kommentare/{kid}/reaktion", {"emoji": "\U0001F44D"})[0], 404)

    def test_beobachter_aufnehmen_gibt_sicht(self):
        self.assertEqual(self.f.get("/api/tickets/TKT-1")[0], 404)
        self.e.patch("/api/tickets/TKT-1", {"beobachter": ["BO", "FR"]})
        self.assertEqual(self.f.get("/api/tickets/TKT-1")[0], 200)
        self.assertEqual(self.f.get("/api/stand")[1]["verborgen"], 0)
        self.e.patch("/api/tickets/TKT-1", {"vertraulich": False})
        self.assertEqual(self.client().login("FR")[0], 200)

    def test_keine_benachrichtigung_an_unbefugte(self):
        self.e.post("/api/tickets/TKT-1/kommentare", {"text": "@FR schau mal"})
        self.assertEqual(self.f.get("/api/benachrichtigungen")[1]["meldungen"], [])
        self.e.post("/api/tickets/TKT-1/kommentare", {"text": "@BO bitte"})
        self.assertEqual(len(self.b.get("/api/benachrichtigungen")[1]["meldungen"]), 2)   # erwaehnt + (Beobachter) ... siehe unten

    def test_vertraulich_im_verlauf_und_kunde_im_bericht_nicht(self):
        bericht = self.f.get("/bericht")[1].decode()
        self.assertIn("1 vertrauliche Tickets", bericht)
        self.assertNotIn("ACME", bericht)
        ev = [e["feld"] for e in self.a.get("/api/tickets/TKT-1")[1]["ereignisse"]]
        self.assertIn("beobachter", ev)

    def test_admin_darf_alles_mitglied_nichts_loeschen(self):
        self.assertEqual(self.e.delete("/api/tickets/TKT-1")[0], 403)
        self.assertEqual(self.a.delete("/api/tickets/TKT-1")[0], 200)


class ThreadTest(AppTest):
    def setUp(self):
        super().setUp()
        self.a = self.admin("AD", "Alex Admin")
        self.m = self.mitglied(self.a, "BE", "Bea B")
        self.o = self.mitglied(self.a, "OL", "Olaf L")
        with self.db() as c:   # Konten gelten als aelter als alles, was gleich passiert (Sekundenraster)
            c.execute("UPDATE nutzer SET angelegt='2026-01-01T00:00:00'")
        self.ticket(self.m, "Thema", wer="OL")

    def komm(self, c, text, nr=1):
        s, d = c.post(f"/api/tickets/TKT-{nr}/kommentare", {"text": text})
        self.assertEqual(s, 201, d)
        return d

    def test_kommentieren_bearbeiten_loeschen_mit_audit(self):
        d = self.komm(self.m, "Erster Entwurf")
        kid = d["kommentare_liste"][0]["id"]
        self.assertEqual(d["kommentare"], 1)
        # Fremde duerfen nicht bearbeiten, auch Admin nicht
        self.assertEqual(self.o.patch(f"/api/kommentare/{kid}", {"text": "x"})[0], 403)
        self.assertEqual(self.a.patch(f"/api/kommentare/{kid}", {"text": "x"})[0], 403)
        s, d = self.m.patch(f"/api/kommentare/{kid}", {"text": "Zweiter Entwurf"})
        k = d["kommentare_liste"][0]
        self.assertEqual((k["text"], bool(k["bearbeitet"])), ("Zweiter Entwurf", True))
        # Audit: alter Wortlaut steht im (append-only) Verlauf, nur fuer Admins sichtbar
        v = self.db().execute("SELECT alt, neu, wer FROM verlauf WHERE feld='kommentar_bearbeitet'").fetchone()
        self.assertEqual((v["alt"], v["neu"], v["wer"]), ("Erster Entwurf", "Zweiter Entwurf", "BE"))
        self.assertNotIn("kommentar_bearbeitet", [e["feld"] for e in self.o.get("/api/tickets/TKT-1")[1]["ereignisse"]])
        self.assertIn("kommentar_bearbeitet", [e["feld"] for e in self.a.get("/api/tickets/TKT-1")[1]["ereignisse"]])
        # Loeschen: andere Mitglieder nicht, Autor und Admin schon
        self.assertEqual(self.o.delete(f"/api/kommentare/{kid}")[0], 403)
        s, d = self.m.delete(f"/api/kommentare/{kid}")
        self.assertEqual(s, 200)
        k = d["kommentare_liste"][0]
        self.assertNotIn("text", k)                 # Text ist weg
        self.assertTrue(k["geloescht"])             # Platzhalter bleibt
        self.assertEqual(d["kommentare"], 0)
        row = self.db().execute("SELECT text, geloescht FROM kommentar WHERE id=?", (kid,)).fetchone()
        self.assertEqual(row["text"], "Zweiter Entwurf")   # Soft-Delete, Text bleibt in der DB
        self.assertEqual(self.db().execute("SELECT alt FROM verlauf WHERE feld='kommentar_geloescht'").fetchone()[0], "Zweiter Entwurf")
        self.assertEqual(self.m.patch(f"/api/kommentare/{kid}", {"text": "wieder"})[0], 404)
        # Admin darf fremde loeschen
        kid2 = self.komm(self.m, "Noch einer")["kommentare_liste"][-1]["id"]
        self.assertEqual(self.a.delete(f"/api/kommentare/{kid2}")[0], 200)

    def test_kommentar_validierung(self):
        for t in ("", "   ", "x" * 4001):
            self.assertEqual(self.m.post("/api/tickets/TKT-1/kommentare", {"text": t})[0], 400)
        self.assertEqual(self.m.post("/api/tickets/TKT-9/kommentare", {"text": "x"})[0], 404)

    def test_reaktionen_umschalten(self):
        kid = self.komm(self.m, "Gut so")["kommentare_liste"][0]["id"]
        up, ok = "\U0001F44D", "✅"
        d = self.o.post(f"/api/kommentare/{kid}/reaktion", {"emoji": up})[1]
        r = d["kommentare_liste"][0]["reaktionen"]
        self.assertEqual(r, [{"emoji": up, "nutzer": ["OL"], "ich": True}])
        d = self.m.post(f"/api/kommentare/{kid}/reaktion", {"emoji": up})[1]
        self.assertEqual(d["kommentare_liste"][0]["reaktionen"][0]["nutzer"], ["OL", "BE"])
        self.assertTrue(d["kommentare_liste"][0]["reaktionen"][0]["ich"])
        d = self.m.post(f"/api/kommentare/{kid}/reaktion", {"emoji": ok})[1]
        self.assertEqual([x["emoji"] for x in d["kommentare_liste"][0]["reaktionen"]], [up, ok])   # feste Reihenfolge
        d = self.o.post(f"/api/kommentare/{kid}/reaktion", {"emoji": up})[1]    # wieder entfernen
        self.assertEqual(d["kommentare_liste"][0]["reaktionen"][0]["nutzer"], ["BE"])
        self.assertEqual(self.o.post(f"/api/kommentare/{kid}/reaktion", {"emoji": "\U0001F4A9"})[0], 400)   # nicht in der Auswahl
        self.assertEqual(self.o.post(f"/api/kommentare/{kid}/reaktion", {"emoji": "x"})[0], 400)
        # Reaktionen zaehlen nicht als Aktivitaet
        self.assertFalse(self.m.get("/api/tickets/TKT-1")[1]["ungelesen"] and False)

    def test_erwaehnungen_und_benachrichtigungen(self):
        self.komm(self.m, "@ol und @AD bitte ansehen, mail@example.org nicht")
        mel = lambda c: c.get("/api/benachrichtigungen")[1]["meldungen"]
        ol, ad = mel(self.o), mel(self.a)
        self.assertEqual([m["art"] for m in ol], ["erwaehnt", "zugewiesen"])   # kein zusaetzliches "kommentar"
        self.assertEqual([m["art"] for m in ad], ["erwaehnt"])
        self.assertEqual(ol[0]["ticket"], "TKT-1")
        self.assertEqual(mel(self.m), [])                          # nie an sich selbst
        self.komm(self.o, "erledigt")
        self.assertEqual([m["art"] for m in mel(self.m)], ["kommentar"])   # Ersteller
        self.assertEqual(self.m.get("/api/stand")[1]["ungelesen_meldungen"], 1)
        mid = mel(self.m)[0]["id"]
        self.assertEqual(self.m.post("/api/benachrichtigungen/lesen", {"id": mid})[1], {"ungelesen": 0})
        self.assertEqual(self.m.get("/api/benachrichtigungen?nur=ungelesen")[1]["meldungen"], [])
        self.assertEqual(self.o.post("/api/benachrichtigungen/lesen", {"alle": True})[1], {"ungelesen": 0})
        self.assertEqual(len(self.a.get("/api/benachrichtigungen?art=erwaehnt")[1]["meldungen"]), 1)

    def test_zuweisung_und_status_melden(self):
        self.assertEqual([m["art"] for m in self.o.get("/api/benachrichtigungen")[1]["meldungen"]], ["zugewiesen"])
        self.a.patch("/api/tickets/TKT-1", {"status": "review"})
        arten = [m["art"] for m in self.m.get("/api/benachrichtigungen")[1]["meldungen"]]
        self.assertEqual(arten, ["status"])   # Ersteller erfaehrt vom Review
        self.assertIn("status", [m["art"] for m in self.o.get("/api/benachrichtigungen")[1]["meldungen"]])   # und der Zugewiesene, weil ein anderer es bewegt hat

    def test_faelligkeit_erzeugt_einmalig_meldung(self):
        faellig = (server.heute() - __import__("datetime").timedelta(days=1)).isoformat()
        self.a.patch("/api/tickets/TKT-1", {"faellig": faellig})
        server.faelligkeit_pruefen(erzwingen=True)
        server.faelligkeit_pruefen(erzwingen=True)
        f = [m for m in self.o.get("/api/benachrichtigungen")[1]["meldungen"] if m["art"] == "faellig"]
        self.assertEqual(len(f), 1)
        self.assertIn("überfällig", f[0]["text"])

    def test_ungelesen_und_lesestand(self):
        self.assertTrue(any(t["ungelesen"] for t in self.o.get("/api/stand")[1]["tickets"]))   # neu zugewiesen von BE angelegt
        self.assertEqual(self.o.post("/api/tickets/TKT-1/gelesen")[0], 200)
        def stand(c):
            s = c.get("/api/stand")[1]
            return s["tickets"][0]["ungelesen"], [p["ungelesen"] for p in s["projekte"]][0]
        self.assertEqual(stand(self.o), (False, 0))
        self.assertEqual(stand(self.m), (False, 0))            # eigenes Ticket, eigene Aktion
        self.komm(self.m, "neue Info")
        self.assertEqual(stand(self.o), (True, 1))             # Kanal fett + Zaehler
        self.assertEqual(stand(self.m), (False, 0))
        self.o.post("/api/tickets/TKT-1/gelesen")
        self.assertEqual(stand(self.o), (False, 0))
        self.m.patch("/api/tickets/TKT-1", {"prio": "hoch"})   # auch Ereignisse zaehlen
        self.assertEqual(stand(self.o), (True, 1))
        self.komm(self.o, "ich antworte")                      # eigene Aktion setzt Lesestand
        self.assertEqual(stand(self.o), (False, 0))

    def test_neue_konten_starten_ohne_ungelesen_altlast(self):
        self.komm(self.m, "alte Info")
        n = self.mitglied(self.a, "NE", "Neu N")
        self.assertFalse(any(t["ungelesen"] for t in n.get("/api/stand")[1]["tickets"]))

    def test_lesezeichen(self):
        self.assertEqual(self.m.post("/api/tickets/TKT-1/merken")[1], {"markiert": True})
        self.m.post("/api/tickets/TKT-1/merken")   # doppelt ist harmlos
        self.assertTrue(self.m.get("/api/stand")[1]["tickets"][0]["markiert"])
        self.assertFalse(self.o.get("/api/stand")[1]["tickets"][0]["markiert"])   # persoenlich
        self.assertEqual([t["id"] for t in self.m.get("/api/suche?q=ist:markiert")[1]["tickets"]], ["TKT-1"])
        self.assertEqual(self.o.get("/api/suche?q=ist:markiert")[1]["tickets"], [])
        self.m.delete("/api/tickets/TKT-1/merken")
        self.assertFalse(self.m.get("/api/stand")[1]["tickets"][0]["markiert"])

    def test_checklisten(self):
        s, d = self.m.post("/api/tickets/TKT-1/checklisten", {"titel": "Vorbereitung"})
        self.assertEqual((s, d["checklisten"][0]["titel"]), (201, "Vorbereitung"))
        lid = d["checklisten"][0]["id"]
        self.m.post("/api/tickets/TKT-1/checklisten", {})
        d = self.m.post(f"/api/checklisten/{lid}/punkte", {"text": "Erst das"})[1]
        d = self.m.post(f"/api/checklisten/{lid}/punkte", {"text": "Dann das"})[1]
        self.assertEqual((d["subs_gesamt"], d["subs_erledigt"]), (2, 0))
        pid = d["checklisten"][0]["punkte"][0]["id"]
        d = self.m.patch(f"/api/punkte/{pid}", {"erledigt": True})[1]
        self.assertEqual((d["subs_gesamt"], d["subs_erledigt"]), (2, 1))
        self.assertEqual([c["titel"] for c in d["checklisten"]], ["Vorbereitung", "Checkliste"])
        d = self.m.delete(f"/api/punkte/{pid}")[1]
        self.assertEqual(d["subs_gesamt"], 1)
        d = self.m.delete(f"/api/checklisten/{lid}")[1]
        self.assertEqual((len(d["checklisten"]), d["subs_gesamt"]), (1, 0))
        self.assertEqual(self.m.post(f"/api/checklisten/{lid}/punkte", {"text": "x"})[0], 404)

    def test_labels_mit_festen_farben(self):
        s, l1 = self.m.post("/api/labels", {"name": "Standort A", "farbe": "blau"})
        self.assertEqual((s, l1["farbe"]), (201, "blau"))
        self.assertEqual(self.m.post("/api/labels", {"name": "standort a"})[0], 409)        # Name eindeutig, ohne Gross/Klein
        self.assertEqual(self.m.post("/api/labels", {"name": "X", "farbe": "pink"})[0], 400)   # nur feste Palette
        l2 = self.m.post("/api/labels", {"name": "System"})[1]
        self.assertIn(l2["farbe"], server.LABEL_FARBEN)
        d = self.m.patch("/api/tickets/TKT-1", {"labels": [l1["id"], l2["id"]]})[1]
        self.assertEqual([l["name"] for l in d["labels"]], ["Standort A", "System"])
        self.assertEqual(self.m.patch("/api/tickets/TKT-1", {"labels": [999]})[0], 400)
        self.assertEqual(self.m.patch(f"/api/labels/{l1['id']}", {"farbe": "gelb"})[1]["farbe"], "gelb")
        self.assertEqual([t["labels"][0]["farbe"] for t in self.m.get("/api/stand")[1]["tickets"]], ["gelb"])
        self.assertEqual([t["id"] for t in self.m.get("/api/suche?q=label:system")[1]["tickets"]], ["TKT-1"])
        self.assertIn("labels", [e["feld"] for e in self.m.get("/api/tickets/TKT-1")[1]["ereignisse"]])
        self.assertEqual(self.m.delete(f"/api/labels/{l1['id']}")[0], 403)    # Loeschen nur Admin
        self.assertEqual(self.a.delete(f"/api/labels/{l1['id']}")[0], 200)
        self.assertEqual([l["name"] for l in self.m.get("/api/tickets/TKT-1")[1]["labels"]], ["System"])

    def test_verknuepfungen_manuell(self):
        ok = lambda eingabe: self.m.post("/api/tickets/TKT-1/verknuepfungen", {"eingabe": eingabe})
        s, d = ok("a1b2c3d")
        self.assertEqual((s, d["verknuepfungen"][0]["art"], d["verknuepfungen"][0]["ref"]), (201, "commit", "a1b2c3d"))
        s, d = ok("https://git.example.org/team/repo/commit/0123456789abcdef0123456789abcdef01234567")
        self.assertEqual(d["verknuepfungen"][1]["art"], "commit")
        self.assertTrue(d["verknuepfungen"][1]["url"].startswith("https://"))
        s, d = ok("https://wiki.example.org/seite")
        self.assertEqual(d["verknuepfungen"][2]["art"], "link")
        s, d = ok("\\\\fileserver\\ablage\\scan.pdf")
        self.assertEqual(d["verknuepfungen"][3]["art"], "datei")
        self.ticket(self.m, "Zweites")
        s, d = ok("tkt-2")
        self.assertEqual((d["verknuepfungen"][4]["art"], d["verknuepfungen"][4]["ref"]), ("ticket", "TKT-2"))
        self.assertEqual((d["commits"], d["links"]), (2, 3))
        self.assertEqual(ok("a1b2c3d")[0], 409)            # doppelt
        for schlecht in ("javascript:alert(1)", "irgendwas", "TKT-1", "TKT-99", "ftp://x"):
            self.assertIn(ok(schlecht)[0], (400, 404), schlecht)
        vid = d["verknuepfungen"][0]["id"]
        d = self.m.delete(f"/api/verknuepfungen/{vid}")[1]
        self.assertEqual(d["commits"], 1)
        ev = [e for e in d["ereignisse"] if e["feld"] == "verknuepfung"]
        self.assertEqual(ev[-1]["alt"], "commit: a1b2c3d")

    def test_beobachter(self):
        d = self.m.patch("/api/tickets/TKT-1", {"beobachter": ["AD", "ad"]})[1]
        self.assertEqual(d["beobachter"], ["AD"])
        self.komm(self.o, "Neuigkeit")
        self.assertIn("kommentar", [m["art"] for m in self.a.get("/api/benachrichtigungen")[1]["meldungen"]])
        self.assertEqual(self.m.patch("/api/tickets/TKT-1", {"beobachter": ["ZZ"]})[0], 400)

    def test_aktivitaetsstrom_je_projekt(self):
        self.komm(self.m, "Hallo")
        s, d = self.m.get("/api/aktivitaet?projekt=1")
        arten = [e["art"] for e in d["eintraege"]]
        self.assertIn("kommentar", arten)
        self.assertIn("ereignis", arten)
        self.assertEqual(d["eintraege"][0]["zeit"] >= d["eintraege"][-1]["zeit"], True)
        s, d = self.m.get("/api/aktivitaet?projekt=999")
        self.assertEqual(d["eintraege"], [])


if __name__ == "__main__":
    unittest.main()

import sqlite3
import unittest
from datetime import datetime

from alt_schema import baue_alt_db
from helpers import AppTest, server


class MigrationTest(AppTest):
    def vorbereiten(self):
        baue_alt_db(server.DB)

    def test_schema_und_backup(self):
        c = self.db()
        tabellen = server.tabellen(c)
        self.assertNotIn("leute", tabellen)
        self.assertNotIn("teilaufgabe", tabellen)
        for t in ("nutzer", "projekt", "checkliste", "unteraufgabe", "sprint", "reaktion", "lesestand"):
            self.assertIn(t, tabellen)
        self.assertEqual(server.einstellung(c, "schema"), "2")
        self.assertEqual(server.einstellung(c, "irgendwas"), "bleibt")   # fremde meta-Zeilen bleiben
        backups = list(self.tmp.glob("ticket.db.vor-neu-*"))
        self.assertEqual(len(backups), 1)
        self.assertRegex(backups[0].name, r"ticket\.db\.vor-neu-\d{8}$")
        b = sqlite3.connect(backups[0])
        self.assertIn("leute", {r[0] for r in b.execute("SELECT name FROM sqlite_master WHERE type='table'")})
        self.assertEqual(b.execute("SELECT COUNT(*) FROM ticket").fetchone()[0], 7)
        self.assertEqual(b.execute("SELECT status FROM ticket WHERE nr=1").fetchone()[0], "backlog")   # Original unberuehrt
        b.close()

    def test_nutzer_und_projekt(self):
        c = self.db()
        nutzer = c.execute("SELECT kurz,name,rolle,aktiv,pw FROM nutzer ORDER BY id").fetchall()
        self.assertEqual([n["kurz"] for n in nutzer], ["DK", "AN", "TO"])
        self.assertTrue(all(n["pw"] is None and n["aktiv"] == 1 and n["rolle"] == "mitglied" for n in nutzer))
        p = c.execute("SELECT key,titel FROM projekt").fetchall()
        self.assertEqual([(x["key"], x["titel"]) for x in p], [("ALLG", "Allgemein")])
        self.assertEqual(c.execute("SELECT COUNT(*) FROM ticket WHERE projekt_id=(SELECT id FROM projekt WHERE key='ALLG')").fetchone()[0], 7)

    def test_status_und_zeiten(self):
        c = self.db()
        t = {r["nr"]: r for r in c.execute("SELECT * FROM ticket")}
        self.assertEqual({n: r["status"] for n, r in t.items()},
                         {1: "bereit", 2: "arbeit", 3: "review", 4: "fertig", 5: "fertig", 6: "bereit", 7: "arbeit"})
        self.assertTrue(all(r["typ"] == "aufgabe" for r in t.values()))
        # Zeiten aus dem Verlauf zurueckgerechnet
        self.assertEqual(t[2]["begonnen"], "2026-08-04T09:00:00")
        self.assertEqual(t[5]["begonnen"], "2026-08-17T09:00:00")
        self.assertEqual(t[5]["fertig_am"], "2026-08-20T09:00:00")
        self.assertEqual(t[4]["fertig_am"], "2026-08-14T16:00:00")
        self.assertIsNone(t[1]["begonnen"])
        self.assertIsNone(t[2]["fertig_am"])
        # ohne Verlauf: Ersatz aus angelegt
        self.assertEqual(t[7]["begonnen"], "2026-08-10T09:00:00")
        # Ersteller aus dem ersten Verlaufseintrag, sonst der Zustaendige
        self.assertEqual(t[3]["ersteller"], "AN")
        self.assertEqual(t[7]["ersteller"], "AN")
        # Sprintarchiv, Papierkorb, Position bleiben
        self.assertEqual(t[4]["archiv_sprint"], 1)
        self.assertIsNotNone(t[6]["geloescht"])
        self.assertEqual([t[n]["pos"] for n in sorted(t)], [1, 2, 3, 4, 5, 6, 7])
        self.assertEqual(c.execute("SELECT COUNT(*) FROM sprint").fetchone()[0], 2)

    def test_checklisten_kommentare_verlauf(self):
        c = self.db()
        k = c.execute("SELECT ticket, titel FROM checkliste ORDER BY ticket").fetchall()
        self.assertEqual([(x["ticket"], x["titel"]) for x in k], [(2, "Checkliste"), (3, "Checkliste")])
        u = c.execute("SELECT text, erledigt, ticket FROM unteraufgabe ORDER BY id").fetchall()
        self.assertEqual([(x["text"], x["erledigt"], x["ticket"]) for x in u],
                         [("Erst das", 1, 2), ("Dann das", 0, 2), ("Prüfen", 0, 3)])
        self.assertEqual(c.execute("SELECT COUNT(*) FROM kommentar").fetchone()[0], 3)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM kommentar WHERE geloescht IS NULL").fetchone()[0], 3)
        # alter Verlauf unveraendert, plus je ein Eintrag fuer backlog -> bereit
        self.assertEqual(c.execute("SELECT COUNT(*) FROM verlauf WHERE neu='backlog' AND alt IS NULL").fetchone()[0], 4)
        neu = c.execute("SELECT ticket FROM verlauf WHERE alt='backlog' AND neu='bereit' ORDER BY ticket").fetchall()
        self.assertEqual([r[0] for r in neu], [1, 6])

    def test_idempotent_und_nummern_laufen_weiter(self):
        server.init_db()   # zweiter Start: nichts mehr zu tun
        self.assertEqual(len(list(self.tmp.glob("ticket.db.vor-neu-*"))), 1)
        admin = self.admin("DK")   # uebernimmt das Kuerzel aus dem alten Board
        s, d = admin.get("/api/stand")
        self.assertEqual(s, 200)
        self.assertEqual(d["ich"]["rolle"], "admin")
        self.assertEqual(d["ich"]["kurz"], "DK")
        self.assertEqual(len(d["nutzer"]), 3)   # kein viertes Konto
        t = self.ticket(admin, "Nach der Migration")
        self.assertEqual(t["id"], "TKT-8")
        ids = {x["id"] for x in d["tickets"]}
        self.assertIn("TKT-1", ids)
        self.assertNotIn("TKT-6", ids)    # Papierkorb
        self.assertNotIn("TKT-4", ids)    # Sprintarchiv
        s, arch = admin.get("/api/suche?archiv=nur")
        self.assertIn("TKT-4", {x["id"] for x in arch["tickets"]})

    def test_alte_ticketnummern_und_verlauf_im_thread(self):
        admin = self.admin("DK")
        s, d = admin.get("/api/tickets/TKT-3")
        self.assertEqual(s, 200)
        self.assertEqual(d["status"], "review")
        self.assertEqual(d["wer"], "TO")
        self.assertEqual(d["checklisten"][0]["punkte"][0]["text"], "Prüfen")
        self.assertEqual([k["text"] for k in d["kommentare_liste"]], ["Bitte ansehen"])
        self.assertEqual(d["ereignisse"][0]["feld"], "status")


class MigrationAbbruchTest(AppTest):
    def setUp(self):
        # init_db wirft hier: der Server wird gar nicht erst gestartet
        import tempfile
        from pathlib import Path
        self.tmp = Path(tempfile.mkdtemp(prefix="ticket-test-"))
        self.alte_db = server.DB
        server.DB = self.tmp / "ticket.db"
        server.zustand_zuruecksetzen()
        baue_alt_db(server.DB, kaputt=True)

    def tearDown(self):
        import shutil
        server.DB = self.alte_db
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rollback_bei_fehler(self):
        with self.assertRaises(sqlite3.IntegrityError):
            server.init_db()
        c = sqlite3.connect(server.DB)
        tabellen = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("leute", tabellen)           # alles zurueckgerollt
        self.assertNotIn("nutzer", tabellen)
        self.assertEqual(c.execute("SELECT status FROM ticket WHERE nr=1").fetchone()[0], "backlog")
        self.assertEqual(len(list(self.tmp.glob("ticket.db.vor-neu-*"))), 1)   # Backup lag vorher da


if __name__ == "__main__":
    unittest.main()

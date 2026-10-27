"""Baut eine synthetische Alt-DB mit dem Schema aus dem alten server.py (Stand 2026-09, vor dem Neubau)."""
import sqlite3

ALT_DDL = """
CREATE TABLE meta(schluessel TEXT PRIMARY KEY, wert TEXT);
CREATE TABLE leute(kurz TEXT PRIMARY KEY, name TEXT NOT NULL, farbe INTEGER NOT NULL DEFAULT 0);
CREATE TABLE sprint(nr INTEGER PRIMARY KEY, start TEXT NOT NULL, ende TEXT NOT NULL, geschlossen TEXT,
    geplant INTEGER, geschafft INTEGER);
CREATE TABLE ticket(
    nr INTEGER PRIMARY KEY, titel TEXT NOT NULL, beschreibung TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL, prio TEXT NOT NULL, wer TEXT NOT NULL REFERENCES leute(kurz),
    sp INTEGER NOT NULL DEFAULT 0, pos REAL NOT NULL DEFAULT 0,
    angelegt TEXT NOT NULL, geaendert TEXT NOT NULL, archiv_sprint INTEGER, geloescht TEXT);
CREATE TABLE teilaufgabe(id INTEGER PRIMARY KEY, ticket INTEGER NOT NULL REFERENCES ticket(nr),
    text TEXT NOT NULL, erledigt INTEGER NOT NULL DEFAULT 0, pos INTEGER NOT NULL DEFAULT 0);
CREATE TABLE kommentar(id INTEGER PRIMARY KEY, ticket INTEGER NOT NULL REFERENCES ticket(nr),
    wer TEXT NOT NULL REFERENCES leute(kurz), text TEXT NOT NULL, zeit TEXT NOT NULL);
CREATE TABLE verlauf(id INTEGER PRIMARY KEY, ticket INTEGER NOT NULL REFERENCES ticket(nr),
    feld TEXT NOT NULL, alt TEXT, neu TEXT, wer TEXT, zeit TEXT NOT NULL);
CREATE INDEX ix_ticket_aktiv ON ticket(archiv_sprint, geloescht);
CREATE INDEX ix_verlauf_ticket ON verlauf(ticket, feld, zeit);
"""


def baue_alt_db(pfad, kaputt=False):
    c = sqlite3.connect(pfad)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(ALT_DDL)
    c.executemany("INSERT INTO leute VALUES(?,?,?)", [("DK", "Dana K", 0), ("AN", "Anna N", 1), ("TO", "Tom O", 2)])
    c.executemany("INSERT INTO sprint VALUES(?,?,?,?,?,?)", [
        (1, "2026-08-01", "2026-08-15", "2026-08-15T10:00:00", 4, 2), (2, "2026-08-15", "2026-08-29", None, None, None)])
    t = [
        # nr, titel, beschr, status, prio, wer, pos, angelegt, geaendert, archiv, geloescht
        (1, "Backlog-Ticket", "Text eins", "backlog", "hoch", "AN", 1, "2026-08-02T09:00:00", "2026-08-02T09:00:00", None, None),
        (2, "In Arbeit", "", "arbeit", "mittel", "DK", 2, "2026-08-03T09:00:00", "2026-08-05T09:00:00", None, None),
        (3, "Im Review", "", "review", "niedrig", "TO", 3, "2026-08-03T10:00:00", "2026-08-06T09:00:00", None, None),
        (4, "Fertig im Sprint 1", "", "fertig", "mittel", "AN", 4, "2026-08-01T09:00:00", "2026-08-15T10:00:00", 1, None),
        (5, "Fertig aktuell", "", "fertig", "hoch", "DK", 5, "2026-08-16T09:00:00", "2026-08-20T09:00:00", None, None),
        (6, "Gelöscht", "", "backlog", "mittel", "TO", 6, "2026-08-16T09:00:00", "2026-08-16T09:00:00", None, "2026-08-17T09:00:00"),
        (7, "Ohne Verlauf in Arbeit", "", "arbeit", "mittel", "AN", 7, "2026-08-10T09:00:00", "2026-08-11T09:00:00", None, None),
    ]
    c.executemany("INSERT INTO ticket(nr,titel,beschreibung,status,prio,wer,pos,angelegt,geaendert,archiv_sprint,geloescht) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?)", t)
    v = [
        (1, "status", None, "backlog", "DK", "2026-08-02T09:00:00"),
        (2, "status", None, "backlog", "DK", "2026-08-03T09:00:00"),
        (2, "status", "backlog", "arbeit", "DK", "2026-08-04T09:00:00"),
        (3, "status", None, "backlog", "AN", "2026-08-03T10:00:00"),
        (3, "status", "backlog", "arbeit", "TO", "2026-08-04T10:00:00"),
        (3, "status", "arbeit", "review", "TO", "2026-08-06T09:00:00"),
        (4, "status", None, "arbeit", "AN", "2026-08-01T09:00:00"),
        (4, "status", "arbeit", "fertig", "AN", "2026-08-14T16:00:00"),
        (5, "status", None, "backlog", "DK", "2026-08-16T09:00:00"),
        (5, "status", "backlog", "arbeit", "DK", "2026-08-17T09:00:00"),
        (5, "status", "arbeit", "fertig", "DK", "2026-08-20T09:00:00"),
    ]
    c.executemany("INSERT INTO verlauf(ticket,feld,alt,neu,wer,zeit) VALUES(?,?,?,?,?,?)", v)
    c.executemany("INSERT INTO teilaufgabe(ticket,text,erledigt,pos) VALUES(?,?,?,?)",
                  [(2, "Erst das", 1, 0), (2, "Dann das", 0, 1), (3, "Prüfen", 0, 0)])
    c.executemany("INSERT INTO kommentar(ticket,wer,text,zeit) VALUES(?,?,?,?)",
                  [(2, "DK", "Läuft.", "2026-08-05T09:00:00"), (2, "AN", "Danke!", "2026-08-05T10:00:00"),
                   (3, "TO", "Bitte ansehen", "2026-08-06T09:05:00")])
    c.execute("INSERT INTO meta VALUES('irgendwas','bleibt')")
    if kaputt:   # Verweis auf eine Person, die es nicht gibt -> foreign_key_check muss die Migration stoppen
        c.execute("UPDATE ticket SET wer='XX' WHERE nr=7")
    c.commit()
    c.close()

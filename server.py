#!/usr/bin/env python3
"""ticket. - internes Ticket-Board fuer Teams.

Python-stdlib + SQLite. Lauscht nur auf 127.0.0.1; nginx macht TLS. Eigene Konten
(scrypt, Session-Cookie, CSRF-Token), Projekte als Kanaele, Board mit fuenf Listen,
Tickets als Thread mit Kommentaren, Checklisten, Verknuepfungen, Webhook fuer Commits,
Wochenansicht, Kennzahlen und ein druckbarer A4-Statusbericht.

Alles Zeitliche in Ortszeit (TICKET_TZ, Standard Europe/Vienna) als ISO-Text.
Kein Netz nach aussen, kein Mailversand.
"""
import base64
import csv
import hashlib
import hmac
import html
import io
import json
import math
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(os.environ.get("TICKET_TZ", "Europe/Vienna"))
except Exception:  # keine tzdata: Systemzeit
    TZ = None

DB = Path(os.environ.get("TICKET_DB", "/var/lib/ticket/ticket.db"))
WEB = Path(__file__).resolve().parent / "web"
PORT = int(os.environ.get("TICKET_PORT", "8909"))
PREFIX = "TKT"
SCHEMA_VERSION = 2

STATUS = ("neu", "bereit", "arbeit", "review", "fertig", "verworfen")
SPALTEN = STATUS[:5]
OFFEN = ("neu", "bereit", "arbeit", "review")
PRIOS = ("hoch", "mittel", "niedrig")
TYPEN = ("thema", "aufgabe", "fehler", "anfrage")
WERT_KLASSEN = ("kein", "klein", "mittel", "gross")
PROJEKT_STATUS = ("offen", "ruhend", "abgeschlossen")
ROLLEN = ("mitglied", "admin")
LABEL_FARBEN = ("grau", "blau", "gruen", "gelb", "rot", "lila")
REAKTIONEN = ("\U0001F44D", "✅", "\U0001F440", "❓")  # Daumen, Haken, Augen, Fragezeichen
STATUS_EMOJI = ("\U0001F334", "\U0001F912", "\U0001F37D️", "\U0001F4AC", "\U0001F3E0", "\U0001F697")
PRAESENZ_MODI = ("auto", "abwesend", "nicht_stoeren")

ARCHIV_TAGE = 14
SESSION_TAGE = 90
EINLADUNG_TAGE = 7
PASSWORT_MIN = 10
SCRYPT_N = 2 ** 14
MAX_BODY = 64 * 1024
MAX_HOOK_BODY = 1024 * 1024
PRAESENZ_SEKUNDEN = 120
# X-Real-IP nur glauben, wenn ein eigener Reverse-Proxy davorsteht, der den Header selbst setzt.
TRUST_PROXY = os.environ.get("TICKET_TRUST_PROXY", "").strip().lower() in ("1", "ja", "true", "yes")
SOCKET_TIMEOUT = 30
VERLAUF_FELDER = ("status", "wer", "prio", "titel", "typ", "projekt", "faellig", "kunde", "wert",
                  "blockiert", "vertraulich", "beobachter", "labels", "beschreibung")
DEFAULTS = {"wip_arbeit": "3", "wip_review": "5", "stillstand_tage": "10",
            "arbeitsbereich": "ticket.", "praesenz_anzeigen": "1"}
STANDARD_KUNDEN = tuple(k.strip() for k in os.environ.get("TICKET_KUNDEN", "intern").split(",") if k.strip())

_lock = threading.Lock()


# ---------------------------------------------------------------- zeit, helfer

def jetzt():
    return datetime.now(TZ).replace(tzinfo=None).isoformat(timespec="seconds")


def heute():
    return datetime.now(TZ).date()


def vor_tagen(n):
    return (datetime.fromisoformat(jetzt()) - timedelta(days=n)).isoformat(timespec="seconds")


class Fehler(Exception):
    def __init__(self, code, text, **extra):
        super().__init__(text)
        self.code, self.text, self.extra = code, text, extra


def conn():
    c = sqlite3.connect(DB, timeout=10, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.create_function("cf", 1, lambda s: s.casefold() if isinstance(s, str) else "", deterministic=True)
    return c


@contextmanager
def lesen():
    c = conn()
    try:
        yield c
    finally:
        c.close()


@contextmanager
def schreiben():
    """Eine Transaktion unter dem globalen Schreibschloss."""
    with _lock:
        c = conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            yield c
            c.execute("COMMIT")
        except BaseException:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            c.close()


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def gleich(a, b):
    """Zeitkonstanter Vergleich zweier Texte; kommt auch mit Nicht-ASCII zurecht (compare_digest auf str nicht)."""
    return hmac.compare_digest(str(a).encode("utf-8", "surrogatepass"), str(b).encode("utf-8", "surrogatepass"))


def text_feld(daten, name, max_len, pflicht=False, label=None):
    v = daten.get(name, "")
    if v is None:
        v = ""
    if not isinstance(v, str):
        raise Fehler(400, f"{label or name} muss Text sein")
    v = v.strip()
    if pflicht and not v:
        raise Fehler(400, f"{label or name} fehlt")
    if len(v) > max_len:
        raise Fehler(400, f"{label or name} ist zu lang")
    return v


def datum_pruefen(v, name="Datum"):
    if v is None or v == "":
        return None
    if not isinstance(v, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        raise Fehler(400, f"{name}: Format JJJJ-MM-TT")
    try:
        date.fromisoformat(v)
    except ValueError:
        raise Fehler(400, f"{name} ist kein Datum")
    return v


def tage_zwischen(a, b):
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 86400


def perzentil(werte, p):
    s = sorted(werte)
    if not s:
        return None
    k = (len(s) - 1) * p
    lo = math.floor(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


# ---------------------------------------------------------------- schema

def ddl_ticket(name):
    return f"""CREATE TABLE {name}(
        nr INTEGER PRIMARY KEY,
        projekt_id INTEGER NOT NULL REFERENCES projekt(id),
        typ TEXT NOT NULL DEFAULT 'aufgabe',
        titel TEXT NOT NULL,
        beschreibung TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL,
        prio TEXT NOT NULL DEFAULT 'mittel',
        wer TEXT REFERENCES nutzer(kurz),
        ersteller TEXT REFERENCES nutzer(kurz),
        faellig TEXT,
        kunde TEXT,
        wert_klasse TEXT,
        wert_eur INTEGER,
        vertraulich INTEGER NOT NULL DEFAULT 0,
        blockiert_grund TEXT,
        blockiert_seit TEXT,
        pos REAL NOT NULL DEFAULT 0,
        angelegt TEXT NOT NULL,
        geaendert TEXT NOT NULL,
        begonnen TEXT,
        fertig_am TEXT,
        archiv_sprint INTEGER,
        geloescht TEXT)"""


def ddl_kommentar(name):
    return f"""CREATE TABLE {name}(
        id INTEGER PRIMARY KEY,
        ticket INTEGER NOT NULL REFERENCES ticket(nr),
        wer TEXT NOT NULL REFERENCES nutzer(kurz),
        text TEXT NOT NULL,
        zeit TEXT NOT NULL,
        bearbeitet TEXT,
        geloescht TEXT)"""


SCHEMA = [
    "CREATE TABLE IF NOT EXISTS meta(schluessel TEXT PRIMARY KEY, wert TEXT)",
    """CREATE TABLE IF NOT EXISTS nutzer(
        id INTEGER PRIMARY KEY,
        kurz TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        rolle TEXT NOT NULL DEFAULT 'mitglied' CHECK(rolle IN ('mitglied','admin')),
        farbe INTEGER NOT NULL DEFAULT 0,
        aktiv INTEGER NOT NULL DEFAULT 1,
        pw TEXT,
        angelegt TEXT NOT NULL,
        gesehen TEXT,
        praesenz TEXT NOT NULL DEFAULT 'auto',
        status_emoji TEXT,
        status_text TEXT,
        status_bis TEXT)""",
    """CREATE TABLE IF NOT EXISTS sitzung(
        id INTEGER PRIMARY KEY,
        token TEXT NOT NULL UNIQUE,
        nutzer INTEGER NOT NULL REFERENCES nutzer(id),
        csrf TEXT NOT NULL,
        erstellt TEXT NOT NULL,
        gesehen TEXT NOT NULL,
        ablauf TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS einladung(
        id INTEGER PRIMARY KEY,
        token TEXT NOT NULL UNIQUE,
        rolle TEXT NOT NULL DEFAULT 'mitglied',
        nutzer INTEGER REFERENCES nutzer(id),
        von INTEGER REFERENCES nutzer(id),
        erstellt TEXT NOT NULL,
        ablauf TEXT NOT NULL,
        benutzt TEXT)""",
    """CREATE TABLE IF NOT EXISTS projekt(
        id INTEGER PRIMARY KEY,
        key TEXT NOT NULL UNIQUE,
        titel TEXT NOT NULL,
        ziel TEXT NOT NULL DEFAULT '',
        eigner TEXT REFERENCES nutzer(kurz),
        status TEXT NOT NULL DEFAULT 'offen',
        angelegt TEXT NOT NULL,
        geschlossen TEXT,
        geloescht TEXT)""",
    """CREATE TABLE IF NOT EXISTS sprint(
        nr INTEGER PRIMARY KEY,
        start TEXT NOT NULL,
        ende TEXT NOT NULL,
        geschlossen TEXT,
        geplant INTEGER,
        geschafft INTEGER)""",
    """CREATE TABLE IF NOT EXISTS verlauf(
        id INTEGER PRIMARY KEY,
        ticket INTEGER NOT NULL REFERENCES ticket(nr),
        feld TEXT NOT NULL,
        alt TEXT,
        neu TEXT,
        wer TEXT,
        zeit TEXT NOT NULL)""",
]

SCHEMA_TICKET_ABHAENGIG = [
    """CREATE TABLE IF NOT EXISTS checkliste(
        id INTEGER PRIMARY KEY,
        ticket INTEGER NOT NULL REFERENCES ticket(nr),
        titel TEXT NOT NULL,
        pos INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS unteraufgabe(
        id INTEGER PRIMARY KEY,
        ticket INTEGER NOT NULL REFERENCES ticket(nr),
        checkliste INTEGER NOT NULL REFERENCES checkliste(id),
        text TEXT NOT NULL,
        erledigt INTEGER NOT NULL DEFAULT 0,
        pos INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS beobachter(
        ticket INTEGER NOT NULL REFERENCES ticket(nr),
        nutzer TEXT NOT NULL REFERENCES nutzer(kurz),
        PRIMARY KEY(ticket, nutzer))""",
    """CREATE TABLE IF NOT EXISTS label(
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE COLLATE NOCASE,
        farbe TEXT NOT NULL DEFAULT 'grau')""",
    """CREATE TABLE IF NOT EXISTS ticket_label(
        ticket INTEGER NOT NULL REFERENCES ticket(nr),
        label_id INTEGER NOT NULL REFERENCES label(id),
        PRIMARY KEY(ticket, label_id))""",
    """CREATE TABLE IF NOT EXISTS verknuepfung(
        id INTEGER PRIMARY KEY,
        ticket INTEGER NOT NULL REFERENCES ticket(nr),
        art TEXT NOT NULL,
        ref TEXT NOT NULL,
        url TEXT,
        titel TEXT,
        autor TEXT,
        ziel_ticket INTEGER,
        quelle TEXT NOT NULL DEFAULT 'manuell',
        wer TEXT,
        zeit TEXT NOT NULL,
        UNIQUE(ticket, art, ref))""",
    """CREATE TABLE IF NOT EXISTS reaktion(
        kommentar INTEGER NOT NULL REFERENCES kommentar(id),
        nutzer TEXT NOT NULL REFERENCES nutzer(kurz),
        emoji TEXT NOT NULL,
        zeit TEXT NOT NULL,
        PRIMARY KEY(kommentar, nutzer, emoji))""",
    """CREATE TABLE IF NOT EXISTS benachrichtigung(
        id INTEGER PRIMARY KEY,
        nutzer TEXT NOT NULL REFERENCES nutzer(kurz),
        ticket INTEGER NOT NULL REFERENCES ticket(nr),
        art TEXT NOT NULL,
        text TEXT NOT NULL,
        zeit TEXT NOT NULL,
        gelesen TEXT)""",
    """CREATE TABLE IF NOT EXISTS lesestand(
        nutzer TEXT NOT NULL REFERENCES nutzer(kurz),
        ticket INTEGER NOT NULL REFERENCES ticket(nr),
        zeit TEXT NOT NULL,
        kid INTEGER NOT NULL DEFAULT 0,
        vid INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(nutzer, ticket))""",
    """CREATE TABLE IF NOT EXISTS lesezeichen(
        nutzer TEXT NOT NULL REFERENCES nutzer(kurz),
        ticket INTEGER NOT NULL REFERENCES ticket(nr),
        zeit TEXT NOT NULL,
        PRIMARY KEY(nutzer, ticket))""",
]

INDIZES = [
    "CREATE INDEX IF NOT EXISTS ix_ticket_status ON ticket(status, geloescht)",
    "CREATE INDEX IF NOT EXISTS ix_ticket_wer ON ticket(wer, status)",
    "CREATE INDEX IF NOT EXISTS ix_ticket_projekt ON ticket(projekt_id)",
    "CREATE INDEX IF NOT EXISTS ix_kommentar_ticket ON kommentar(ticket)",
    "CREATE INDEX IF NOT EXISTS ix_verlauf_ticket ON verlauf(ticket, feld, zeit)",
    "CREATE INDEX IF NOT EXISTS ix_benachr ON benachrichtigung(nutzer, gelesen)",
    "CREATE INDEX IF NOT EXISTS ix_sitzung_nutzer ON sitzung(nutzer)",
]

# Der Verlauf ist das Audit: die Datenbank selbst verbietet Aendern und Loeschen.
TRIGGER = [
    """CREATE TRIGGER IF NOT EXISTS verlauf_kein_update BEFORE UPDATE ON verlauf
       BEGIN SELECT RAISE(ABORT, 'verlauf ist append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS verlauf_kein_delete BEFORE DELETE ON verlauf
       BEGIN SELECT RAISE(ABORT, 'verlauf ist append-only'); END""",
]


def tabellen(c):
    return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def schema_anlegen(c):
    """Idempotent; wird auch auf eine frisch migrierte DB angewandt."""
    for s in SCHEMA:
        c.execute(s)
    t = tabellen(c)
    if "ticket" not in t:
        c.execute(ddl_ticket("ticket"))
    if "kommentar" not in t:
        c.execute(ddl_kommentar("kommentar"))
    for s in SCHEMA_TICKET_ABHAENGIG:
        c.execute(s)
    for s in INDIZES + TRIGGER:
        c.execute(s)
    for k, v in DEFAULTS.items():
        c.execute("INSERT OR IGNORE INTO meta VALUES(?,?)", (k, v))
    c.execute("INSERT OR REPLACE INTO meta VALUES('schema', ?)", (str(SCHEMA_VERSION),))
    if not c.execute("SELECT 1 FROM projekt").fetchone():
        c.execute("INSERT INTO projekt(key,titel,ziel,angelegt) VALUES('ALLG','Allgemein',"
                  "'Alles, was zu keinem Vorhaben gehört',?)", (jetzt(),))


def schema_zustand(c):
    t = tabellen(c)
    if "nutzer" in t:
        return "neu"
    if "leute" in t or "ticket" in t:
        return "alt"
    return "leer"


# ---------------------------------------------------------------- migration

def backup_pfad():
    tag = datetime.now(TZ).strftime("%Y%m%d")
    p = DB.with_name(f"{DB.name}.vor-neu-{tag}")
    if p.exists():
        p = DB.with_name(f"{DB.name}.vor-neu-{tag}-{datetime.now(TZ).strftime('%H%M%S')}")
    return p


def migrieren():
    """Altes Schema (leute/teilaufgabe/backlog) -> Schema 2. Backup davor, eine Transaktion."""
    pfad = backup_pfad()
    quelle = sqlite3.connect(DB)
    ziel = sqlite3.connect(pfad)
    try:
        quelle.backup(ziel)   # konsistent, auch mit WAL
    finally:
        ziel.close()
        quelle.close()
    c = sqlite3.connect(DB, timeout=10, isolation_level=None)
    c.row_factory = sqlite3.Row
    t = jetzt()
    try:
        c.execute("PRAGMA foreign_keys=OFF")
        c.execute("BEGIN IMMEDIATE")
        for s in SCHEMA:
            c.execute(s)
        # nutzer.farbe bleibt als Altwert; Rollen setzt der erste Admin im Setup/Admin-Bereich.
        c.execute("INSERT INTO nutzer(kurz,name,rolle,farbe,aktiv,angelegt) "
                  "SELECT kurz,name,'mitglied',farbe,1,? FROM leute ORDER BY rowid", (t,))
        c.execute("INSERT INTO projekt(key,titel,ziel,angelegt) VALUES('ALLG','Allgemein',"
                  "'Alles, was zu keinem Vorhaben gehört',?)", (t,))
        pid = c.execute("SELECT id FROM projekt WHERE key='ALLG'").fetchone()[0]
        c.execute(ddl_ticket("ticket_neu"))
        c.execute(ddl_kommentar("kommentar_neu"))
        # Verlauf fuer die Statusumstellung anhaengen (append-only bleibt gewahrt).
        c.execute("INSERT INTO verlauf(ticket,feld,alt,neu,wer,zeit) "
                  "SELECT nr,'status','backlog','bereit',NULL,? FROM ticket WHERE status='backlog'", (t,))
        c.execute("""INSERT INTO ticket_neu(nr,projekt_id,typ,titel,beschreibung,status,prio,wer,ersteller,
                         pos,angelegt,geaendert,begonnen,fertig_am,archiv_sprint,geloescht)
            SELECT x.nr, ?, 'aufgabe', x.titel, x.beschreibung,
              CASE WHEN x.status IN ('arbeit','review','fertig') THEN x.status ELSE 'bereit' END,
              CASE WHEN x.prio IN ('hoch','mittel','niedrig') THEN x.prio ELSE 'mittel' END,
              x.wer,
              COALESCE((SELECT v.wer FROM verlauf v WHERE v.ticket=x.nr AND v.feld='status' AND v.alt IS NULL
                          AND v.wer IN (SELECT kurz FROM leute) ORDER BY v.zeit, v.id LIMIT 1), x.wer),
              x.pos, x.angelegt, x.geaendert,
              COALESCE((SELECT MIN(v.zeit) FROM verlauf v WHERE v.ticket=x.nr AND v.feld='status'
                          AND v.neu='arbeit'),
                       CASE WHEN x.status IN ('arbeit','review','fertig') THEN x.angelegt END),
              CASE WHEN x.status='fertig' THEN
                   COALESCE((SELECT MAX(v.zeit) FROM verlauf v WHERE v.ticket=x.nr AND v.feld='status'
                               AND v.neu='fertig'), x.geaendert) END,
              x.archiv_sprint, x.geloescht
            FROM ticket x""", (pid,))
        c.execute("INSERT INTO kommentar_neu(id,ticket,wer,text,zeit) SELECT id,ticket,wer,text,zeit FROM kommentar")
        c.execute("DROP TABLE kommentar")
        c.execute("DROP TABLE ticket")   # Indizes ix_ticket_aktiv gehen mit
        c.execute("ALTER TABLE ticket_neu RENAME TO ticket")
        c.execute("ALTER TABLE kommentar_neu RENAME TO kommentar")
        for s in SCHEMA_TICKET_ABHAENGIG:
            c.execute(s)
        # teilaufgabe -> checkliste "Checkliste" + unteraufgabe
        if "teilaufgabe" in {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
            c.execute("INSERT INTO checkliste(ticket,titel,pos) "
                      "SELECT DISTINCT ticket,'Checkliste',0 FROM teilaufgabe")
            c.execute("INSERT INTO unteraufgabe(id,ticket,checkliste,text,erledigt,pos) "
                      "SELECT s.id,s.ticket,(SELECT k.id FROM checkliste k WHERE k.ticket=s.ticket),"
                      "s.text,s.erledigt,s.pos FROM teilaufgabe s")
            c.execute("DROP TABLE teilaufgabe")
        c.execute("DROP TABLE leute")
        for s in INDIZES + TRIGGER:
            c.execute(s)
        for k, v in DEFAULTS.items():
            c.execute("INSERT OR IGNORE INTO meta VALUES(?,?)", (k, v))
        c.execute("INSERT OR REPLACE INTO meta VALUES('schema', ?)", (str(SCHEMA_VERSION),))
        c.execute("INSERT OR REPLACE INTO meta VALUES('migriert', ?)", (t,))
        schaeden = c.execute("PRAGMA foreign_key_check").fetchall()
        if schaeden:
            raise sqlite3.IntegrityError(f"foreign_key_check: {len(schaeden)} Verstöße")
        c.execute("COMMIT")
    except BaseException:
        try:
            c.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        c.close()
    return pfad


def init_db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = conn()
    try:
        c.execute("PRAGMA journal_mode=WAL")
        zustand = schema_zustand(c)
    finally:
        c.close()
    if zustand == "alt":
        pfad = migrieren()
        print(f"ticket.: altes Schema migriert, Backup {pfad}", file=sys.stderr)
    with schreiben() as c:
        schema_anlegen(c)


# ---------------------------------------------------------------- routen, kontext

ROUTEN = []


def route(methode, muster, auth="nutzer", schreibend=None):
    """auth: None = oeffentlich, 'nutzer' = angemeldet, 'admin'."""
    if schreibend is None:
        schreibend = methode != "GET"

    def deko(f):
        ROUTEN.append((methode, re.compile("^" + muster + "$"), f, auth, schreibend))
        return f
    return deko


class Antwort:
    def __init__(self, code=200, body=b"", ctype="text/plain; charset=utf-8", headers=None):
        self.code, self.body, self.ctype, self.headers = code, body, ctype, headers or []


class Ctx:
    def __init__(self, handler, query, nutzer, sitzung, c):
        self.h, self.query, self.nutzer, self.sitzung, self.c = handler, query, nutzer, sitzung, c
        self.cookies = []

    @property
    def ich(self):
        return self.nutzer["kurz"]

    @property
    def admin(self):
        return bool(self.nutzer) and self.nutzer["rolle"] == "admin"

    @property
    def ip(self):
        if TRUST_PROXY:
            echte = (self.h.headers.get("X-Real-IP") or "").strip()[:64]
            if echte:
                return echte
        return self.h.client_address[0]

    @property
    def https(self):
        return self.h.headers.get("X-Forwarded-Proto", "").lower() == "https"

    def body(self, limit=MAX_BODY):
        # Nur JSON: erzwingt bei Cross-Site-Requests einen Preflight.
        if not self.h.headers.get("Content-Type", "").lower().startswith("application/json"):
            raise Fehler(415, "nur JSON")
        roh = self.h.body_roh   # schon gelesen, bevor das Schreibschloss genommen wurde
        if len(roh) > limit:
            raise Fehler(413, "zu groß")
        try:
            d = json.loads(roh or b"{}")
        except (ValueError, RecursionError):
            raise Fehler(400, "kaputtes JSON")
        if not isinstance(d, dict):
            raise Fehler(400, "Objekt erwartet")
        return d

    def setze_cookie(self, token, loeschen=False):
        teile = [f"ticket_session={'' if loeschen else token}", "Path=/", "HttpOnly", "SameSite=Lax",
                 f"Max-Age={0 if loeschen else SESSION_TAGE * 86400}"]
        if self.https:
            teile.append("Secure")
        self.cookies.append("; ".join(teile))


class Drossel:
    """Zaehlt Fehlversuche je Schluessel im Zeitfenster (im Speicher, reicht fuer eine kleine App)."""

    def __init__(self, maximal, fenster):
        self.maximal, self.fenster, self.d, self.l = maximal, fenster, {}, threading.Lock()
        self.max_schluessel = 2000

    def pruefen(self, key):
        key = key[:200]
        jetzt_ = time.monotonic()
        with self.l:
            v = [t for t in self.d.get(key, []) if jetzt_ - t < self.fenster]
            if v:
                self.d[key] = v
            else:
                self.d.pop(key, None)
            if len(v) >= self.maximal:
                raise Fehler(429, "Zu viele Versuche. Bitte ein paar Minuten warten.")

    def fehlschlag(self, key):
        key = key[:200]
        with self.l:
            if len(self.d) > self.max_schluessel:   # Schluessel, die nie wieder geprueft werden, blieben sonst ewig im Speicher
                grenze = time.monotonic() - self.fenster
                self.d = {k: v for k, v in self.d.items() if v and v[-1] > grenze}
            self.d.setdefault(key, []).append(time.monotonic())

    def leeren(self):
        with self.l:
            self.d.clear()


LOGIN_DROSSEL = Drossel(8, 600)
IP_DROSSEL = Drossel(40, 600)
HOOK_DROSSEL = Drossel(20, 300)
SETUP_DROSSEL = Drossel(8, 600)
_setup_code = None
_gesehen_cache = {}


def zustand_zuruecksetzen():
    """Fuer Tests: fluechtigen Zustand leeren."""
    global _setup_code
    _setup_code = None
    globals()["_faellig_zuletzt"] = 0.0
    _gesehen_cache.clear()
    for d in (LOGIN_DROSSEL, IP_DROSSEL, HOOK_DROSSEL, SETUP_DROSSEL):
        d.leeren()


def setup_code():
    global _setup_code
    env = os.environ.get("TICKET_SETUP_CODE")
    if env:
        return env
    if _setup_code is None:
        _setup_code = secrets.token_urlsafe(9)
        print(f"ticket.: Setup-Code für den ersten Admin: {_setup_code}", file=sys.stderr, flush=True)
    return _setup_code


def setup_noetig(c):
    return not c.execute("SELECT 1 FROM nutzer WHERE rolle='admin' AND aktiv=1 AND pw IS NOT NULL").fetchone()


# ---------------------------------------------------------------- passwort, sitzung

def pw_hash(pw):
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=SCRYPT_N, r=8, p=1, maxmem=128 * 1024 * 1024, dklen=32)
    return "scrypt$%d$%s$%s" % (SCRYPT_N, base64.b64encode(salt).decode(), base64.b64encode(h).decode())


def pw_pruefen(pw, gespeichert):
    try:
        art, n, salt, h = gespeichert.split("$")
        if art != "scrypt":
            return False
        erwartet = base64.b64decode(h)
        probe = hashlib.scrypt(pw.encode(), salt=base64.b64decode(salt), n=int(n), r=8, p=1,
                               maxmem=128 * 1024 * 1024, dklen=len(erwartet))
        return hmac.compare_digest(probe, erwartet)
    except (ValueError, TypeError):
        return False


_dummy = None


def dummy_hash():
    global _dummy
    if _dummy is None:
        _dummy = pw_hash("dummy-passwort-fuer-gleiche-laufzeit")
    return _dummy


def passwort_regeln(pw):
    if not isinstance(pw, str) or len(pw) < PASSWORT_MIN:
        raise Fehler(400, f"Passwort: mindestens {PASSWORT_MIN} Zeichen")
    if len(pw) > 200:
        raise Fehler(400, "Passwort ist zu lang")
    return pw


def kurz_regeln(kurz):
    kurz = (kurz or "").strip().upper() if isinstance(kurz, str) else ""
    if not re.fullmatch(r"[A-ZÄÖÜ]{2,3}", kurz):
        raise Fehler(400, "Kürzel: zwei oder drei Buchstaben")
    return kurz


def sitzung_anlegen(c, nutzer_id):
    token, csrf, t = secrets.token_urlsafe(32), secrets.token_urlsafe(24), jetzt()
    c.execute("DELETE FROM sitzung WHERE ablauf < ?", (t,))
    ablauf = (datetime.fromisoformat(t) + timedelta(days=SESSION_TAGE)).isoformat(timespec="seconds")
    c.execute("INSERT INTO sitzung(token,nutzer,csrf,erstellt,gesehen,ablauf) VALUES(?,?,?,?,?,?)",
              (sha(token), nutzer_id, csrf, t, t, ablauf))
    return token


def sitzung_laden(token):
    """Gibt (nutzer, sitzung) oder (None, None). Verlaengert gleitend, hoechstens stuendlich."""
    if not token:
        return None, None
    with lesen() as c:
        row = c.execute("SELECT s.id AS sid, s.csrf, s.gesehen AS sgesehen, n.id, n.kurz, n.name, n.rolle "
                        "FROM sitzung s JOIN nutzer n ON n.id=s.nutzer "
                        "WHERE s.token=? AND s.ablauf > ? AND n.aktiv=1", (sha(token), jetzt())).fetchone()
    if not row:
        return None, None
    if tage_zwischen(row["sgesehen"], jetzt()) > 1 / 24:
        t = jetzt()
        ablauf = (datetime.fromisoformat(t) + timedelta(days=SESSION_TAGE)).isoformat(timespec="seconds")
        with schreiben() as c:
            c.execute("UPDATE sitzung SET gesehen=?, ablauf=? WHERE id=?", (t, ablauf, row["sid"]))
    nutzer = {k: row[k] for k in ("id", "kurz", "name", "rolle")}
    return nutzer, {"id": row["sid"], "csrf": row["csrf"]}


# ---------------------------------------------------------------- nutzer, praesenz

def einstellung(c, schluessel):
    r = c.execute("SELECT wert FROM meta WHERE schluessel=?", (schluessel,)).fetchone()
    return r[0] if r else DEFAULTS.get(schluessel)


def einstellung_int(c, schluessel):
    try:
        return int(einstellung(c, schluessel))
    except (TypeError, ValueError):
        return int(DEFAULTS[schluessel])


def praesenz_von(row, an):
    if not an:
        return None
    if row["praesenz"] == "nicht_stoeren":
        return "nicht_stoeren"
    if row["praesenz"] == "abwesend":
        return "abwesend"
    g = row["gesehen"]
    if g and tage_zwischen(g, jetzt()) * 86400 <= PRAESENZ_SEKUNDEN:
        return "aktiv"
    return "abwesend"


def status_von(row):
    if not (row["status_emoji"] or row["status_text"]):
        return None
    bis = row["status_bis"]
    if bis and bis <= jetzt():
        return None
    return {"emoji": row["status_emoji"] or "", "text": row["status_text"] or "", "bis": bis}


def nutzer_dict(row, an, admin_sicht=False, eigen=False):
    # Die manuelle Wahl (abwesend / nicht stören) ist Präsenzdaten: bei abgeschalteter Anzeige sieht sie nur die Person selbst.
    d = {"kurz": row["kurz"], "name": row["name"], "rolle": row["rolle"], "aktiv": bool(row["aktiv"]),
         "praesenz": praesenz_von(row, an) if row["aktiv"] else None, "status": status_von(row),
         "modus": row["praesenz"] if (an or eigen) else "auto"}
    if admin_sicht:
        d["hat_passwort"] = bool(row["pw"])
    return d


def alle_nutzer(c, admin_sicht=False):
    an = einstellung(c, "praesenz_anzeigen") == "1"
    return [nutzer_dict(r, an, admin_sicht) for r in c.execute("SELECT * FROM nutzer ORDER BY aktiv DESC, name")]


def herzschlag(kurz):
    """Letzte Aktivitaet vermerken, hoechstens alle 30 s je Nutzer (keine Websockets)."""
    n = time.monotonic()
    if n - _gesehen_cache.get(kurz, -1e9) < 30:
        return
    _gesehen_cache[kurz] = n
    with schreiben() as c:
        if einstellung(c, "praesenz_anzeigen") == "1":
            c.execute("UPDATE nutzer SET gesehen=? WHERE kurz=?", (jetzt(), kurz))


# ---------------------------------------------------------------- auth-routen

@route("GET", "/api/status", auth=None)
def r_status(ctx):
    return {"setup": setup_noetig(ctx.c), "angemeldet": bool(ctx.nutzer),
            "arbeitsbereich": einstellung(ctx.c, "arbeitsbereich")}


@route("POST", "/api/setup", auth=None)
def r_setup(ctx):
    c, d = ctx.c, ctx.body()
    SETUP_DROSSEL.pruefen(ctx.ip)
    if not setup_noetig(c):
        raise Fehler(409, "Die Einrichtung ist schon erledigt")
    if not gleich(d.get("code", ""), setup_code()):
        SETUP_DROSSEL.fehlschlag(ctx.ip)
        raise Fehler(403, "Der Setup-Code stimmt nicht")
    name = text_feld(d, "name", 60, pflicht=True)
    kurz = kurz_regeln(d.get("kurz"))
    pw = passwort_regeln(d.get("passwort"))
    vorhanden = c.execute("SELECT * FROM nutzer WHERE kurz=?", (kurz,)).fetchone()
    if vorhanden and vorhanden["pw"]:
        raise Fehler(409, "Kürzel ist schon vergeben")
    if vorhanden:   # Person aus dem alten Board uebernehmen
        c.execute("UPDATE nutzer SET name=?, rolle='admin', aktiv=1, pw=? WHERE id=?",
                  (name, pw_hash(pw), vorhanden["id"]))
        uid = vorhanden["id"]
    else:
        uid = c.execute("INSERT INTO nutzer(kurz,name,rolle,aktiv,pw,angelegt) VALUES(?,?,'admin',1,?,?)",
                        (kurz, name, pw_hash(pw), jetzt())).lastrowid
    ctx.setze_cookie(sitzung_anlegen(c, uid))
    return 201, {"ok": True}


@route("POST", "/api/login", auth=None)
def r_login(ctx):
    c, d = ctx.c, ctx.body()
    kurz = d.get("kurz", "")
    kurz = kurz.strip().upper() if isinstance(kurz, str) else ""
    pw = d.get("passwort", "")
    pw = pw if isinstance(pw, str) else ""
    IP_DROSSEL.pruefen(ctx.ip)
    LOGIN_DROSSEL.pruefen(ctx.ip + "|" + kurz)
    u = c.execute("SELECT * FROM nutzer WHERE kurz=?", (kurz,)).fetchone()
    ok = pw_pruefen(pw, u["pw"] if u and u["pw"] and u["aktiv"] else dummy_hash())
    if not (ok and u and u["pw"] and u["aktiv"]):
        IP_DROSSEL.fehlschlag(ctx.ip)
        LOGIN_DROSSEL.fehlschlag(ctx.ip + "|" + kurz)
        raise Fehler(401, "Kürzel oder Passwort stimmt nicht")
    ctx.setze_cookie(sitzung_anlegen(c, u["id"]))
    return {"ok": True}


@route("POST", "/api/logout", auth="nutzer")
def r_logout(ctx):
    ctx.c.execute("DELETE FROM sitzung WHERE id=?", (ctx.sitzung["id"],))
    ctx.setze_cookie("", loeschen=True)
    return {"ok": True}


@route("POST", "/api/passwort", auth="nutzer")
def r_passwort(ctx):
    d = ctx.body()
    u = ctx.c.execute("SELECT pw FROM nutzer WHERE id=?", (ctx.nutzer["id"],)).fetchone()
    LOGIN_DROSSEL.pruefen(ctx.ip + "|pw|" + ctx.ich)
    if not pw_pruefen(str(d.get("alt", "")), u["pw"] or dummy_hash()):
        LOGIN_DROSSEL.fehlschlag(ctx.ip + "|pw|" + ctx.ich)
        raise Fehler(403, "Das alte Passwort stimmt nicht")
    neu = passwort_regeln(d.get("neu"))
    ctx.c.execute("UPDATE nutzer SET pw=? WHERE id=?", (pw_hash(neu), ctx.nutzer["id"]))
    ctx.c.execute("DELETE FROM sitzung WHERE nutzer=? AND id!=?", (ctx.nutzer["id"], ctx.sitzung["id"]))
    return {"ok": True}


# ---- Einladungen

@route("POST", "/api/einladungen", auth="admin")
def r_einladung_neu(ctx):
    c, d = ctx.c, ctx.body()
    rolle = d.get("rolle", "mitglied")
    if rolle not in ROLLEN:
        raise Fehler(400, "unbekannte Rolle")
    nid = None
    if d.get("kurz"):
        u = c.execute("SELECT id FROM nutzer WHERE kurz=?", (kurz_regeln(d["kurz"]),)).fetchone()
        if not u:
            raise Fehler(404, "Person nicht gefunden")
        nid = u["id"]
    token, t = secrets.token_urlsafe(24), jetzt()
    ablauf = (datetime.fromisoformat(t) + timedelta(days=EINLADUNG_TAGE)).isoformat(timespec="seconds")
    c.execute("INSERT INTO einladung(token,rolle,nutzer,von,erstellt,ablauf) VALUES(?,?,?,?,?,?)",
              (sha(token), rolle, nid, ctx.nutzer["id"], t, ablauf))
    return 201, {"token": token, "pfad": f"/#/einladung/{token}", "ablauf": ablauf}


@route("GET", "/api/einladungen", auth="admin")
def r_einladungen(ctx):
    return [dict(r) for r in ctx.c.execute(
        "SELECT e.id, e.rolle, e.erstellt, e.ablauf, n.kurz AS fuer FROM einladung e "
        "LEFT JOIN nutzer n ON n.id=e.nutzer WHERE e.benutzt IS NULL AND e.ablauf > ? ORDER BY e.id DESC",
        (jetzt(),))]


@route("DELETE", r"/api/einladungen/(\d+)", auth="admin")
def r_einladung_weg(ctx, eid):
    ctx.c.execute("DELETE FROM einladung WHERE id=?", (int(eid),))
    return {"ok": True}


def einladung_holen(ctx, token):
    HOOK_DROSSEL.pruefen(ctx.ip + "|einl")
    row = ctx.c.execute("SELECT * FROM einladung WHERE token=? AND benutzt IS NULL AND ablauf > ?",
                        (sha(token), jetzt())).fetchone()
    if not row:
        HOOK_DROSSEL.fehlschlag(ctx.ip + "|einl")
        raise Fehler(404, "Der Einladungslink ist ungültig oder abgelaufen")
    return row


@route("GET", r"/api/einladung/([A-Za-z0-9_\-]{16,80})", auth=None)
def r_einladung_pruefen(ctx, token):
    e = einladung_holen(ctx, token)
    u = ctx.c.execute("SELECT kurz,name FROM nutzer WHERE id=?", (e["nutzer"],)).fetchone() if e["nutzer"] else None
    return {"gueltig": True, "nutzer": dict(u) if u else None}


@route("POST", r"/api/einladung/([A-Za-z0-9_\-]{16,80})", auth=None)
def r_einladung_annehmen(ctx, token):
    c, d = ctx.c, ctx.body()
    e = einladung_holen(ctx, token)
    name = text_feld(d, "name", 60, pflicht=True)
    pw = passwort_regeln(d.get("passwort"))
    if e["nutzer"]:
        uid = e["nutzer"]
        c.execute("UPDATE nutzer SET name=?, pw=?, aktiv=1 WHERE id=?", (name, pw_hash(pw), uid))
        c.execute("DELETE FROM sitzung WHERE nutzer=?", (uid,))   # Zugang zurücksetzen heißt: alte Sitzungen sind weg
    else:
        kurz = kurz_regeln(d.get("kurz"))
        if c.execute("SELECT 1 FROM nutzer WHERE kurz=?", (kurz,)).fetchone():
            raise Fehler(409, "Kürzel ist schon vergeben")
        uid = c.execute("INSERT INTO nutzer(kurz,name,rolle,aktiv,pw,angelegt) VALUES(?,?,?,1,?,?)",
                        (kurz, name, e["rolle"], pw_hash(pw), jetzt())).lastrowid
    c.execute("UPDATE einladung SET benutzt=? WHERE id=?", (jetzt(), e["id"]))
    ctx.setze_cookie(sitzung_anlegen(c, uid))
    return 201, {"ok": True}


# ---- Personen, Status, Einstellungen

@route("GET", "/api/nutzer", auth="admin")
def r_nutzer(ctx):
    return alle_nutzer(ctx.c, admin_sicht=True)


@route("PATCH", r"/api/nutzer/([A-ZÄÖÜ]{2,3})", auth="admin")
def r_nutzer_aendern(ctx, kurz):
    c, d = ctx.c, ctx.body()
    u = c.execute("SELECT * FROM nutzer WHERE kurz=?", (kurz,)).fetchone()
    if not u:
        raise Fehler(404, "Person nicht gefunden")
    rolle = d.get("rolle", u["rolle"])
    aktiv = 1 if d.get("aktiv", bool(u["aktiv"])) else 0
    name = text_feld(d, "name", 60, pflicht=True) if "name" in d else u["name"]
    if rolle not in ROLLEN:
        raise Fehler(400, "unbekannte Rolle")
    if u["rolle"] == "admin" and u["aktiv"] and (rolle != "admin" or not aktiv):
        andere = c.execute("SELECT COUNT(*) FROM nutzer WHERE rolle='admin' AND aktiv=1 AND pw IS NOT NULL "
                           "AND id!=?", (u["id"],)).fetchone()[0]
        if not andere:
            raise Fehler(409, "Es muss mindestens ein aktiver Admin bleiben")
    c.execute("UPDATE nutzer SET rolle=?, aktiv=?, name=? WHERE id=?", (rolle, aktiv, name, u["id"]))
    if not aktiv:
        c.execute("DELETE FROM sitzung WHERE nutzer=?", (u["id"],))
    return nutzer_dict(c.execute("SELECT * FROM nutzer WHERE id=?", (u["id"],)).fetchone(), False, True)


@route("PATCH", "/api/ich/status", auth="nutzer")
def r_eigenstatus(ctx):
    c, d = ctx.c, ctx.body()
    sets = {}
    if "praesenz" in d:
        if d["praesenz"] not in PRAESENZ_MODI:
            raise Fehler(400, "unbekannte Präsenz")
        sets["praesenz"] = d["praesenz"]
    if "emoji" in d or "text" in d:
        emoji = d.get("emoji") or ""
        if emoji and emoji not in STATUS_EMOJI:
            raise Fehler(400, "Emoji nicht in der Auswahl")
        text = text_feld(d, "text", 60)
        dauer = d.get("dauer", "nie")
        if dauer not in ("heute", "1h", "nie"):
            raise Fehler(400, "Ablauf: heute, 1h oder nie")
        bis = None
        if emoji or text:
            if dauer == "heute":
                bis = jetzt()[:10] + "T23:59:59"
            elif dauer == "1h":
                bis = (datetime.fromisoformat(jetzt()) + timedelta(hours=1)).isoformat(timespec="seconds")
        sets.update(status_emoji=emoji or None, status_text=text or None,
                    status_bis=bis if (emoji or text) else None)
    if sets:
        c.execute("UPDATE nutzer SET " + ",".join(f"{k}=?" for k in sets) + " WHERE id=?",
                  (*sets.values(), ctx.nutzer["id"]))
    an = einstellung(c, "praesenz_anzeigen") == "1"
    return nutzer_dict(c.execute("SELECT * FROM nutzer WHERE id=?", (ctx.nutzer["id"],)).fetchone(), an, eigen=True)


def token_ausgeben(c, name):
    return einstellung(c, name + "_token") or None


@route("GET", "/api/einstellungen", auth="admin")
def r_einstellungen(ctx):
    c = ctx.c
    return {"wip_arbeit": einstellung_int(c, "wip_arbeit"), "wip_review": einstellung_int(c, "wip_review"),
            "stillstand_tage": einstellung_int(c, "stillstand_tage"),
            "arbeitsbereich": einstellung(c, "arbeitsbereich"),
            "praesenz_anzeigen": einstellung(c, "praesenz_anzeigen") == "1",
            "hook_token": token_ausgeben(c, "hook"), "bericht_token": token_ausgeben(c, "bericht")}


@route("PATCH", "/api/einstellungen", auth="admin")
def r_einstellungen_aendern(ctx):
    c, d = ctx.c, ctx.body()
    for k, lo, hi in (("wip_arbeit", 1, 50), ("wip_review", 1, 50), ("stillstand_tage", 1, 365)):
        if k in d:
            v = d[k]
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                raise Fehler(400, f"{k}: Zahl von {lo} bis {hi}")
            c.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, str(v)))
    if "arbeitsbereich" in d:
        c.execute("INSERT OR REPLACE INTO meta VALUES('arbeitsbereich',?)",
                  (text_feld(d, "arbeitsbereich", 40, pflicht=True),))
    if "praesenz_anzeigen" in d:
        c.execute("INSERT OR REPLACE INTO meta VALUES('praesenz_anzeigen',?)", ("1" if d["praesenz_anzeigen"] else "0",))
    return r_einstellungen(ctx)


@route("POST", r"/api/einstellungen/token/(hook|bericht)", auth="admin")
def r_token_neu(ctx, welcher):
    token = secrets.token_urlsafe(24)
    ctx.c.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (welcher + "_token", token))
    return 201, {welcher + "_token": token}


@route("DELETE", r"/api/einstellungen/token/(hook|bericht)", auth="admin")
def r_token_weg(ctx, welcher):
    ctx.c.execute("DELETE FROM meta WHERE schluessel=?", (welcher + "_token",))
    return {"ok": True}


# ---------------------------------------------------------------- tickets: lesen

_faellig_zuletzt = 0.0


def ticket_id(nr):
    return f"{PREFIX}-{nr}"


def sichtbar_sql(nutzer, alias="t"):
    """Vertrauliche Tickets sehen nur Ersteller, Zugewiesene, Beobachter und Admins."""
    if nutzer is None:
        return f"{alias}.vertraulich=0", {}
    if nutzer["rolle"] == "admin":
        return "1=1", {}
    return (f"({alias}.vertraulich=0 OR {alias}.ersteller=:ich OR {alias}.wer=:ich OR EXISTS("
            f"SELECT 1 FROM beobachter bo WHERE bo.ticket={alias}.nr AND bo.nutzer=:ich))"), {"ich": nutzer["kurz"]}


TICKET_SELECT = ("SELECT t.*, pr.key AS projekt_key FROM ticket t "
                 "JOIN projekt pr ON pr.id=t.projekt_id")


def ticket_row(c, nr, nutzer, geloeschte=False):
    sicht, p = sichtbar_sql(nutzer)
    zusatz = "" if geloeschte else " AND t.geloescht IS NULL"
    row = c.execute(f"{TICKET_SELECT} WHERE t.nr=:nr AND {sicht}{zusatz}", {**p, "nr": nr}).fetchone()
    if not row:
        raise Fehler(404, "Dieses Ticket gibt es nicht (mehr)")
    return row


def aggregate(c, nutzer, nr=None):
    """Zaehler, Labels, Beobachter usw. fuer alle Tickets (oder eines) in wenigen Abfragen."""
    w = " WHERE ticket=:nr" if nr else ""
    p = {"nr": nr} if nr else {}
    ag = {"labels": {}, "kom": {}, "subs": {}, "verk": {}, "beob": {}, "markiert": set(), "ungelesen": set()}
    for r in c.execute("SELECT tl.ticket, l.id, l.name, l.farbe FROM ticket_label tl JOIN label l ON l.id=tl.label_id"
                       + (" WHERE tl.ticket=:nr" if nr else "") + " ORDER BY l.name", p):
        ag["labels"].setdefault(r["ticket"], []).append(
            {"id": r["id"], "name": r["name"], "farbe": r["farbe"]})
    for r in c.execute("SELECT ticket, COUNT(*) n FROM kommentar" + (w + " AND" if w else " WHERE")
                       + " geloescht IS NULL GROUP BY ticket", p):
        ag["kom"][r["ticket"]] = r["n"]
    for r in c.execute("SELECT ticket, COUNT(*) n, SUM(erledigt) e FROM unteraufgabe" + w + " GROUP BY ticket", p):
        ag["subs"][r["ticket"]] = (r["n"], r["e"] or 0)
    for r in c.execute("SELECT ticket, art, COUNT(*) n FROM verknuepfung" + w + " GROUP BY ticket, art", p):
        ag["verk"].setdefault(r["ticket"], {})[r["art"]] = r["n"]
    for r in c.execute("SELECT ticket, nutzer FROM beobachter" + w + " ORDER BY nutzer", p):
        ag["beob"].setdefault(r["ticket"], []).append(r["nutzer"])
    if nutzer:
        ich = nutzer["kurz"]
        for r in c.execute("SELECT ticket FROM lesezeichen WHERE nutzer=?", (ich,)):
            ag["markiert"].add(r["ticket"])
        basis = c.execute("SELECT angelegt FROM nutzer WHERE kurz=?", (ich,)).fetchone()[0]
        lese = {r["ticket"]: (r["kid"], r["vid"]) for r in c.execute(
            "SELECT ticket, kid, vid FROM lesestand WHERE nutzer=?", (ich,))}
        # Gelesen heisst: bis zu diesen Kommentar-/Verlauf-IDs (exakt, nicht sekundengenau).
        fremd = {}   # ticket -> [max kommentar-id, max verlauf-id, max zeit]
        for r in c.execute("SELECT ticket, MAX(id) i, MAX(zeit) z FROM kommentar WHERE geloescht IS NULL AND wer!=? "
                           "GROUP BY ticket", (ich,)):
            fremd[r["ticket"]] = [r["i"], 0, r["z"]]
        for r in c.execute("SELECT ticket, MAX(id) i, MAX(zeit) z FROM verlauf WHERE (wer IS NULL OR wer!=?) "
                           "AND feld NOT IN ('kommentar_bearbeitet','kommentar_geloescht') GROUP BY ticket", (ich,)):
            e = fremd.setdefault(r["ticket"], [0, 0, r["z"]])
            e[1], e[2] = r["i"], max(e[2], r["z"])
        for t, (ki, vi, z) in fremd.items():
            if t in lese:
                if ki > lese[t][0] or vi > lese[t][1]:
                    ag["ungelesen"].add(t)
            elif z > basis:
                ag["ungelesen"].add(t)
    return ag


def karte(row, ag):
    nr = row["nr"]
    subs = ag["subs"].get(nr, (0, 0))
    verk = ag["verk"].get(nr, {})
    return {
        "id": ticket_id(nr), "nr": nr, "titel": row["titel"], "status": row["status"], "prio": row["prio"],
        "typ": row["typ"], "wer": row["wer"], "ersteller": row["ersteller"], "projekt": row["projekt_id"],
        "projekt_key": row["projekt_key"], "faellig": row["faellig"], "kunde": row["kunde"],
        "wert_klasse": row["wert_klasse"], "wert_eur": row["wert_eur"], "vertraulich": bool(row["vertraulich"]),
        "blockiert": row["blockiert_grund"], "blockiert_seit": row["blockiert_seit"],
        "labels": ag["labels"].get(nr, []), "beobachter": ag["beob"].get(nr, []),
        "kommentare": ag["kom"].get(nr, 0), "subs_gesamt": subs[0], "subs_erledigt": subs[1],
        "commits": verk.get("commit", 0), "links": sum(n for a, n in verk.items() if a != "commit"),
        "pos": row["pos"], "angelegt": row["angelegt"], "geaendert": row["geaendert"],
        "begonnen": row["begonnen"], "fertig_am": row["fertig_am"], "archiv_sprint": row["archiv_sprint"],
        "geloescht": row["geloescht"], "markiert": nr in ag["markiert"], "ungelesen": nr in ag["ungelesen"],
    }


def archiv_bedingung():
    return ("(t.archiv_sprint IS NOT NULL OR (t.status='fertig' AND COALESCE(t.fertig_am,t.geaendert) < :g)"
            " OR (t.status='verworfen' AND t.geaendert < :g))")


def kunden_liste(c, nutzer=None):
    """Standardkunden plus die Kunden der Tickets, die diese Person sehen darf (vertrauliche verraten keinen Namen)."""
    namen = {k.casefold(): k for k in STANDARD_KUNDEN}
    sicht, p = sichtbar_sql(nutzer)
    for r in c.execute(f"SELECT DISTINCT t.kunde FROM ticket t WHERE t.kunde IS NOT NULL AND t.geloescht IS NULL AND {sicht}", p):
        namen.setdefault(r[0].casefold(), r[0])
    return sorted(namen.values(), key=str.casefold)


def projekte_liste(c, karten):
    offen, ungelesen = {}, {}
    for k in karten:
        if k["status"] in OFFEN:
            offen[k["projekt"]] = offen.get(k["projekt"], 0) + 1
        if k["ungelesen"] and k["status"] != "verworfen":
            ungelesen[k["projekt"]] = ungelesen.get(k["projekt"], 0) + 1
    out = []
    for p in c.execute("SELECT * FROM projekt WHERE geloescht IS NULL ORDER BY (key='ALLG') DESC, titel COLLATE NOCASE"):
        d = {k: p[k] for k in ("id", "key", "titel", "ziel", "eigner", "status", "angelegt", "geschlossen")}
        d["offen"], d["ungelesen"] = offen.get(p["id"], 0), ungelesen.get(p["id"], 0)
        out.append(d)
    return out


def ungelesene_meldungen(c, nutzer):
    """Zaehlt wie die Liste: nur Meldungen zu Tickets, die es noch gibt und die die Person sehen darf."""
    sicht, p = sichtbar_sql(nutzer)
    return c.execute("SELECT COUNT(*) FROM benachrichtigung b JOIN ticket t ON t.nr=b.ticket "
                     f"WHERE b.nutzer=:nutzer AND b.gelesen IS NULL AND t.geloescht IS NULL AND {sicht}",
                     {**p, "nutzer": nutzer["kurz"]}).fetchone()[0]


def etag_antwort(ctx, obj):
    body = json.dumps(obj, ensure_ascii=False).encode()
    tag = '"' + hashlib.sha256(body).hexdigest()[:20] + '"'
    if ctx.h.headers.get("If-None-Match") == tag:
        return Antwort(304, b"", headers=[("ETag", tag)])
    return Antwort(200, body, "application/json; charset=utf-8", [("ETag", tag)])


@route("GET", "/api/stand")
def r_stand(ctx):
    herzschlag(ctx.ich)
    faelligkeit_pruefen()
    c, nutzer = ctx.c, ctx.nutzer
    sicht, p = sichtbar_sql(nutzer)
    g = vor_tagen(ARCHIV_TAGE)
    rows = c.execute(f"{TICKET_SELECT} WHERE t.geloescht IS NULL AND NOT {archiv_bedingung()} AND {sicht} "
                     "ORDER BY t.pos, t.nr", {**p, "g": g}).fetchall()
    ag = aggregate(c, nutzer)
    karten = [karte(r, ag) for r in rows]
    verborgen = 0
    if nutzer["rolle"] != "admin":
        verborgen = c.execute(f"SELECT COUNT(*) FROM ticket t WHERE t.geloescht IS NULL AND t.vertraulich=1 "
                              f"AND NOT {archiv_bedingung()} AND NOT {sicht}", {**p, "g": g}).fetchone()[0]
    an = einstellung(c, "praesenz_anzeigen") == "1"
    ich = c.execute("SELECT * FROM nutzer WHERE id=?", (nutzer["id"],)).fetchone()
    ich_d = nutzer_dict(ich, an, eigen=True)
    ich_d["csrf"] = ctx.sitzung["csrf"]
    return etag_antwort(ctx, {
        "ich": ich_d, "nutzer": alle_nutzer(c), "projekte": projekte_liste(c, karten),
        "labels": [dict(r) for r in c.execute("SELECT id,name,farbe FROM label ORDER BY name COLLATE NOCASE")],
        "kunden": kunden_liste(c, nutzer), "tickets": karten, "verborgen": verborgen,
        "ungelesen_meldungen": ungelesene_meldungen(c, nutzer),
        "einstellungen": {"wip_arbeit": einstellung_int(c, "wip_arbeit"), "wip_review": einstellung_int(c, "wip_review"),
                          "arbeitsbereich": einstellung(c, "arbeitsbereich"), "praesenz_anzeigen": an},
        "heute": heute().isoformat(), "emoji_reaktionen": list(REAKTIONEN), "emoji_status": list(STATUS_EMOJI),
    })


def ticket_detail(c, nutzer, nr):
    row = ticket_row(c, nr, nutzer, geloeschte=nutzer["rolle"] == "admin")
    ag = aggregate(c, nutzer, nr)
    d = karte(row, ag)
    d["beschreibung"] = row["beschreibung"]
    d["projekt_titel"] = c.execute("SELECT titel FROM projekt WHERE id=?", (row["projekt_id"],)).fetchone()[0]
    d["checklisten"] = []
    for k in c.execute("SELECT * FROM checkliste WHERE ticket=? ORDER BY pos, id", (nr,)):
        d["checklisten"].append({"id": k["id"], "titel": k["titel"], "punkte": [
            {"id": u["id"], "text": u["text"], "erledigt": bool(u["erledigt"])}
            for u in c.execute("SELECT * FROM unteraufgabe WHERE checkliste=? ORDER BY pos, id", (k["id"],))]})
    reakt = {}
    for r in c.execute("SELECT r.* FROM reaktion r JOIN kommentar k ON k.id=r.kommentar WHERE k.ticket=? "
                       "ORDER BY r.zeit, r.rowid", (nr,)):
        reakt.setdefault(r["kommentar"], {}).setdefault(r["emoji"], []).append(r["nutzer"])
    d["kommentare_liste"] = []
    for k in c.execute("SELECT * FROM kommentar WHERE ticket=? ORDER BY zeit, id", (nr,)):
        if k["geloescht"]:
            d["kommentare_liste"].append({"id": k["id"], "wer": k["wer"], "zeit": k["zeit"], "geloescht": k["geloescht"]})
            continue
        rk = reakt.get(k["id"], {})
        d["kommentare_liste"].append({
            "id": k["id"], "wer": k["wer"], "zeit": k["zeit"], "text": k["text"], "bearbeitet": k["bearbeitet"],
            "reaktionen": [{"emoji": e, "nutzer": rk[e], "ich": nutzer["kurz"] in rk[e]} for e in REAKTIONEN if e in rk]})
    admin = nutzer["rolle"] == "admin"
    d["ereignisse"] = []
    for v in c.execute("SELECT * FROM verlauf WHERE ticket=? ORDER BY zeit, id", (nr,)):
        if v["feld"].startswith("kommentar_") and not admin:
            continue
        alt, neu = v["alt"], v["neu"]
        if v["feld"] == "beschreibung":
            alt = neu = None
        d["ereignisse"].append({"id": v["id"], "feld": v["feld"], "alt": alt, "neu": neu, "wer": v["wer"], "zeit": v["zeit"]})
    d["verknuepfungen"] = [dict(v) for v in c.execute(
        "SELECT id,art,ref,url,titel,autor,ziel_ticket,quelle,wer,zeit FROM verknuepfung WHERE ticket=? ORDER BY id", (nr,))]
    return d


@route("GET", rf"/api/tickets/{PREFIX}-(\d+)")
def r_ticket(ctx, nr):
    return ticket_detail(ctx.c, ctx.nutzer, int(nr))


# ---------------------------------------------------------------- suche

def such_tokens(q):
    """'von:AB status:arbeit "zwei Worte" rest' -> (operatoren, begriffe)"""
    ops, rest = [], []
    for m in re.finditer(r'(\w+):("[^"]*"|\S*)|"([^"]+)"|(\S+)', q or ""):
        if m.group(1):
            ops.append((m.group(1).lower(), m.group(2).strip('"')))
        else:
            rest.append(m.group(3) or m.group(4))
    return ops, rest


def like_muster(s):
    return "%" + s.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def kurz_suche_schreibweise(s):
    return s.strip().upper()


def suche(c, nutzer, q="", projekt=None, archiv="0", limit=200):
    sicht, p = sichtbar_sql(nutzer)
    p = dict(p)
    p["g"] = vor_tagen(ARCHIV_TAGE)
    conds = ["t.geloescht IS NULL", sicht]
    ops, begriffe = such_tokens(q)
    nach_ungelesen = False
    for i, (op, wert) in enumerate(ops):
        k = f"o{i}"
        if op in ("von", "from"):
            conds.append(f"(t.ersteller=:{k} OR EXISTS(SELECT 1 FROM kommentar k WHERE k.ticket=t.nr "
                         f"AND k.wer=:{k} AND k.geloescht IS NULL))")
            p[k] = kurz_suche_schreibweise(wert)
        elif op in ("in", "projekt"):
            conds.append(f"pr.key=:{k}")
            p[k] = wert.upper()
        elif op == "status":
            if wert.lower() == "offen":
                conds.append("t.status IN ('neu','bereit','arbeit','review')")
            elif wert.lower() in STATUS:
                conds.append(f"t.status=:{k}")
                p[k] = wert.lower()
            else:
                conds.append("0")
        elif op == "kunde":
            conds.append(f"cf(t.kunde)=:{k}")
            p[k] = wert.casefold()
        elif op in ("wer", "an"):
            if wert.lower() in ("niemand", "keiner"):
                conds.append("t.wer IS NULL")
            else:
                conds.append(f"t.wer=:{k}")
                p[k] = kurz_suche_schreibweise(wert)
        elif op == "prio":
            conds.append(f"t.prio=:{k}")
            p[k] = wert.lower()
        elif op == "typ":
            conds.append(f"t.typ=:{k}")
            p[k] = wert.lower()
        elif op == "label":
            conds.append(f"EXISTS(SELECT 1 FROM ticket_label tl JOIN label l ON l.id=tl.label_id "
                         f"WHERE tl.ticket=t.nr AND cf(l.name)=:{k})")
            p[k] = wert.casefold()
        elif op == "ist":
            w = wert.lower()
            if w == "blockiert":
                conds.append("t.blockiert_grund IS NOT NULL")
            elif w in ("ueberfaellig", "überfällig"):
                conds.append("t.faellig < :heute AND t.status IN ('neu','bereit','arbeit','review')")
                p["heute"] = heute().isoformat()
            elif w == "vertraulich":
                conds.append("t.vertraulich=1")
            elif w in ("markiert", "gespeichert"):
                conds.append("EXISTS(SELECT 1 FROM lesezeichen z WHERE z.ticket=t.nr AND z.nutzer=:zich)")
                p["zich"] = nutzer["kurz"]
            elif w == "ungelesen":
                nach_ungelesen = True
            else:
                conds.append("0")
        else:
            begriffe.append(f"{op}:{wert}")   # unbekannter Operator: als Text behandeln
    for i, b in enumerate(begriffe):
        k = f"b{i}"
        p[k] = like_muster(b)
        m = re.fullmatch(r"(?i)(?:TKT-)?(\d{1,15})", b.strip())
        nr_teil = f" OR t.nr={int(m.group(1))}" if m else ""
        conds.append(f"(cf(t.titel) LIKE :{k} ESCAPE '\\' OR cf(t.beschreibung) LIKE :{k} ESCAPE '\\' "
                     f"OR cf(t.kunde) LIKE :{k} ESCAPE '\\' "
                     f"OR EXISTS(SELECT 1 FROM kommentar k WHERE k.ticket=t.nr AND k.geloescht IS NULL "
                     f"AND cf(k.text) LIKE :{k} ESCAPE '\\') "
                     f"OR EXISTS(SELECT 1 FROM ticket_label tl JOIN label l ON l.id=tl.label_id "
                     f"WHERE tl.ticket=t.nr AND cf(l.name) LIKE :{k} ESCAPE '\\'){nr_teil})")
    if projekt:
        conds.append("t.projekt_id=:projekt")
        p["projekt"] = projekt
    if archiv == "0":
        conds.append(f"NOT {archiv_bedingung()}")
    elif archiv == "nur":
        conds.append(archiv_bedingung())
    sql = f"{TICKET_SELECT} WHERE {' AND '.join(conds)} ORDER BY t.geaendert DESC, t.nr DESC"
    rows = c.execute(sql, p).fetchall()
    ag = aggregate(c, nutzer)
    karten = [karte(r, ag) for r in rows]
    if nach_ungelesen:
        karten = [k for k in karten if k["ungelesen"]]
    return {"tickets": karten[:limit], "gesamt": len(karten)}


@route("GET", "/api/suche")
def r_suche(ctx):
    qs = ctx.query
    try:
        limit = max(1, min(500, int(qs.get("limit", "200"))))
        projekt = int(qs["projekt"]) if qs.get("projekt") else None
    except ValueError:
        raise Fehler(400, "ungültiger Parameter")
    archiv = qs.get("archiv", "0")
    if archiv not in ("0", "1", "nur"):
        raise Fehler(400, "archiv: 0, 1 oder nur")
    return suche(ctx.c, ctx.nutzer, qs.get("q", ""), projekt, archiv, limit)


@route("GET", "/api/papierkorb", auth="admin")
def r_papierkorb(ctx):
    rows = ctx.c.execute(f"{TICKET_SELECT} WHERE t.geloescht IS NOT NULL ORDER BY t.geloescht DESC").fetchall()
    ag = aggregate(ctx.c, ctx.nutzer)
    return {"tickets": [karte(r, ag) for r in rows]}


# ---------------------------------------------------------------- tickets: schreiben

def ereignis(c, nr, feld, alt, neu, wer, t):
    c.execute("INSERT INTO verlauf(ticket,feld,alt,neu,wer,zeit) VALUES(?,?,?,?,?,?)",
              (nr, feld, None if alt is None else str(alt), None if neu is None else str(neu), wer, t))


def beruehren(c, nr, t):
    c.execute("UPDATE ticket SET geaendert=? WHERE nr=?", (t, nr))


def gelesen_setzen(c, kurz, nr, t):
    kid = c.execute("SELECT COALESCE(MAX(id),0) FROM kommentar WHERE ticket=?", (nr,)).fetchone()[0]
    vid = c.execute("SELECT COALESCE(MAX(id),0) FROM verlauf WHERE ticket=?", (nr,)).fetchone()[0]
    c.execute("INSERT OR REPLACE INTO lesestand(nutzer,ticket,zeit,kid,vid) VALUES(?,?,?,?,?)", (kurz, nr, t, kid, vid))


def name_von(c, kurz):
    r = c.execute("SELECT name FROM nutzer WHERE kurz=?", (kurz,)).fetchone()
    return r[0] if r else (kurz or "System")


def nutzer_pruefen(c, kurz, pflicht=False):
    if kurz in (None, ""):
        if pflicht:
            raise Fehler(400, "Person fehlt")
        return None
    if not isinstance(kurz, str):
        raise Fehler(400, "unbekannte Person")
    kurz = kurz.strip().upper()
    if not c.execute("SELECT 1 FROM nutzer WHERE kurz=? AND aktiv=1", (kurz,)).fetchone():
        raise Fehler(400, "unbekannte Person")
    return kurz


def kunde_normalisieren(c, v, nutzer=None):
    if v is None or v == "":
        return None
    if not isinstance(v, str) or len(v.strip()) > 40:
        raise Fehler(400, "Kunde: höchstens 40 Zeichen")
    v = v.strip()
    for k in kunden_liste(c, nutzer):
        if k.casefold() == v.casefold():
            return k
    return v


def kunde_aus_titel(c, titel, nutzer=None):
    for k in sorted(kunden_liste(c, nutzer), key=len, reverse=True):
        if titel.casefold().startswith(k.casefold()) and (len(titel) == len(k) or titel[len(k)] in " :,-/–"):
            return k
    return None


def projekt_id_pruefen(c, v):
    if isinstance(v, bool) or not isinstance(v, int):
        raise Fehler(400, "unbekanntes Projekt")
    if not c.execute("SELECT 1 FROM projekt WHERE id=? AND geloescht IS NULL", (v,)).fetchone():
        raise Fehler(400, "unbekanntes Projekt")
    return v


def felder_pruefen(c, d, nutzer=None):
    """Optionale Felder -> Spalten. Gilt fuer Anlegen und Aendern."""
    f = {}
    if "titel" in d:
        f["titel"] = text_feld(d, "titel", 200, pflicht=True, label="Titel")
    if "beschreibung" in d:
        f["beschreibung"] = text_feld(d, "beschreibung", 5000, label="Beschreibung")
    if "prio" in d:
        if d["prio"] not in PRIOS:
            raise Fehler(400, "unbekannte Priorität")
        f["prio"] = d["prio"]
    if "typ" in d:
        if d["typ"] not in TYPEN:
            raise Fehler(400, "unbekannter Typ")
        f["typ"] = d["typ"]
    if "projekt" in d:
        f["projekt_id"] = projekt_id_pruefen(c, d["projekt"])
    if "faellig" in d:
        f["faellig"] = datum_pruefen(d["faellig"], "Fällig")
    if "kunde" in d:
        f["kunde"] = kunde_normalisieren(c, d["kunde"], nutzer)
    if "wert_klasse" in d:
        if d["wert_klasse"] not in (None, "") + WERT_KLASSEN:
            raise Fehler(400, "unbekannte Wertklasse")
        f["wert_klasse"] = d["wert_klasse"] or None
    if "wert_eur" in d:
        v = d["wert_eur"]
        if v in (None, ""):
            f["wert_eur"] = None
        elif isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 1_000_000_000:
            raise Fehler(400, "Betrag: ganze Euro, 0 bis 1.000.000.000")
        else:
            f["wert_eur"] = v
    if "vertraulich" in d:
        f["vertraulich"] = 1 if d["vertraulich"] else 0
    if "wer" in d:
        f["wer"] = nutzer_pruefen(c, d["wer"])
    return f


def ende_der_spalte(c, status):
    return (c.execute("SELECT MAX(pos) FROM ticket WHERE status=? AND geloescht IS NULL", (status,)).fetchone()[0]
            or 0) + 1


def spalte_normalisieren(c, status):
    for i, r in enumerate(c.execute("SELECT nr FROM ticket WHERE status=? AND geloescht IS NULL ORDER BY pos, nr",
                                    (status,)).fetchall(), 1):
        c.execute("UPDATE ticket SET pos=? WHERE nr=?", (float(i), r["nr"]))


def pos_berechnen(c, status, vor, nach, nr):
    """Position zwischen zwei Nachbarn; renumeriert die Spalte, wenn der Abstand zu klein wird."""
    def pos_von(x):
        if x is None or x == nr:
            return None
        if isinstance(x, bool) or not isinstance(x, int):
            raise Fehler(400, "ungültiger Nachbar")
        # nur Nachbarn aus derselben Liste: wer inzwischen woanders liegt, taugt nicht als Ankerpunkt
        r = c.execute("SELECT pos FROM ticket WHERE nr=? AND geloescht IS NULL AND status=?", (x, status)).fetchone()
        return r[0] if r else None
    hi, lo = pos_von(vor), pos_von(nach)
    if lo is None and hi is None:
        return ende_der_spalte(c, status)
    if lo is None:
        return hi - 1
    if hi is None:
        return lo + 1
    if hi - lo < 1e-6:
        spalte_normalisieren(c, status)
        hi, lo = pos_von(vor), pos_von(nach)
    if hi <= lo:
        return lo + 0.5
    return (lo + hi) / 2


def wip_zaehlen(c, status, kurz, ohne_nr):
    if kurz:
        return c.execute("SELECT COUNT(*) FROM ticket WHERE status=? AND wer=? AND geloescht IS NULL AND nr!=?",
                         (status, kurz, ohne_nr)).fetchone()[0]
    return c.execute("SELECT COUNT(*) FROM ticket WHERE status=? AND geloescht IS NULL AND nr!=?",
                     (status, ohne_nr)).fetchone()[0]


MENTION = re.compile(r"(?<![\w.@])@([A-Za-zÄÖÜäöü]{2,3})(?![\w])")


def erwaehnte(c, text):
    out = set()
    for m in MENTION.finditer(text):
        kurz = m.group(1).upper()
        if c.execute("SELECT 1 FROM nutzer WHERE kurz=? AND aktiv=1", (kurz,)).fetchone():
            out.add(kurz)
    return out


def darf_sehen(c, row, kurz):
    if not row["vertraulich"]:
        return True
    u = c.execute("SELECT rolle FROM nutzer WHERE kurz=?", (kurz,)).fetchone()
    if u and u["rolle"] == "admin":
        return True
    if kurz in (row["ersteller"], row["wer"]):
        return True
    return bool(c.execute("SELECT 1 FROM beobachter WHERE ticket=? AND nutzer=?", (row["nr"], kurz)).fetchone())


def melden(c, row, empfaenger, art, text, von, t):
    """Benachrichtigung in die App-Glocke, nie an sich selbst, nie an Leute ohne Sicht aufs Ticket."""
    for kurz in sorted(set(e for e in empfaenger if e)):
        if kurz == von:
            continue
        u = c.execute("SELECT aktiv FROM nutzer WHERE kurz=?", (kurz,)).fetchone()
        if not u or not u["aktiv"] or not darf_sehen(c, row, kurz):
            continue
        c.execute("INSERT INTO benachrichtigung(nutzer,ticket,art,text,zeit) VALUES(?,?,?,?,?)",
                  (kurz, row["nr"], art, text, t))


def beteiligte(c, nr, row):
    return {row["ersteller"], row["wer"], *[r[0] for r in c.execute("SELECT nutzer FROM beobachter WHERE ticket=?", (nr,))]}


def aktuelle_zeile(c, nr):
    return c.execute("SELECT * FROM ticket WHERE nr=?", (nr,)).fetchone()


@route("POST", "/api/tickets")
def r_ticket_neu(ctx):
    c, d, ich = ctx.c, ctx.body(), ctx.ich
    if d.get("status", "neu") != "neu":
        raise Fehler(400, "Neue Tickets starten in 'neu'")
    f = felder_pruefen(c, d, ctx.nutzer)
    if "titel" not in f:
        raise Fehler(400, "Titel fehlt")
    if "projekt_id" not in f:
        f["projekt_id"] = c.execute("SELECT id FROM projekt WHERE key='ALLG'").fetchone()[0]
    if "kunde" not in d:
        f["kunde"] = kunde_aus_titel(c, f["titel"], ctx.nutzer)
    f.setdefault("beschreibung", "")
    f.setdefault("prio", "mittel")
    f.setdefault("typ", "aufgabe")
    t = jetzt()
    nr = (c.execute("SELECT MAX(nr) FROM ticket").fetchone()[0] or 0) + 1
    f.update(nr=nr, status="neu", ersteller=ich, pos=ende_der_spalte(c, "neu"), angelegt=t, geaendert=t)
    f.setdefault("wer", None)
    spalten = list(f)
    c.execute(f"INSERT INTO ticket({','.join(spalten)}) VALUES({','.join('?' * len(spalten))})",
              [f[s] for s in spalten])
    ereignis(c, nr, "angelegt", None, "neu", ich, t)
    if f["wer"]:
        ereignis(c, nr, "wer", None, f["wer"], ich, t)
    gelesen_setzen(c, ich, nr, t)
    row = aktuelle_zeile(c, nr)
    if f["wer"]:
        melden(c, row, [f["wer"]], "zugewiesen", f"{name_von(c, ich)} hat dir {ticket_id(nr)} zugewiesen", ich, t)
    return 201, ticket_detail(c, ctx.nutzer, nr)


def wert_text(klasse, eur):
    teile = []
    if klasse:
        teile.append("groß" if klasse == "gross" else klasse)
    if eur is not None:
        teile.append(f"{eur} €")
    return " / ".join(teile) or None


def ticket_aendern(ctx, nr, d):
    c, ich = ctx.c, ctx.ich
    alt = ticket_row(c, nr, ctx.nutzer)
    t = jetzt()
    f = felder_pruefen(c, d, ctx.nutzer)
    neu_status = d.get("status", alt["status"])
    if neu_status not in STATUS:
        raise Fehler(400, "unbekannter Status")
    wer_neu = f["wer"] if "wer" in f else alt["wer"]
    begr = text_feld(d, "begruendung", 1000)
    kommentar_text = None
    wechsel = neu_status != alt["status"]

    if wechsel:
        if neu_status == "verworfen":
            if not begr:
                raise Fehler(409, "Warum wird das verworfen? Bitte kurz begründen.", braucht="begruendung", grund="verworfen")
            kommentar_text = "Verworfen: " + begr
        if alt["status"] in ("fertig", "verworfen") and neu_status not in ("fertig", "verworfen"):
            if not begr:
                raise Fehler(409, "Warum wird das wieder geöffnet? Bitte kurz begründen.", braucht="begruendung",
                             grund="wiedereroeffnet")
            kommentar_text = "Wieder geöffnet: " + begr
        if neu_status in ("bereit", "arbeit", "review", "fertig") and not wer_neu:
            if neu_status == "bereit":
                raise Fehler(400, "Bereit braucht eine zuständige Person")
            wer_neu = f["wer"] = ich
    if neu_status == "arbeit" and wer_neu and (wechsel or wer_neu != alt["wer"]):
        limit, n = einstellung_int(c, "wip_arbeit"), wip_zaehlen(c, "arbeit", wer_neu, nr)
        if n >= limit and kommentar_text is None:
            if not begr:
                raise Fehler(409, f"{name_von(c, wer_neu)} hat schon {n} Tickets in Arbeit (Limit {limit}).",
                             braucht="begruendung", grund="wip", wip={"anzahl": n, "limit": limit})
            kommentar_text = "Warum trotzdem (WIP-Limit): " + begr
    if neu_status == "review" and wechsel:
        limit, n = einstellung_int(c, "wip_review"), wip_zaehlen(c, "review", None, nr)
        if n >= limit and kommentar_text is None:
            if not begr:
                raise Fehler(409, f"Im Review liegen schon {n} Tickets (Limit {limit}).",
                             braucht="begruendung", grund="review", wip={"anzahl": n, "limit": limit})
            kommentar_text = "Warum trotzdem (Review-Limit): " + begr

    if wechsel:
        f["status"] = neu_status
        if neu_status == "arbeit" and not alt["begonnen"]:
            f["begonnen"] = t
        if neu_status == "fertig":
            f["fertig_am"] = t
        elif alt["status"] == "fertig":
            f["fertig_am"] = None
        if neu_status in ("fertig", "verworfen"):
            f["blockiert_grund"] = f["blockiert_seit"] = None
    if "blockiert_grund" in d and neu_status not in ("fertig", "verworfen"):
        g = text_feld(d, "blockiert_grund", 200, label="Blockade-Grund")   # nur Leerzeichen = kein Grund = entblockt
        if not g:
            f["blockiert_grund"] = f["blockiert_seit"] = None
        else:
            f["blockiert_grund"] = g
            if not alt["blockiert_grund"]:
                f["blockiert_seit"] = t
    elif "blockiert_grund" in d and d["blockiert_grund"]:
        raise Fehler(400, "Fertige oder verworfene Tickets sind nicht blockiert")

    if "pos" in d:
        p = d["pos"]
        if isinstance(p, bool) or not isinstance(p, (int, float)) or p != p or abs(p) > 1e12:
            raise Fehler(400, "ungültige Position")
        f["pos"] = float(p)
    elif "vor" in d or "nach" in d:
        f["pos"] = pos_berechnen(c, neu_status, d.get("vor"), d.get("nach"), nr)
    elif wechsel:
        f["pos"] = ende_der_spalte(c, neu_status)

    beob_neu = labels_neu = None
    if "beobachter" in d:
        if not isinstance(d["beobachter"], list):
            raise Fehler(400, "beobachter: Liste")
        beob_neu = sorted({nutzer_pruefen(c, k, True) for k in d["beobachter"]})
    if "labels" in d:
        if not isinstance(d["labels"], list):
            raise Fehler(400, "labels: Liste")
        labels_neu = sorted({int(x) for x in d["labels"] if isinstance(x, int) and not isinstance(x, bool)})
        for lid in labels_neu:
            if not c.execute("SELECT 1 FROM label WHERE id=?", (lid,)).fetchone():
                raise Fehler(400, "unbekanntes Label")

    # --- anwenden
    diffs = {k: v for k, v in f.items() if alt[k] != v}
    inhaltlich = [k for k in diffs if k != "pos"]
    if diffs:
        sets = dict(diffs)
        if inhaltlich:
            sets["geaendert"] = t
        c.execute("UPDATE ticket SET " + ",".join(f"{k}=?" for k in sets) + " WHERE nr=?", (*sets.values(), nr))
    for k in inhaltlich:
        if k in VERLAUF_FELDER:
            ereignis(c, nr, k, alt[k], diffs[k], ich, t)
    if "projekt_id" in diffs:
        a = c.execute("SELECT key FROM projekt WHERE id=?", (alt["projekt_id"],)).fetchone()[0]
        n = c.execute("SELECT key FROM projekt WHERE id=?", (diffs["projekt_id"],)).fetchone()[0]
        ereignis(c, nr, "projekt", a, n, ich, t)
    if "wert_klasse" in diffs or "wert_eur" in diffs:
        ereignis(c, nr, "wert", wert_text(alt["wert_klasse"], alt["wert_eur"]),
                 wert_text(f.get("wert_klasse", alt["wert_klasse"]), f.get("wert_eur", alt["wert_eur"])), ich, t)
    if "blockiert_grund" in diffs:
        ereignis(c, nr, "blockiert", alt["blockiert_grund"], diffs["blockiert_grund"], ich, t)
    geaendert_extra = False
    if beob_neu is not None:
        vorher = sorted(r[0] for r in c.execute("SELECT nutzer FROM beobachter WHERE ticket=?", (nr,)))
        if vorher != beob_neu:
            c.execute("DELETE FROM beobachter WHERE ticket=?", (nr,))
            c.executemany("INSERT INTO beobachter(ticket,nutzer) VALUES(?,?)", [(nr, k) for k in beob_neu])
            ereignis(c, nr, "beobachter", ",".join(vorher), ",".join(beob_neu), ich, t)
            geaendert_extra = True
    if labels_neu is not None:
        vorher = sorted(r[0] for r in c.execute("SELECT label_id FROM ticket_label WHERE ticket=?", (nr,)))
        if vorher != labels_neu:
            namen = lambda ids: ",".join(sorted(r[0] for i in ids for r in c.execute("SELECT name FROM label WHERE id=?", (i,))))
            ereignis(c, nr, "labels", namen(vorher), namen(labels_neu), ich, t)
            c.execute("DELETE FROM ticket_label WHERE ticket=?", (nr,))
            c.executemany("INSERT INTO ticket_label(ticket,label_id) VALUES(?,?)", [(nr, i) for i in labels_neu])
            geaendert_extra = True
    if geaendert_extra and not inhaltlich:
        beruehren(c, nr, t)
    if kommentar_text:
        c.execute("INSERT INTO kommentar(ticket,wer,text,zeit) VALUES(?,?,?,?)", (nr, ich, kommentar_text, t))
    if inhaltlich or geaendert_extra or kommentar_text:
        gelesen_setzen(c, ich, nr, t)

    # --- benachrichtigen
    row = aktuelle_zeile(c, nr)
    wer_ich = name_von(c, ich)
    if "wer" in diffs and diffs["wer"]:
        melden(c, row, [diffs["wer"]], "zugewiesen", f"{wer_ich} hat dir {ticket_id(nr)} zugewiesen", ich, t)
    if "status" in diffs:
        empf = set()
        if neu_status in ("review", "fertig", "verworfen"):
            empf |= {row["ersteller"], *[r[0] for r in c.execute("SELECT nutzer FROM beobachter WHERE ticket=?", (nr,))]}
        if row["wer"] != ich:
            empf.add(row["wer"])
        melden(c, row, empf, "status", f"{wer_ich}: {ticket_id(nr)} ist jetzt {neu_status}", ich, t)
    try:
        return ticket_detail(c, ctx.nutzer, nr)
    except Fehler:
        # Wer sich mit der Änderung selbst die Sicht nimmt (vertraulich, Zuständigkeit abgeben), bekommt keine halbe
        # Antwort: die Transaktion wird zurückgerollt und der Grund genannt.
        raise Fehler(409, "Danach könntest du dieses vertrauliche Ticket nicht mehr sehen. "
                          "Nimm dich zuerst als Beobachter auf.")


@route("PATCH", rf"/api/tickets/{PREFIX}-(\d+)")
def r_ticket_aendern(ctx, nr):
    return ticket_aendern(ctx, int(nr), ctx.body())


@route("DELETE", rf"/api/tickets/{PREFIX}-(\d+)", auth="admin")
def r_ticket_loeschen(ctx, nr):
    nr, t = int(nr), jetzt()
    ticket_row(ctx.c, nr, ctx.nutzer)
    ctx.c.execute("UPDATE ticket SET geloescht=? WHERE nr=?", (t, nr))
    ereignis(ctx.c, nr, "geloescht", None, t, ctx.ich, t)
    return {"ok": True}


@route("POST", rf"/api/tickets/{PREFIX}-(\d+)/wiederherstellen", auth="admin")
def r_ticket_zurueck(ctx, nr):
    nr, t = int(nr), jetzt()
    row = ticket_row(ctx.c, nr, ctx.nutzer, geloeschte=True)
    if not row["geloescht"]:
        raise Fehler(409, "Das Ticket ist nicht gelöscht")
    ctx.c.execute("UPDATE ticket SET geloescht=NULL, geaendert=? WHERE nr=?", (t, nr))
    ereignis(ctx.c, nr, "wiederhergestellt", row["geloescht"], None, ctx.ich, t)
    pr = ctx.c.execute("SELECT key, geloescht FROM projekt WHERE id=?", (row["projekt_id"],)).fetchone()
    if pr["geloescht"]:   # das Projekt wurde inzwischen gelöscht: das Ticket würde in keiner Liste auftauchen
        allg = ctx.c.execute("SELECT id FROM projekt WHERE key='ALLG'").fetchone()[0]
        ctx.c.execute("UPDATE ticket SET projekt_id=? WHERE nr=?", (allg, nr))
        ereignis(ctx.c, nr, "projekt", pr["key"], "ALLG", ctx.ich, t)
    return ticket_detail(ctx.c, ctx.nutzer, nr)


@route("POST", rf"/api/tickets/{PREFIX}-(\d+)/gelesen")
def r_gelesen(ctx, nr):
    nr, t = int(nr), jetzt()
    ticket_row(ctx.c, nr, ctx.nutzer)
    gelesen_setzen(ctx.c, ctx.ich, nr, t)
    ctx.c.execute("UPDATE benachrichtigung SET gelesen=? WHERE nutzer=? AND ticket=? AND gelesen IS NULL",
                  (t, ctx.ich, nr))
    return {"ok": True}


@route("POST", rf"/api/tickets/{PREFIX}-(\d+)/merken")
def r_merken(ctx, nr):
    nr = int(nr)
    ticket_row(ctx.c, nr, ctx.nutzer)
    ctx.c.execute("INSERT OR IGNORE INTO lesezeichen(nutzer,ticket,zeit) VALUES(?,?,?)", (ctx.ich, nr, jetzt()))
    return {"markiert": True}


@route("DELETE", rf"/api/tickets/{PREFIX}-(\d+)/merken")
def r_merken_weg(ctx, nr):
    ctx.c.execute("DELETE FROM lesezeichen WHERE nutzer=? AND ticket=?", (ctx.ich, int(nr)))
    return {"markiert": False}


# ---- Kommentare, Reaktionen

def kommentar_holen(c, nutzer, kid):
    k = c.execute("SELECT * FROM kommentar WHERE id=?", (kid,)).fetchone()
    if not k:
        raise Fehler(404, "Kommentar nicht gefunden")
    row = ticket_row(c, k["ticket"], nutzer)
    return k, row


@route("POST", rf"/api/tickets/{PREFIX}-(\d+)/kommentare")
def r_kommentar_neu(ctx, nr):
    c, ich, nr = ctx.c, ctx.ich, int(nr)
    row = ticket_row(c, nr, ctx.nutzer)
    text = text_feld(ctx.body(), "text", 4000, pflicht=True, label="Kommentar")
    t = jetzt()
    c.execute("INSERT INTO kommentar(ticket,wer,text,zeit) VALUES(?,?,?,?)", (nr, ich, text, t))
    beruehren(c, nr, t)
    gelesen_setzen(c, ich, nr, t)
    erw = erwaehnte(c, text)
    wer_ich = name_von(c, ich)
    melden(c, row, erw, "erwaehnt", f"{wer_ich} hat dich in {ticket_id(nr)} erwähnt", ich, t)
    melden(c, row, beteiligte(c, nr, row) - erw, "kommentar", f"{wer_ich} hat {ticket_id(nr)} kommentiert", ich, t)
    return 201, ticket_detail(c, ctx.nutzer, nr)


@route("PATCH", r"/api/kommentare/(\d+)")
def r_kommentar_aendern(ctx, kid):
    c, ich = ctx.c, ctx.ich
    k, row = kommentar_holen(c, ctx.nutzer, int(kid))
    if k["geloescht"]:
        raise Fehler(404, "Kommentar nicht gefunden")
    if k["wer"] != ich:
        raise Fehler(403, "Nur eigene Kommentare lassen sich bearbeiten")
    text = text_feld(ctx.body(), "text", 4000, pflicht=True, label="Kommentar")
    if text != k["text"]:
        t = jetzt()
        c.execute("UPDATE kommentar SET text=?, bearbeitet=? WHERE id=?", (text, t, k["id"]))
        ereignis(c, k["ticket"], "kommentar_bearbeitet", k["text"], text, ich, t)
        beruehren(c, k["ticket"], t)
    return ticket_detail(c, ctx.nutzer, k["ticket"])


@route("DELETE", r"/api/kommentare/(\d+)")
def r_kommentar_weg(ctx, kid):
    c, ich = ctx.c, ctx.ich
    k, row = kommentar_holen(c, ctx.nutzer, int(kid))
    if k["geloescht"]:
        raise Fehler(404, "Kommentar nicht gefunden")
    if k["wer"] != ich and not ctx.admin:
        raise Fehler(403, "Nur eigene Kommentare lassen sich löschen")
    t = jetzt()
    c.execute("UPDATE kommentar SET geloescht=? WHERE id=?", (t, k["id"]))
    ereignis(c, k["ticket"], "kommentar_geloescht", k["text"], None, ich, t)
    beruehren(c, k["ticket"], t)
    return ticket_detail(c, ctx.nutzer, k["ticket"])


@route("POST", r"/api/kommentare/(\d+)/reaktion")
def r_reaktion(ctx, kid):
    c, ich = ctx.c, ctx.ich
    k, row = kommentar_holen(c, ctx.nutzer, int(kid))
    if k["geloescht"]:
        raise Fehler(404, "Kommentar nicht gefunden")
    emoji = ctx.body().get("emoji")
    if emoji not in REAKTIONEN:
        raise Fehler(400, "Emoji nicht in der Auswahl")
    if c.execute("SELECT 1 FROM reaktion WHERE kommentar=? AND nutzer=? AND emoji=?", (k["id"], ich, emoji)).fetchone():
        c.execute("DELETE FROM reaktion WHERE kommentar=? AND nutzer=? AND emoji=?", (k["id"], ich, emoji))
    else:
        c.execute("INSERT INTO reaktion(kommentar,nutzer,emoji,zeit) VALUES(?,?,?,?)", (k["id"], ich, emoji, jetzt()))
    return ticket_detail(c, ctx.nutzer, k["ticket"])


# ---- Checklisten

@route("POST", rf"/api/tickets/{PREFIX}-(\d+)/checklisten")
def r_checkliste_neu(ctx, nr):
    c, nr = ctx.c, int(nr)
    ticket_row(c, nr, ctx.nutzer)
    titel = text_feld(ctx.body(), "titel", 100, label="Titel") or "Checkliste"
    if c.execute("SELECT COUNT(*) FROM checkliste WHERE ticket=?", (nr,)).fetchone()[0] >= 20:
        raise Fehler(409, "Mehr als 20 Checklisten pro Ticket sind zu viel")
    pos = c.execute("SELECT COALESCE(MAX(pos),-1)+1 FROM checkliste WHERE ticket=?", (nr,)).fetchone()[0]
    c.execute("INSERT INTO checkliste(ticket,titel,pos) VALUES(?,?,?)", (nr, titel, pos))
    beruehren(c, nr, jetzt())
    return 201, ticket_detail(c, ctx.nutzer, nr)


def checkliste_holen(c, nutzer, kid):
    k = c.execute("SELECT * FROM checkliste WHERE id=?", (kid,)).fetchone()
    if not k:
        raise Fehler(404, "Checkliste nicht gefunden")
    ticket_row(c, k["ticket"], nutzer)
    return k


@route("PATCH", r"/api/checklisten/(\d+)")
def r_checkliste_aendern(ctx, kid):
    k = checkliste_holen(ctx.c, ctx.nutzer, int(kid))
    titel = text_feld(ctx.body(), "titel", 100, pflicht=True, label="Titel")
    ctx.c.execute("UPDATE checkliste SET titel=? WHERE id=?", (titel, k["id"]))
    return ticket_detail(ctx.c, ctx.nutzer, k["ticket"])


@route("DELETE", r"/api/checklisten/(\d+)")
def r_checkliste_weg(ctx, kid):
    k = checkliste_holen(ctx.c, ctx.nutzer, int(kid))
    ctx.c.execute("DELETE FROM unteraufgabe WHERE checkliste=?", (k["id"],))
    ctx.c.execute("DELETE FROM checkliste WHERE id=?", (k["id"],))
    beruehren(ctx.c, k["ticket"], jetzt())
    return ticket_detail(ctx.c, ctx.nutzer, k["ticket"])


@route("POST", r"/api/checklisten/(\d+)/punkte")
def r_punkt_neu(ctx, kid):
    c = ctx.c
    k = checkliste_holen(c, ctx.nutzer, int(kid))
    text = text_feld(ctx.body(), "text", 200, pflicht=True, label="Text")
    pos = c.execute("SELECT COALESCE(MAX(pos),-1)+1 FROM unteraufgabe WHERE checkliste=?", (k["id"],)).fetchone()[0]
    c.execute("INSERT INTO unteraufgabe(ticket,checkliste,text,pos) VALUES(?,?,?,?)", (k["ticket"], k["id"], text, pos))
    beruehren(c, k["ticket"], jetzt())
    return 201, ticket_detail(c, ctx.nutzer, k["ticket"])


def punkt_holen(c, nutzer, pid):
    p = c.execute("SELECT * FROM unteraufgabe WHERE id=?", (pid,)).fetchone()
    if not p:
        raise Fehler(404, "Punkt nicht gefunden")
    ticket_row(c, p["ticket"], nutzer)
    return p


@route("PATCH", r"/api/punkte/(\d+)")
def r_punkt_aendern(ctx, pid):
    c, d = ctx.c, ctx.body()
    p = punkt_holen(c, ctx.nutzer, int(pid))
    if "text" in d:
        c.execute("UPDATE unteraufgabe SET text=? WHERE id=?", (text_feld(d, "text", 200, pflicht=True), p["id"]))
    if "erledigt" in d:
        c.execute("UPDATE unteraufgabe SET erledigt=? WHERE id=?", (1 if d["erledigt"] else 0, p["id"]))
    beruehren(c, p["ticket"], jetzt())
    return ticket_detail(c, ctx.nutzer, p["ticket"])


@route("DELETE", r"/api/punkte/(\d+)")
def r_punkt_weg(ctx, pid):
    p = punkt_holen(ctx.c, ctx.nutzer, int(pid))
    ctx.c.execute("DELETE FROM unteraufgabe WHERE id=?", (p["id"],))
    beruehren(ctx.c, p["ticket"], jetzt())
    return ticket_detail(ctx.c, ctx.nutzer, p["ticket"])


# ---- Verknuepfungen

def verknuepfung_erkennen(eingabe):
    s = eingabe.strip()
    m = re.fullmatch(rf"(?i){PREFIX}-(\d{{1,9}})", s)
    if m:
        return "ticket", f"{PREFIX}-{int(m.group(1))}", None, int(m.group(1))
    if re.fullmatch(r"[0-9a-fA-F]{7,40}", s):
        return "commit", s.lower(), None, None
    if re.fullmatch(r"https?://\S+", s):
        m = re.search(r"/commits?/([0-9a-fA-F]{7,40})(?:[/?#]|$)", s)
        if m:
            return "commit", m.group(1).lower(), s, None
        return "link", s, s, None
    if s.startswith("\\\\") or re.match(r"^[A-Za-z]:\\", s) or (s.startswith("/") and len(s) > 1):
        return "datei", s, None, None
    raise Fehler(400, "Erwartet: Commit-Hash, Adresse (http/https), Dateipfad oder TKT-Nummer")


def verknuepfen(c, nr, art, ref, url, titel, autor, ziel, quelle, wer, t):
    """Legt die Verknuepfung an, wenn es sie noch nicht gibt. Gibt True zurueck, wenn neu."""
    cur = c.execute("INSERT OR IGNORE INTO verknuepfung(ticket,art,ref,url,titel,autor,ziel_ticket,quelle,wer,zeit) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)", (nr, art, ref, url, titel, autor, ziel, quelle, wer, t))
    if cur.rowcount:
        ereignis(c, nr, "verknuepfung", None, f"{art}: {ref}", wer, t)
        beruehren(c, nr, t)
        return True
    return False


@route("POST", rf"/api/tickets/{PREFIX}-(\d+)/verknuepfungen")
def r_verknuepfung_neu(ctx, nr):
    c, d, nr = ctx.c, ctx.body(), int(nr)
    ticket_row(c, nr, ctx.nutzer)
    art, ref, url, ziel = verknuepfung_erkennen(text_feld(d, "eingabe", 500, pflicht=True, label="Eingabe"))
    if art == "ticket":
        if ziel == nr:
            raise Fehler(400, "Ein Ticket lässt sich nicht mit sich selbst verknüpfen")
        ticket_row(c, ziel, ctx.nutzer)
    if not verknuepfen(c, nr, art, ref, url, text_feld(d, "titel", 200) or None, None, ziel, "manuell", ctx.ich, jetzt()):
        raise Fehler(409, "Ist schon verknüpft")
    return 201, ticket_detail(c, ctx.nutzer, nr)


@route("DELETE", r"/api/verknuepfungen/(\d+)")
def r_verknuepfung_weg(ctx, vid):
    c = ctx.c
    v = c.execute("SELECT * FROM verknuepfung WHERE id=?", (int(vid),)).fetchone()
    if not v:
        raise Fehler(404, "Verknüpfung nicht gefunden")
    ticket_row(c, v["ticket"], ctx.nutzer)
    t = jetzt()
    c.execute("DELETE FROM verknuepfung WHERE id=?", (v["id"],))
    ereignis(c, v["ticket"], "verknuepfung", f"{v['art']}: {v['ref']}", None, ctx.ich, t)
    beruehren(c, v["ticket"], t)
    return ticket_detail(c, ctx.nutzer, v["ticket"])


# ---- Labels, Projekte

@route("POST", "/api/labels")
def r_label_neu(ctx):
    c, d = ctx.c, ctx.body()
    name = text_feld(d, "name", 30, pflicht=True, label="Name")
    farbe = d.get("farbe")
    if farbe is None:
        n = c.execute("SELECT COUNT(*) FROM label").fetchone()[0]
        farbe = LABEL_FARBEN[(n + 1) % len(LABEL_FARBEN)]
    if farbe not in LABEL_FARBEN:
        raise Fehler(400, "unbekannte Farbe")
    if c.execute("SELECT 1 FROM label WHERE name=?", (name,)).fetchone():
        raise Fehler(409, "Das Label gibt es schon")
    lid = c.execute("INSERT INTO label(name,farbe) VALUES(?,?)", (name, farbe)).lastrowid
    return 201, {"id": lid, "name": name, "farbe": farbe}


@route("PATCH", r"/api/labels/(\d+)")
def r_label_aendern(ctx, lid):
    c, d = ctx.c, ctx.body()
    lab = c.execute("SELECT * FROM label WHERE id=?", (int(lid),)).fetchone()
    if not lab:
        raise Fehler(404, "Label nicht gefunden")
    name = text_feld(d, "name", 30, pflicht=True, label="Name") if "name" in d else lab["name"]
    farbe = d.get("farbe", lab["farbe"])
    if farbe not in LABEL_FARBEN:
        raise Fehler(400, "unbekannte Farbe")
    if c.execute("SELECT 1 FROM label WHERE name=? AND id!=?", (name, lab["id"])).fetchone():
        raise Fehler(409, "Das Label gibt es schon")
    c.execute("UPDATE label SET name=?, farbe=? WHERE id=?", (name, farbe, lab["id"]))
    return {"id": lab["id"], "name": name, "farbe": farbe}


@route("DELETE", r"/api/labels/(\d+)", auth="admin")
def r_label_weg(ctx, lid):
    ctx.c.execute("DELETE FROM ticket_label WHERE label_id=?", (int(lid),))
    ctx.c.execute("DELETE FROM label WHERE id=?", (int(lid),))
    return {"ok": True}


@route("POST", "/api/projekte")
def r_projekt_neu(ctx):
    c, d = ctx.c, ctx.body()
    titel = text_feld(d, "titel", 60, pflicht=True, label="Name")
    key = text_feld(d, "key", 6, label="Kurzname").upper()
    if not key:
        key = re.sub(r"[^A-Z0-9]", "", titel.upper())[:4]
    if not re.fullmatch(r"[A-Z0-9]{2,6}", key):
        raise Fehler(400, "Kurzname: zwei bis sechs Buchstaben oder Ziffern")
    if c.execute("SELECT 1 FROM projekt WHERE key=?", (key,)).fetchone():
        raise Fehler(409, "Den Kurznamen gibt es schon")
    eigner = nutzer_pruefen(c, d.get("eigner")) or ctx.ich
    pid = c.execute("INSERT INTO projekt(key,titel,ziel,eigner,angelegt) VALUES(?,?,?,?,?)",
                    (key, titel, text_feld(d, "ziel", 300, label="Beschreibung"), eigner, jetzt())).lastrowid
    return 201, dict(c.execute("SELECT * FROM projekt WHERE id=?", (pid,)).fetchone())


@route("PATCH", r"/api/projekte/(\d+)")
def r_projekt_aendern(ctx, pid):
    c, d = ctx.c, ctx.body()
    p = c.execute("SELECT * FROM projekt WHERE id=? AND geloescht IS NULL", (int(pid),)).fetchone()
    if not p:
        raise Fehler(404, "Projekt nicht gefunden")
    titel = text_feld(d, "titel", 60, pflicht=True, label="Name") if "titel" in d else p["titel"]
    ziel = text_feld(d, "ziel", 300, label="Beschreibung") if "ziel" in d else p["ziel"]
    eigner = nutzer_pruefen(c, d["eigner"]) if "eigner" in d else p["eigner"]
    status, geschlossen = p["status"], p["geschlossen"]
    if "status" in d and d["status"] != p["status"]:
        if d["status"] not in PROJEKT_STATUS:
            raise Fehler(400, "unbekannter Status")
        if not ctx.admin:
            raise Fehler(403, "Projekte schließen oder ruhen lassen darf nur ein Admin")
        status = d["status"]
        geschlossen = jetzt() if status == "abgeschlossen" else None
    c.execute("UPDATE projekt SET titel=?, ziel=?, eigner=?, status=?, geschlossen=? WHERE id=?",
              (titel, ziel, eigner, status, geschlossen, p["id"]))
    return dict(c.execute("SELECT * FROM projekt WHERE id=?", (p["id"],)).fetchone())


@route("DELETE", r"/api/projekte/(\d+)", auth="admin")
def r_projekt_weg(ctx, pid):
    c = ctx.c
    p = c.execute("SELECT * FROM projekt WHERE id=? AND geloescht IS NULL", (int(pid),)).fetchone()
    if not p:
        raise Fehler(404, "Projekt nicht gefunden")
    if p["key"] == "ALLG":
        raise Fehler(409, "Allgemein bleibt")
    if c.execute("SELECT 1 FROM ticket WHERE projekt_id=? AND geloescht IS NULL", (p["id"],)).fetchone():
        raise Fehler(409, "Im Projekt liegen noch Tickets")
    c.execute("UPDATE projekt SET geloescht=? WHERE id=?", (jetzt(), p["id"]))
    return {"ok": True}


# ---- Aktivitaet, Benachrichtigungen

@route("GET", "/api/aktivitaet")
def r_aktivitaet(ctx):
    c, qs = ctx.c, ctx.query
    try:
        limit = max(1, min(200, int(qs.get("limit", "50"))))
        projekt = int(qs["projekt"]) if qs.get("projekt") else None
    except ValueError:
        raise Fehler(400, "ungültiger Parameter")
    sicht, p = sichtbar_sql(ctx.nutzer)
    p = dict(p)
    zusatz = ""
    if projekt:
        zusatz = " AND t.projekt_id=:projekt"
        p["projekt"] = projekt
    if qs.get("vor"):
        p["vor"] = qs["vor"]
    zeit_k = " AND k.zeit < :vor" if qs.get("vor") else ""
    zeit_v = " AND v.zeit < :vor" if qs.get("vor") else ""
    eintraege = []
    for k in c.execute(f"SELECT k.id, k.ticket, k.wer, k.zeit, k.text, t.titel FROM kommentar k JOIN ticket t ON t.nr=k.ticket "
                       f"WHERE k.geloescht IS NULL AND t.geloescht IS NULL AND {sicht}{zusatz}{zeit_k} "
                       f"ORDER BY k.zeit DESC, k.id DESC LIMIT {limit}", p):
        eintraege.append({"art": "kommentar", "id": k["id"], "ticket": ticket_id(k["ticket"]), "titel": k["titel"],
                          "wer": k["wer"], "zeit": k["zeit"], "text": k["text"]})
    for v in c.execute(f"SELECT v.id, v.ticket, v.feld, v.alt, v.neu, v.wer, v.zeit, t.titel FROM verlauf v "
                       f"JOIN ticket t ON t.nr=v.ticket WHERE t.geloescht IS NULL AND v.feld NOT LIKE 'kommentar\\_%' ESCAPE '\\' "
                       f"AND {sicht}{zusatz}{zeit_v} ORDER BY v.zeit DESC, v.id DESC LIMIT {limit}", p):
        alt, neu = v["alt"], v["neu"]
        if v["feld"] == "beschreibung":
            alt = neu = None
        eintraege.append({"art": "ereignis", "id": v["id"], "ticket": ticket_id(v["ticket"]), "titel": v["titel"],
                          "feld": v["feld"], "alt": alt, "neu": neu, "wer": v["wer"], "zeit": v["zeit"]})
    eintraege.sort(key=lambda e: (e["zeit"], e["id"]), reverse=True)
    return {"eintraege": eintraege[:limit]}


@route("GET", "/api/benachrichtigungen")
def r_meldungen(ctx):
    c, qs = ctx.c, ctx.query
    try:
        limit = max(1, min(200, int(qs.get("limit", "50"))))
    except ValueError:
        raise Fehler(400, "ungültiger Parameter")
    sicht, p = sichtbar_sql(ctx.nutzer)
    zusatz = ""
    if qs.get("art") == "erwaehnt":
        zusatz = " AND b.art='erwaehnt'"
    if qs.get("nur") == "ungelesen":
        zusatz += " AND b.gelesen IS NULL"
    rows = c.execute(f"SELECT b.*, t.titel, pr.key AS projekt_key FROM benachrichtigung b JOIN ticket t ON t.nr=b.ticket "
                     f"JOIN projekt pr ON pr.id=t.projekt_id WHERE b.nutzer=:nutzer AND t.geloescht IS NULL AND {sicht}{zusatz} "
                     f"ORDER BY b.id DESC LIMIT {limit}", {**p, "nutzer": ctx.ich}).fetchall()
    return {"meldungen": [{"id": r["id"], "ticket": ticket_id(r["ticket"]), "titel": r["titel"], "art": r["art"],
                           "text": r["text"], "zeit": r["zeit"], "gelesen": r["gelesen"],
                           "projekt_key": r["projekt_key"]} for r in rows]}


@route("POST", "/api/benachrichtigungen/lesen")
def r_meldungen_lesen(ctx):
    c, d, t = ctx.c, ctx.body(), jetzt()
    if d.get("alle"):
        c.execute("UPDATE benachrichtigung SET gelesen=? WHERE nutzer=? AND gelesen IS NULL", (t, ctx.ich))
    elif isinstance(d.get("id"), int):
        c.execute("UPDATE benachrichtigung SET gelesen=? WHERE nutzer=? AND id=? AND gelesen IS NULL", (t, ctx.ich, d["id"]))
    else:
        raise Fehler(400, "id oder alle erwartet")
    return {"ungelesen": ungelesene_meldungen(c, ctx.nutzer)}


def faelligkeit_pruefen(erzwingen=False):
    """Hoechstens stuendlich: Erinnerungen fuer faellige Tickets an die Zugewiesenen (einmal je Stand)."""
    global _faellig_zuletzt
    if not erzwingen and time.monotonic() - _faellig_zuletzt < 3600 and _faellig_zuletzt:
        return
    _faellig_zuletzt = time.monotonic()
    h = heute()
    grenze = (h + timedelta(days=3)).isoformat()
    t = jetzt()
    with schreiben() as c:
        for r in c.execute("SELECT * FROM ticket WHERE geloescht IS NULL AND status IN ('neu','bereit','arbeit','review') "
                           "AND wer IS NOT NULL AND faellig IS NOT NULL AND faellig <= ?", (grenze,)).fetchall():
            tag = date.fromisoformat(r["faellig"]).strftime("%d.%m.")
            text = (f"{ticket_id(r['nr'])} ist überfällig (seit {tag})" if r["faellig"] < h.isoformat()
                    else f"{ticket_id(r['nr'])} ist am {tag} fällig")
            if not c.execute("SELECT 1 FROM benachrichtigung WHERE nutzer=? AND ticket=? AND art='faellig' AND text=?",
                             (r["wer"], r["nr"], text)).fetchone():
                c.execute("INSERT INTO benachrichtigung(nutzer,ticket,art,text,zeit) VALUES(?,?,?,?,?)",
                          (r["wer"], r["nr"], "faellig", text, t))


# ---------------------------------------------------------------- kennzahlen, woche

def sichtbar_fn(c, nutzer):
    """Python-Seite der Sichtbarkeitsregel (fuer Auswertungen ueber alle Tickets)."""
    beob = {}
    for r in c.execute("SELECT ticket, nutzer FROM beobachter"):
        beob.setdefault(r["ticket"], set()).add(r["nutzer"])

    def sieht(r):
        if not r["vertraulich"]:
            return True
        if nutzer is None:
            return False
        return (nutzer["rolle"] == "admin" or nutzer["kurz"] in (r["ersteller"], r["wer"])
                or nutzer["kurz"] in beob.get(r["nr"], ()))
    return sieht


def montag(d):
    return d - timedelta(days=d.weekday())


def kennzahlen_daten(c, nutzer):
    """Zahlen ueber alle Tickets; Listen nur mit sichtbaren Tickets (vertrauliche zaehlen nur mit)."""
    h = heute()
    hs = h.isoformat()
    sieht = sichtbar_fn(c, nutzer)
    rows = c.execute(f"{TICKET_SELECT} WHERE t.geloescht IS NULL").fetchall()
    jetzt_s = jetzt()
    offen = [r for r in rows if r["status"] in OFFEN]
    je_person = {}
    for r in rows:
        if r["status"] in ("arbeit", "review") and r["wer"]:
            je_person.setdefault(r["wer"], {"arbeit": 0, "review": 0})[r["status"]] += 1
    acht_wochen = (datetime.fromisoformat(jetzt_s) - timedelta(weeks=8)).isoformat(timespec="seconds")
    fertige = [r for r in rows if r["status"] == "fertig" and r["fertig_am"] and r["fertig_am"] >= acht_wochen]
    durch = [tage_zwischen(r["angelegt"], r["fertig_am"]) for r in fertige]
    bearb = [tage_zwischen(r["begonnen"], r["fertig_am"]) for r in fertige if r["begonnen"]]

    def stat(w):
        if not w:
            return None
        return {"median": round(perzentil(w, 0.5), 1), "p85": round(perzentil(w, 0.85), 1), "anzahl": len(w)}
    neu_alt = [r for r in offen if r["status"] in ("neu", "bereit")]
    aelteste = sorted((r for r in neu_alt if sieht(r)), key=lambda r: r["angelegt"])[:5]
    blockiert = [r for r in offen if r["blockiert_grund"]]
    wochen = []
    m0 = montag(h)
    for i in range(7, -1, -1):
        von = m0 - timedelta(weeks=i)
        bis = von + timedelta(days=7)
        vs, bs = von.isoformat(), bis.isoformat()
        wochen.append({"von": vs,
                       "fertig": sum(1 for r in rows if r["fertig_am"] and vs <= r["fertig_am"][:10] < bs and r["status"] == "fertig"),
                       "neu": sum(1 for r in rows if vs <= r["angelegt"][:10] < bs)})
    ab = (h + timedelta(days=3)).isoformat()
    return {
        "stand": jetzt_s,
        "wip": {"arbeit": sum(1 for r in rows if r["status"] == "arbeit"),
                "review": sum(1 for r in rows if r["status"] == "review"),
                "limit_arbeit": einstellung_int(c, "wip_arbeit"), "limit_review": einstellung_int(c, "wip_review"),
                "je_person": [{"kurz": k, **v} for k, v in sorted(je_person.items())]},
        "offen": len(offen),
        "durchlaufzeit": stat(durch), "bearbeitungszeit": stat(bearb),
        "alter": {"aelteste": [{"id": ticket_id(r["nr"]), "titel": r["titel"], "status": r["status"],
                                "tage": int(tage_zwischen(r["angelegt"], jetzt_s))} for r in aelteste],
                  "triage_faellig": sum(1 for r in neu_alt if r["status"] == "neu" and tage_zwischen(r["angelegt"], jetzt_s) > 7),
                  "bereit_alt": sum(1 for r in neu_alt if r["status"] == "bereit" and tage_zwischen(r["angelegt"], jetzt_s) > 30)},
        "ueberfaellig": sum(1 for r in offen if r["faellig"] and r["faellig"] < hs),
        "faellig_3_tage": sum(1 for r in offen if r["faellig"] and hs <= r["faellig"] <= ab),
        "blockiert": {"anzahl": len(blockiert),
                      "laengste_tage": max((int(tage_zwischen(r["blockiert_seit"] or r["geaendert"], jetzt_s)) for r in blockiert), default=0),
                      "liste": [{"id": ticket_id(r["nr"]), "titel": r["titel"], "grund": r["blockiert_grund"],
                                 "tage": int(tage_zwischen(r["blockiert_seit"] or r["geaendert"], jetzt_s))}
                                for r in blockiert if sieht(r)]},
        "wochen": wochen,
        "vertraulich_offen": sum(1 for r in offen if r["vertraulich"] and not sieht(r)),
    }


@route("GET", "/api/kennzahlen")
def r_kennzahlen(ctx):
    return kennzahlen_daten(ctx.c, ctx.nutzer)


@route("GET", "/api/woche")
def r_woche(ctx):
    c = ctx.c
    try:
        start = montag(date.fromisoformat(ctx.query["von"])) if ctx.query.get("von") else montag(heute())
    except ValueError:
        raise Fehler(400, "von: Format JJJJ-MM-TT")
    h, hs = heute(), heute().isoformat()
    sieht = sichtbar_fn(c, ctx.nutzer)
    rows = c.execute(f"{TICKET_SELECT} WHERE t.geloescht IS NULL ORDER BY t.pos, t.nr").fetchall()
    ag = aggregate(c, ctx.nutzer)
    k = lambda r: karte(r, ag)
    offen = [r for r in rows if r["status"] in OFFEN and sieht(r)]
    tage = []
    for i in range(5):
        d = start + timedelta(days=i)
        ziele = {d} | ({d + timedelta(days=1), d + timedelta(days=2)} if i == 4 else set())   # Sa/So landen bei Fr
        items = sorted((r for r in offen if r["faellig"] and r["faellig"] >= hs and date.fromisoformat(r["faellig"]) in ziele),
                       key=lambda r: (r["faellig"], r["pos"]))
        tage.append({"datum": d.isoformat(), "tickets": [k(r) for r in items]})
    bis = start + timedelta(days=7)
    vs, bs = start.isoformat(), bis.isoformat()
    erledigt = [r for r in rows if r["status"] == "fertig" and r["fertig_am"] and vs <= r["fertig_am"][:10] < bs and sieht(r)]
    return {
        "von": vs, "bis": (bis - timedelta(days=1)).isoformat(), "heute": hs,
        "ueberfaellig": [k(r) for r in sorted((r for r in offen if r["faellig"] and r["faellig"] < hs), key=lambda r: r["faellig"])],
        "tage": tage,
        "ohne_termin": [k(r) for r in offen if r["status"] in ("arbeit", "review") and not r["faellig"]],
        "ruckblick": {
            "erledigt": [k(r) for r in erledigt],
            "neu_anzahl": sum(1 for r in rows if vs <= r["angelegt"][:10] < bs),
            "aelteste": [k(r) for r in sorted((r for r in offen if r["status"] in ("neu", "bereit")), key=lambda r: r["angelegt"])[:3]],
        },
    }


# ---------------------------------------------------------------- bericht (A4, ohne Skript)

MONATE = ("Jan.", "Feb.", "März", "Apr.", "Mai", "Juni", "Juli", "Aug.", "Sep.", "Okt.", "Nov.", "Dez.")


def datum_de(s):
    d = date.fromisoformat(s[:10])
    return f"{d.day}. {MONATE[d.month - 1]} {d.year}"


def kurz_de(s):
    d = date.fromisoformat(s[:10])
    return f"{d.day}.{d.month}."


def tage_text(f):
    return "–" if f is None else (f"{f:.1f}".replace(".", ",") + " Tage")


def bericht_daten(c, nutzer):
    h = heute()
    hs = h.isoformat()
    sieht = sichtbar_fn(c, nutzer)
    kz = kennzahlen_daten(c, nutzer)
    rows = c.execute(f"{TICKET_SELECT} WHERE t.geloescht IS NULL").fetchall()
    offen = [r for r in rows if r["status"] in OFFEN]
    sichtbar_offen = [r for r in offen if sieht(r)]
    jetzt_s = jetzt()
    stillstand = einstellung_int(c, "stillstand_tage")
    esk_ueberfaellig = sorted((r for r in sichtbar_offen if r["faellig"] and r["faellig"] < hs), key=lambda r: r["faellig"])
    esk_blockiert = [r for r in sichtbar_offen if r["blockiert_grund"]]
    esk_stillstand = [r for r in sichtbar_offen if r["status"] in ("arbeit", "review") and not r["blockiert_grund"]
                      and tage_zwischen(r["geaendert"], jetzt_s) > stillstand]
    bald_bis = (h + timedelta(days=14)).isoformat()
    bald = sorted((r for r in sichtbar_offen if r["faellig"] and hs <= r["faellig"] <= bald_bis), key=lambda r: r["faellig"])
    kunden = {}
    for r in sichtbar_offen:
        e = kunden.setdefault(r["kunde"] or "", {"offen": 0, "ueberfaellig": 0, "blockiert": 0, "termine": [], "eur": 0,
                                                 "klassen": {"klein": 0, "mittel": 0, "gross": 0}})
        e["offen"] += 1
        e["ueberfaellig"] += 1 if r["faellig"] and r["faellig"] < hs else 0
        e["blockiert"] += 1 if r["blockiert_grund"] else 0
        if r["faellig"]:
            e["termine"].append(r)
        e["eur"] += r["wert_eur"] or 0
        if r["wert_klasse"] in e["klassen"]:
            e["klassen"][r["wert_klasse"]] += 1
    for e in kunden.values():
        e["termine"].sort(key=lambda r: r["faellig"])
    woche_start = montag(h).isoformat()
    fertig_woche = sum(1 for r in rows if r["status"] == "fertig" and r["fertig_am"] and r["fertig_am"][:10] >= woche_start)
    return {"kz": kz, "stand": jetzt_s, "offen_status": {s: sum(1 for r in offen if r["status"] == s) for s in OFFEN},
            "esk_ueberfaellig": esk_ueberfaellig, "esk_blockiert": esk_blockiert, "esk_stillstand": esk_stillstand,
            "stillstand_tage": stillstand, "bald": bald, "kunden": kunden, "fertig_woche": fertig_woche,
            "vertraulich": kz["vertraulich_offen"], "arbeitsbereich": einstellung(c, "arbeitsbereich")}


BERICHT_CSS = """
:root{--fg:#1b1d20;--fg2:#50565e;--line:#d9dce0;--soft:#f6f7f8;--accent:#2557c7;--danger:#b3261e}
*{box-sizing:border-box}
body{margin:0;background:#eceef0;color:var(--fg);font:14px/1.45 system-ui,"Segoe UI",sans-serif}
main{max-width:210mm;margin:16px auto;background:#fff;padding:14mm 14mm 12mm;border:1px solid var(--line)}
h1{font-size:22px;margin:0 0 2px;font-weight:600}
h2{font-size:13px;font-weight:600;margin:18px 0 6px;padding-bottom:3px;border-bottom:1px solid var(--line)}
.stand{color:var(--fg2);font-size:13px;margin:0 0 14px}
.kz{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.kz div{border:1px solid var(--line);border-radius:6px;padding:6px 10px}
.kz b{display:block;font-size:20px;font-weight:600;line-height:1.2}
.kz span{font-size:12px;color:var(--fg2)}
.tw{overflow-x:auto}
table{width:100%;min-width:480px;border-collapse:collapse;table-layout:fixed;font-size:13px}
thead{display:table-header-group}
th{font-size:12px;text-align:left;color:var(--fg2);font-weight:600;border-bottom:1px solid var(--fg2);padding:3px 6px 3px 0;white-space:nowrap}
td{padding:3px 6px 3px 0;border-bottom:1px solid #dfe2e6;vertical-align:top;overflow-wrap:anywhere}
th.c-nr{width:64px}
th.c-kunde{width:19%}
th.c-wer{width:36px}
th.c-fall{width:52px}
th.c-seit{width:66px}
th.c-grund{width:27%}
td.nr{font-family:ui-monospace,Menlo,monospace;font-size:12px;color:var(--fg2);white-space:nowrap}
.warn{color:var(--danger);font-weight:600}
.leer{color:var(--fg2);margin:4px 0}
.kunde{break-inside:avoid;margin:0 0 8px}
.kunde h3{font-size:13px;margin:0;font-weight:600}
.kunde p{margin:0;color:var(--fg2);font-size:13px}
footer{margin-top:16px;padding-top:6px;border-top:1px solid var(--line);color:var(--fg2);font-size:12px}
.noprint{max-width:210mm;margin:8px auto;color:var(--fg2);font-size:12px;padding:0 4px}
@page{size:A4;margin:12mm}
@media print{body{background:#fff}main{margin:0;border:0;padding:0;max-width:none}.noprint{display:none}h2,h3{break-after:avoid}tr{break-inside:avoid}.tw{overflow:visible}table{min-width:0}td .warn{text-decoration:underline;text-underline-offset:2px}}
@media (max-width:640px){main{padding:12px}.kz{grid-template-columns:repeat(2,1fr)}}
"""


def bericht_html(daten):
    e = html.escape
    kz = daten["kz"]

    def kachel(zahl, text, warn=False):
        return f'<div><b class="{"warn" if warn and zahl else ""}">{e(str(zahl))}</b><span>{e(text)}</span></div>'

    def tab(zeilen, spalten):
        if not zeilen:
            return '<p class="leer">Nichts.</p>'
        klassen = {"Nr.": "c-nr", "Kunde": "c-kunde", "Wer": "c-wer", "Fällig": "c-fall", "Seit": "c-seit",
                   "Still seit": "c-seit", "Grund": "c-grund"}
        kopf = "".join(f'<th class="{klassen.get(s, "")}">{e(s)}</th>' for s in spalten)
        koerper = "".join(zeilen)
        return f'<div class="tw"><table><thead><tr>{kopf}</tr></thead><tbody>{koerper}</tbody></table></div>'

    def zeile(r, *extra):
        zellen = "".join(f"<td>{x}</td>" for x in extra)
        return (f'<tr><td class="nr">{ticket_id(r["nr"])}</td><td>{e(r["titel"])}</td>'
                f'<td>{e(r["kunde"] or "–")}</td><td>{e(r["wer"] or "–")}</td>{zellen}</tr>')
    durch = kz["durchlaufzeit"]
    h = []
    h.append(f'<h1>Statusbericht</h1><p class="stand">{e(daten["arbeitsbereich"])} · Stand {e(datum_de(daten["stand"]))}, '
             f'{e(daten["stand"][11:16])} Uhr</p>')
    h.append('<div class="kz">'
             + kachel(kz["offen"], "offen")
             + kachel(daten["offen_status"]["arbeit"], "in Arbeit")
             + kachel(daten["offen_status"]["review"], "im Review")
             + kachel(kz["blockiert"]["anzahl"], "blockiert", True)
             + kachel(kz["ueberfaellig"], "überfällig", True)
             + kachel(daten["fertig_woche"], "diese Woche fertig")
             + kachel(kz["faellig_3_tage"], "fällig in 3 Tagen")
             + kachel(tage_text(durch["median"]) if durch else "–", "Durchlaufzeit, Median (8 Wochen)")
             + "</div>")
    h.append("<h2>Was hängt</h2><h3 style='font-size:13px;margin:6px 0 2px'>Überfällig</h3>")
    h.append(tab([zeile(r, f'<span class="warn">{e(kurz_de(r["faellig"]))}</span>') for r in daten["esk_ueberfaellig"]],
                 ["Nr.", "Titel", "Kunde", "Wer", "Fällig"]))
    h.append("<h3 style='font-size:13px;margin:10px 0 2px'>Blockiert</h3>")
    h.append(tab([zeile(r, e(r["blockiert_grund"]), f'{int(tage_zwischen(r["blockiert_seit"] or r["geaendert"], daten["stand"]))} Tage')
                  for r in daten["esk_blockiert"]], ["Nr.", "Titel", "Kunde", "Wer", "Grund", "Seit"]))
    h.append(f"<h3 style='font-size:13px;margin:10px 0 2px'>Seit über {daten['stillstand_tage']} Tagen ohne Bewegung</h3>")
    h.append(tab([zeile(r, f'{int(tage_zwischen(r["geaendert"], daten["stand"]))} Tage') for r in daten["esk_stillstand"]],
                 ["Nr.", "Titel", "Kunde", "Wer", "Still seit"]))
    h.append("<h2>Fällig in den nächsten 14 Tagen</h2>")
    h.append(tab([zeile(r, e(kurz_de(r["faellig"]))) for r in daten["bald"]], ["Nr.", "Titel", "Kunde", "Wer", "Fällig"]))
    h.append("<h2>Je Kunde</h2>")
    if not daten["kunden"]:
        h.append('<p class="leer">Keine offenen Tickets.</p>')
    for name in sorted(daten["kunden"], key=lambda n: (n == "", n.casefold())):
        k = daten["kunden"][name]
        z1 = f'{k["offen"]} offen'
        if k["ueberfaellig"]:
            z1 += f' · <span class="warn">{k["ueberfaellig"]} überfällig</span>'
        if k["blockiert"]:
            z1 += f' · {k["blockiert"]} blockiert'
        z2 = ("Nächste Termine: " + "; ".join(f'{e(kurz_de(r["faellig"]))} {ticket_id(r["nr"])} {e(r["titel"][:60])}'
                                              for r in k["termine"][:2])) if k["termine"] else "Keine Termine"
        kl = ", ".join(f'{n} × {("groß" if t == "gross" else t)}' for t, n in k["klassen"].items() if n)
        z3 = "Offenes Risiko: " + (f'{k["eur"]:,} €'.replace(",", ".") if k["eur"] else "kein Betrag") + (f" ({kl})" if kl else "")
        h.append(f'<div class="kunde"><h3>{e(name or "Ohne Kunde")}</h3><p>{z1}</p><p>{z2}</p><p>{e(z3)}</p></div>')
    if daten["vertraulich"]:
        h.append(f'<p class="leer">{daten["vertraulich"]} vertrauliche Tickets sind gezählt, aber nicht aufgeführt.</p>')
    h.append("<footer>Nur für den internen Gebrauch. Enthält keine Kundenstammdaten, nur Kürzel und Verweise. "
             "Der Bericht zeigt immer den aktuellen Stand.</footer>")
    return ("<!doctype html><html lang=\"de\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><meta name=\"robots\" content=\"noindex\">"
            f"<title>Statusbericht</title><style>{BERICHT_CSS}</style></head><body>"
            "<p class=\"noprint\">Drucken mit Strg+P, als PDF speichern über den Druckdialog.</p>"
            f"<main>{''.join(h)}</main></body></html>")


def html_antwort(code, inhalt):
    return Antwort(code, inhalt.encode(), "text/html; charset=utf-8",
                   [("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'"),
                    ("Cache-Control", "no-store")])


@route("GET", "/bericht", auth=None)
def r_bericht(ctx):
    if not ctx.nutzer:
        return html_antwort(401, "<!doctype html><meta charset=utf-8><title>Anmelden</title>"
                                 "<p style='font:16px system-ui;margin:2em'>Bitte zuerst <a href='/'>anmelden</a>.</p>")
    return html_antwort(200, bericht_html(bericht_daten(ctx.c, ctx.nutzer)))


@route("GET", r"/bericht/([A-Za-z0-9_\-]{16,80})", auth=None)
def r_bericht_token(ctx, token):
    HOOK_DROSSEL.pruefen(ctx.ip + "|bericht")
    gespeichert = einstellung(ctx.c, "bericht_token")
    if not gespeichert or not hmac.compare_digest(gespeichert, token):
        HOOK_DROSSEL.fehlschlag(ctx.ip + "|bericht")
        return html_antwort(404, "<!doctype html><meta charset=utf-8><title>Nicht gefunden</title>"
                                 "<p style='font:16px system-ui;margin:2em'>Diesen Berichtslink gibt es nicht (mehr).</p>")
    return html_antwort(200, bericht_html(bericht_daten(ctx.c, None)))


# ---------------------------------------------------------------- csv

def csv_zelle(v):
    if v is None:
        return ""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v   # keine Formeln in Excel
    return v


@route("GET", "/api/export.csv")
def r_csv(ctx):
    c = ctx.c
    sicht, p = sichtbar_sql(ctx.nutzer)
    rows = c.execute(f"{TICKET_SELECT} WHERE t.geloescht IS NULL AND {sicht} ORDER BY t.nr", p).fetchall()
    labels = {}
    for r in c.execute("SELECT tl.ticket, l.name FROM ticket_label tl JOIN label l ON l.id=tl.label_id ORDER BY l.name"):
        labels.setdefault(r["ticket"], []).append(r["name"])
    out = io.StringIO()
    w = csv.writer(out, delimiter=";", lineterminator="\r\n")
    w.writerow(["Nr", "Titel", "Typ", "Status", "Prio", "Projekt", "Zuständig", "Ersteller", "Kunde", "Fällig",
                "Wertklasse", "Betrag EUR", "Vertraulich", "Blockiert", "Labels", "Angelegt", "Geändert", "Begonnen", "Fertig am"])
    for r in rows:
        w.writerow([csv_zelle(x) for x in (
            ticket_id(r["nr"]), r["titel"], r["typ"], r["status"], r["prio"], r["projekt_key"], r["wer"], r["ersteller"],
            r["kunde"], r["faellig"], r["wert_klasse"], r["wert_eur"], "ja" if r["vertraulich"] else "",
            r["blockiert_grund"], ", ".join(labels.get(r["nr"], [])), r["angelegt"], r["geaendert"], r["begonnen"], r["fertig_am"])])
    name = f"tickets-{heute().isoformat()}.csv"
    return Antwort(200, ("﻿" + out.getvalue()).encode("utf-8"), "text/csv; charset=utf-8",
                   [("Content-Disposition", f'attachment; filename="{name}"'), ("Cache-Control", "no-store")])


# ---------------------------------------------------------------- webhook

HOOK_EVENTS = ("X-GitHub-Event", "X-Gitea-Event", "X-Forgejo-Event", "X-Gogs-Event")


@route("POST", r"/api/hook/([A-Za-z0-9_\-]{16,80})", auth=None)
def r_hook(ctx, token):
    c = ctx.c
    HOOK_DROSSEL.pruefen(ctx.ip + "|hook")
    gespeichert = einstellung(c, "hook_token")
    if not gespeichert or not hmac.compare_digest(gespeichert, token):
        HOOK_DROSSEL.fehlschlag(ctx.ip + "|hook")
        raise Fehler(404, "nicht gefunden")
    d = ctx.body(limit=MAX_HOOK_BODY)
    event = next((ctx.h.headers.get(h) for h in HOOK_EVENTS if ctx.h.headers.get(h)), None)
    if event and event.lower() != "push":
        return {"ok": True, "ignoriert": event, "verknuepft": 0}
    repo = d.get("repository") if isinstance(d.get("repository"), dict) else {}
    repo_url = repo.get("html_url") if isinstance(repo.get("html_url"), str) else None
    commits = d.get("commits") if isinstance(d.get("commits"), list) else []
    if not commits and isinstance(d.get("head_commit"), dict):
        commits = [d["head_commit"]]
    neu, t = 0, jetzt()
    for cm in commits[:100]:
        if not isinstance(cm, dict) or not isinstance(cm.get("id"), str) or not re.fullmatch(r"[0-9a-fA-F]{7,64}", cm["id"]):
            continue
        msg = cm.get("message") if isinstance(cm.get("message"), str) else ""
        url = cm.get("url") if isinstance(cm.get("url"), str) else (f"{repo_url}/commit/{cm['id']}" if repo_url else None)
        if url and not re.fullmatch(r"https?://\S+", url):
            url = None
        autor = cm.get("author") if isinstance(cm.get("author"), dict) else {}
        autor_name = str(autor.get("username") or autor.get("name") or "")[:80] or None
        titel = (msg.splitlines()[0] if msg else "")[:200] or None
        for nr in sorted({int(n) for n in re.findall(rf"(?i)\b{PREFIX}-(\d{{1,9}})\b", msg)}):
            row = c.execute("SELECT nr FROM ticket WHERE nr=? AND geloescht IS NULL", (nr,)).fetchone()
            if row and verknuepfen(c, nr, "commit", cm["id"].lower(), url, titel, autor_name, None, "webhook", None, t):
                neu += 1
    return {"ok": True, "verknuepft": neu}


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    server_version = "ticket"
    timeout = SOCKET_TIMEOUT   # stockende Clients geben den Thread nach einer Weile wieder frei

    def log_message(self, fmt, *args):
        pass

    def version_string(self):
        return self.server_version   # keine Python-Version im Server-Kopf

    SICHERHEIT = (("X-Content-Type-Options", "nosniff"), ("X-Frame-Options", "DENY"),
                  ("Referrer-Policy", "same-origin"))

    def senden(self, a, cookies=()):
        self.send_response(a.code)
        self.send_header("Content-Type", a.ctype)
        self.send_header("Content-Length", str(len(a.body)))
        for k, v in self.SICHERHEIT + tuple(a.headers):
            self.send_header(k, v)
        if not any(k.lower() == "cache-control" for k, _ in a.headers):
            self.send_header("Cache-Control", "no-store")
        for ck in cookies:
            self.send_header("Set-Cookie", ck)
        self.end_headers()
        if a.body and self.command != "HEAD":
            self.wfile.write(a.body)

    def json(self, code, obj, cookies=()):
        self.senden(Antwort(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8"), cookies)

    def startseite(self):
        nonce = secrets.token_urlsafe(16)
        body = (WEB / "index.html").read_text(encoding="utf-8").replace("__NONCE__", nonce).encode()
        csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; img-src 'self' data:; "
               "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
        self.senden(Antwort(200, body, "text/html; charset=utf-8",
                            [("Content-Security-Policy", csp), ("Cache-Control", "no-cache")]))

    def herkunft_pruefen(self):
        o = self.headers.get("Origin")
        if o is None:
            return
        if o == "null" or urlsplit(o).netloc != self.headers.get("Host", ""):
            raise Fehler(403, "Anfrage von fremder Seite abgelehnt")

    def cookie_token(self):
        for teil in self.headers.get("Cookie", "").split(";"):
            k, _, v = teil.strip().partition("=")
            if k == "ticket_session":
                return v
        return None

    body_roh = b""

    def body_lesen(self):
        """Liest den ganzen Body, bevor das globale Schreibschloss genommen wird: ein Client, der nach den
        Kopfzeilen schweigt, darf keine anderen Schreibzugriffe aufhalten."""
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            raise Fehler(411, "Content-Length erforderlich (kein chunked)")   # sonst würde der Body stillschweigend ignoriert
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise Fehler(400, "ungültige Content-Length")
        if n < 0:
            raise Fehler(400, "ungültige Content-Length")
        if n > MAX_HOOK_BODY:
            raise Fehler(413, "zu groß")
        self.body_roh = self.rfile.read(n) if n else b""

    def bearbeiten(self, methode):
        url = urlsplit(self.path)
        path = unquote(url.path)
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        try:
            if methode == "GET" and path in ("/", "/index.html"):
                return self.startseite()
            treffer = None
            for m, muster, f, auth, schreibend in ROUTEN:
                if m == methode:
                    mm = muster.match(path)
                    if mm:
                        treffer = (f, auth, schreibend, mm.groups())
                        break
            if not treffer:
                raise Fehler(404, "nicht gefunden")
            f, auth, schreibend, gruppen = treffer
            nutzer, sitzung = sitzung_laden(self.cookie_token())
            if auth and not nutzer:
                raise Fehler(401, "Nicht angemeldet")
            if auth == "admin" and nutzer["rolle"] != "admin":
                raise Fehler(403, "Das darf nur ein Admin")
            if methode != "GET":
                self.herkunft_pruefen()
                if auth and not gleich(self.headers.get("X-CSRF-Token", ""), sitzung["csrf"]):
                    raise Fehler(403, "Sicherheitstoken fehlt oder stimmt nicht. Seite neu laden.")
                self.body_lesen()
            kontext = schreiben() if schreibend else lesen()
            with kontext as c:
                ctx = Ctx(self, query, nutzer, sitzung, c)
                res = f(ctx, *gruppen)
            if isinstance(res, Antwort):
                a = res
            elif isinstance(res, tuple):
                a = Antwort(res[0], json.dumps(res[1], ensure_ascii=False).encode(), "application/json; charset=utf-8")
            else:
                a = Antwort(200, json.dumps(res, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            self.senden(a, ctx.cookies)
        except Fehler as e:
            self.json(e.code, {"fehler": e.text, **e.extra})
        except sqlite3.IntegrityError as e:
            print("ticket.: Integritätsfehler:", e, file=sys.stderr)   # Tabellen-/Spaltennamen gehören nicht in die Antwort
            self.json(409, {"fehler": "Das passt nicht zu den vorhandenen Daten."})
        except sqlite3.Error as e:
            print("ticket.: Datenbankfehler:", e, file=sys.stderr)
            self.json(500, {"fehler": "Datenbankfehler"})
        except (UnicodeError, OverflowError):   # lone Surrogates, Zahlen jenseits von 64 Bit, Datum ausserhalb des Bereichs
            self.json(400, {"fehler": "Ungültige Eingabe"})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception:
            import traceback
            traceback.print_exc()
            self.json(500, {"fehler": "Interner Fehler"})

    def do_GET(self):
        self.bearbeiten("GET")

    def do_POST(self):
        self.bearbeiten("POST")

    def do_PATCH(self):
        self.bearbeiten("PATCH")

    def do_DELETE(self):
        self.bearbeiten("DELETE")


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Abgebrochene Verbindungen sind kein Fehler der App und brauchen keinen Stacktrace im Log.
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def main():
    init_db()
    with lesen() as c:
        if setup_noetig(c):
            setup_code()
            print("ticket.: noch kein Admin. Die Oberfläche fragt beim Öffnen nach dem Setup-Code "
                  + ("(aus TICKET_SETUP_CODE)." if os.environ.get("TICKET_SETUP_CODE") else "(steht oben im Log)."),
                  file=sys.stderr, flush=True)
    print(f"ticket.: läuft auf http://127.0.0.1:{PORT}", file=sys.stderr, flush=True)
    Server(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()

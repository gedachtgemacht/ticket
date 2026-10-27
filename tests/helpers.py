"""Gemeinsame Hilfen: temporaere DB, echter HTTP-Server auf 127.0.0.1, einfacher Client.

Kein Netz nach aussen, keine echten Zugangsdaten: Setup-Code und Passwoerter sind Testwerte.
"""
import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.cookies import SimpleCookie
from pathlib import Path

os.environ["TICKET_SETUP_CODE"] = "test-setup-code"
os.environ["TICKET_KUNDEN"] = "ACME,BETA,GAMMA,intern"
os.environ.setdefault("TICKET_TZ", "Europe/Vienna")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402

PW = "test-passwort-123"


class Client:
    def __init__(self, port):
        self.port = port
        self.cookies = {}
        self.csrf = None

    def req(self, methode, pfad, body=None, csrf=True, headers=None, roh=None, ctype="application/json"):
        h = dict(headers or {})
        daten = None
        if roh is not None:
            daten = roh
        elif body is not None:
            daten = json.dumps(body).encode()
        if daten is not None:
            h.setdefault("Content-Type", ctype)
        if self.cookies:
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        if csrf and self.csrf and methode != "GET":
            h.setdefault("X-CSRF-Token", self.csrf)
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            c.request(methode, pfad, body=daten, headers=h)
            r = c.getresponse()
            roh_antwort = r.read()
            for sc in r.getheaders():
                if sc[0].lower() == "set-cookie":
                    sk = SimpleCookie()
                    sk.load(sc[1])
                    for k, m in sk.items():
                        if m.value == "" or m["max-age"] == "0":
                            self.cookies.pop(k, None)
                        else:
                            self.cookies[k] = m.value
            self.letzte_header = dict(r.getheaders())
            self.rohe_header = r.getheaders()
            ct = r.getheader("Content-Type", "")
            if ct.startswith("application/json") and roh_antwort:
                return r.status, json.loads(roh_antwort)
            return r.status, roh_antwort
        finally:
            c.close()

    def get(self, pfad, **kw):
        return self.req("GET", pfad, **kw)

    def post(self, pfad, body=None, **kw):
        return self.req("POST", pfad, {} if body is None else body, **kw)

    def patch(self, pfad, body, **kw):
        return self.req("PATCH", pfad, body, **kw)

    def delete(self, pfad, **kw):
        return self.req("DELETE", pfad, **kw)

    def laden(self):
        """Stand holen und CSRF-Token merken."""
        s, d = self.get("/api/stand")
        if s == 200:
            self.csrf = d["ich"]["csrf"]
        return s, d

    def login(self, kurz, pw=PW):
        s, d = self.post("/api/login", {"kurz": kurz, "passwort": pw}, csrf=False)
        if s == 200:
            self.laden()
        return s, d


class AppTest(unittest.TestCase):
    """Frische DB + laufender Server je Testfall."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ticket-test-"))
        self.alte_db = server.DB
        server.DB = self.tmp / "ticket.db"
        server.zustand_zuruecksetzen()
        self.vorbereiten()
        server.init_db()
        self.httpd = server.Server(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def vorbereiten(self):
        """Hook fuer Unterklassen (z. B. alte DB anlegen, bevor init_db laeuft)."""

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        server.DB = self.alte_db
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- Bequemlichkeiten
    def client(self):
        return Client(self.port)

    def admin(self, kurz="AD", name="Alex Admin"):
        c = self.client()
        s, d = c.post("/api/setup", {"code": "test-setup-code", "name": name, "kurz": kurz, "passwort": PW}, csrf=False)
        self.assertEqual(s, 201, d)
        c.laden()
        return c

    def mitglied(self, admin, kurz, name=None, rolle="mitglied"):
        s, d = admin.post("/api/einladungen", {"rolle": rolle})
        self.assertEqual(s, 201, d)
        c = self.client()
        s, d = c.post("/api/einladung/" + d["token"], {"name": name or f"Person {kurz}", "kurz": kurz, "passwort": PW}, csrf=False)
        self.assertEqual(s, 201, d)
        c.laden()
        return c

    def ticket(self, c, titel="Testticket", **kw):
        s, d = c.post("/api/tickets", {"titel": titel, **kw})
        self.assertEqual(s, 201, d)
        return d

    def db(self):
        return server.conn()

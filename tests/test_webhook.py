import unittest

from helpers import AppTest, server


def push(commits, repo="https://git.example.org/team/repo"):
    return {"ref": "refs/heads/main", "repository": {"html_url": repo, "full_name": "team/repo"}, "commits": commits}


def commit(sha, msg, **kw):
    return {"id": sha, "message": msg, "url": f"https://git.example.org/team/repo/commit/{sha}",
            "author": {"name": "Tom Tester", "username": "tom"}, **kw}


class WebhookTest(AppTest):
    def setUp(self):
        super().setUp()
        self.a = self.admin()
        self.m = self.mitglied(self.a, "BE")
        self.ticket(self.m, "Erstes")
        self.ticket(self.m, "Zweites")
        self.token = self.a.post("/api/einstellungen/token/hook")[1]["hook_token"]
        self.hook = self.client()   # ohne Login, wie Forgejo
        self.pfad = "/api/hook/" + self.token

    def test_commit_wird_verknuepft(self):
        s, d = self.hook.post(self.pfad, push([commit("a" * 40, "Fix Rundung\n\nrefs TKT-1 und tkt-2, nicht TKT-99")]), csrf=False,
                              headers={"X-Forgejo-Event": "push"})
        self.assertEqual((s, d), (200, {"ok": True, "verknuepft": 2}))
        t = self.m.get("/api/tickets/TKT-1")[1]
        v = t["verknuepfungen"][0]
        self.assertEqual((v["art"], v["ref"], v["quelle"], v["titel"], v["autor"]), ("commit", "a" * 40, "webhook", "Fix Rundung", "tom"))
        self.assertEqual(v["url"], "https://git.example.org/team/repo/commit/" + "a" * 40)
        self.assertEqual(t["commits"], 1)
        self.assertEqual(self.m.get("/api/tickets/TKT-2")[1]["commits"], 1)
        ev = [e for e in t["ereignisse"] if e["feld"] == "verknuepfung"][0]
        self.assertIsNone(ev["wer"])   # System

    def test_doppelte_zustellung_legt_nichts_doppelt_an(self):
        body = push([commit("b" * 40, "TKT-1 erledigt")])
        self.assertEqual(self.hook.post(self.pfad, body, csrf=False)[1]["verknuepft"], 1)
        self.assertEqual(self.hook.post(self.pfad, body, csrf=False)[1]["verknuepft"], 0)
        self.assertEqual(self.m.get("/api/tickets/TKT-1")[1]["commits"], 1)

    def test_formate_github_gitea_forgejo(self):
        # GitHub: ohne repository.html_url im Commit, aber url vorhanden; Gitea: author.username fehlt teilweise
        s, d = self.hook.post(self.pfad, push([{"id": "c" * 40, "message": "TKT-1", "url": "https://github.com/o/r/commit/" + "c" * 40,
                                                "author": {"name": "Gina", "email": "g@example.org"}}]), csrf=False,
                              headers={"X-GitHub-Event": "push"})
        self.assertEqual(d["verknuepft"], 1)
        self.assertEqual(self.m.get("/api/tickets/TKT-1")[1]["verknuepfungen"][0]["autor"], "Gina")
        # ohne url im Commit: aus Repo-URL zusammensetzen
        s, d = self.hook.post(self.pfad, push([{"id": "d" * 40, "message": "TKT-2"}]), csrf=False, headers={"X-Gitea-Event": "push"})
        self.assertEqual(self.m.get("/api/tickets/TKT-2")[1]["verknuepfungen"][0]["url"],
                         "https://git.example.org/team/repo/commit/" + "d" * 40)
        # nur head_commit
        s, d = self.hook.post(self.pfad, {"head_commit": commit("e" * 40, "TKT-1 nochmal")}, csrf=False)
        self.assertEqual(d["verknuepft"], 1)

    def test_falscher_token_und_nicht_push(self):
        self.assertEqual(self.hook.post("/api/hook/" + "x" * 32, push([]), csrf=False)[0], 404)
        self.assertEqual(self.hook.post(self.pfad[:-3] + "abc", push([]), csrf=False)[0], 404)
        s, d = self.hook.post(self.pfad, push([commit("f" * 40, "TKT-1")]), csrf=False, headers={"X-GitHub-Event": "ping"})
        self.assertEqual((s, d["verknuepft"], d["ignoriert"]), (200, 0, "ping"))
        self.assertEqual(self.m.get("/api/tickets/TKT-1")[1]["commits"], 0)

    def test_token_rotation_sperrt_alten_token(self):
        neu = self.a.post("/api/einstellungen/token/hook")[1]["hook_token"]
        self.assertEqual(self.hook.post(self.pfad, push([]), csrf=False)[0], 404)
        self.assertEqual(self.hook.post("/api/hook/" + neu, push([]), csrf=False)[0], 200)
        self.a.delete("/api/einstellungen/token/hook")
        self.assertEqual(self.hook.post("/api/hook/" + neu, push([]), csrf=False)[0], 404)

    def test_ohne_token_konfiguration_ist_der_hook_aus(self):
        self.a.delete("/api/einstellungen/token/hook")
        self.assertEqual(self.hook.post(self.pfad, push([]), csrf=False)[0], 404)

    def test_ungueltige_daten_bringen_den_server_nicht_aus_dem_tritt(self):
        for body in ({}, {"commits": "x"}, {"commits": [None, 5, {"id": 7}]}, {"commits": [{"id": "zzzz", "message": "TKT-1"}]},
                     {"commits": [{"id": "a" * 40, "message": 12}]}, {"repository": 3, "commits": [{"id": "a" * 40, "message": "TKT-1", "url": "javascript:x"}]}):
            s, d = self.hook.post(self.pfad, body, csrf=False)
            self.assertEqual(s, 200, body)
        # javascript:-URL wurde nicht uebernommen
        v = self.m.get("/api/tickets/TKT-1")[1]["verknuepfungen"]
        self.assertTrue(all(not (x["url"] or "").startswith("javascript") for x in v))
        self.assertEqual(self.hook.req("POST", self.pfad, roh=b"payload=x", ctype="application/x-www-form-urlencoded")[0], 415)
        self.assertEqual(self.hook.req("POST", self.pfad, roh=b"[1,2]", ctype="application/json")[0], 400)

    def test_geloeschte_und_vertrauliche_tickets(self):
        self.a.delete("/api/tickets/TKT-2")
        self.m.patch("/api/tickets/TKT-1", {"vertraulich": True})
        s, d = self.hook.post(self.pfad, push([commit("1" * 40, "TKT-1 TKT-2")]), csrf=False)
        self.assertEqual(d["verknuepft"], 1)   # TKT-2 gelöscht -> ignoriert; TKT-1 vertraulich bekommt die Verknüpfung
        self.assertEqual(self.m.get("/api/tickets/TKT-1")[1]["commits"], 1)

    def test_wiederholte_fehlversuche_werden_gedrosselt(self):
        codes = [self.hook.post("/api/hook/" + "y" * 32, push([]), csrf=False)[0] for _ in range(22)]
        self.assertEqual(codes[0], 404)
        self.assertEqual(codes[-1], 429)
        self.assertEqual(self.hook.post(self.pfad, push([]), csrf=False)[0], 429)   # auch der richtige Token in der Sperre


if __name__ == "__main__":
    unittest.main()

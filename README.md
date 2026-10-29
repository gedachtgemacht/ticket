# ticket.

**A ticket board for a small team that runs on your own server and needs nothing but Python.**

ticket. is a self-hosted team ticket board and kanban board: a Slack-style sidebar with projects as channels and a thread on every ticket, plus a Trello-style board with lists and cards. It is built on the Python standard library and SQLite, with one static interface in `web/index.html` (no framework, no CDN dependency, no outbound network access, no email sending). Ticket numbers are `TKT-n`.

![License: MIT](https://img.shields.io/badge/license-MIT-blue) ![Python stdlib only](https://img.shields.io/badge/Python-stdlib_only-3776ab) ![SQLite](https://img.shields.io/badge/database-SQLite-003b57) ![Self-hosted](https://img.shields.io/badge/self--hosted-yes-2ea44f)

The app interface is in German.

![Board view in dark mode: sidebar with projects and people, five lists from new to done](docs/screenshots/board-dunkel.png)

| Card with thread | Board, light | Phone |
|---|---|---|
| ![Open card with fields, checklist and comment thread](docs/screenshots/karte-dunkel.png) | ![Board view in light mode](docs/screenshots/board-hell.png) | ![Board on a phone, lists as tabs](docs/screenshots/mobil-hell.png) |

The screenshots show made-up demo data.

- `server.py` the server, 127.0.0.1:8909 (`TICKET_PORT`), systemd unit `ticket` running as user `ticket`
- `web/index.html` the whole interface (Slack-style shell: sidebar, projects as channels, thread; Trello-style board: lists, cards, card back)
- `tests/` tests with `unittest` against temporary databases: `python3 -m unittest discover -s tests`
- Data in `/var/lib/ticket/ticket.db` (`TICKET_DB`); backup: copy the file, or run `sqlite3 ticket.db ".backup ziel.db"` regularly and copy the result away
- Time zone `TICKET_TZ` (default `Europe/Vienna`)

## How it works

Two levels: **project** (shown as a channel in the sidebar, "Allgemein" ("General") for loose topics) and **ticket**. Subtasks are checklists on the ticket. Status: `neu` ("new") → `bereit` ("ready") → `arbeit` ("in progress") → `review` → `fertig` ("done"), plus `verworfen` ("discarded") and the flag **blockiert** ("blocked", with a reason). The board shows five lists; `fertig` only shows the last 14 days, after that tickets move to the archive (tab **Liste** ("List"), **Archiv einblenden** ("show archive")). No sprints.

- **WIP hint:** more than 3 tickets in `arbeit` per person, or more than 5 in `review`, asks for a reason ("Warum trotzdem?", "Why anyway?") but does not block. The limits are in the settings. `verworfen` and reopening from `fertig` also need one sentence. The reason is saved as a comment on the ticket.
- **Order** within a list is stored in the field `ticket.pos` and saved when you drag.
- **Confidential:** only the creator, assignees, watchers and admins see the ticket (board, search, export, activity, report). Everyone else only sees a count of "vertrauliche Tickets" ("confidential tickets").
- **History** is append-only (database triggers forbid UPDATE/DELETE). Deleting is a soft delete (admins only, trash under administration). Comments can be edited or deleted (only your own; admins may delete other people's); the old wording stays in the history (only admins see it).
- No customer master data or personal data of third parties in tickets. A customer is just a short code (the list grows with use; preset suggestions via `TICKET_KUNDEN="ACME,BETA,intern"`).
- **Presence** (active / away / do not disturb) comes from the interface's last poll (every 20 s). The timestamp itself is not sent out, only the state. This counts as employee data: clear it with your works council / data protection officer before use; it can be turned off under Verwaltung → Einstellungen (Administration → Settings).

## First start and accounts

Own accounts, no nginx login. Passwords hashed with scrypt, session cookie (HttpOnly, SameSite=Lax, 90 days sliding, `Secure` behind HTTPS), CSRF token for all write requests.

1. If there is no admin yet, the server prints a **setup code** to the log on start (`journalctl -u ticket`; or set it fixed via `TICKET_SETUP_CODE`).
2. The interface then shows the setup: code, name, short code (Kürzel), password. A short code from the old board is taken over and becomes admin.
3. More people: Verwaltung → Personen → **Einladungslink erzeugen** (Administration → People → "create invite link"; usable once, valid 7 days). Whoever opens it sets name, short code and password. For short codes from the old board there is "Einladungslink" (name and short code are already filled in) and later "Zugang zurücksetzen" ("reset access").
4. Roles: `mitglied` (member) and `admin`. Admins manage people, settings and trash, close projects and delete labels.

## Migration from the old schema

On start, `server.py` detects the old schema (table `leute`) and migrates in **one transaction**. Before that, a backup of the file is written next to the database: `ticket.db.vor-neu-JJJJMMTT` (YYYYMMDD). If the migration fails, the database stays unchanged and the server does not start. To go back to the old state: stop the service, copy the backup over `ticket.db`, put the old `server.py` back.

- `leute` → `nutzer` (the short code stays the key, all as members, without password), everything in project "Allgemein", type `aufgabe` (task)
- Status `backlog` → `bereit`; `arbeit`/`review`/`fertig` unchanged; plus one history entry "Backlog → Bereit" each
- `begonnen`, `fertig_am`, `ersteller` (started, done on, creator) reconstructed from the history (otherwise filled from `angelegt`/`geaendert`)
- `teilaufgabe` → checklist "Checkliste" + `unteraufgabe`; comments, history, numbers, `pos`, trash and sprint archive (`archiv_sprint`, table `sprint`) are kept. Old `TKT-n` links keep working.
- After the migration nobody has a password: run the setup (see above), then send invite links.

## Webhook for commits

Verwaltung → Einstellungen → "Webhook für Commits" (Administration → Settings → "Webhook for commits") creates a URL `https://…/api/hook/<token>`. Add it as a webhook in Forgejo, Gitea or GitHub (event Push, content type `application/json`). If `TKT-12` appears in a commit message, the commit is linked to the ticket (once; duplicate delivery does no harm). The token is the secret: only use it over HTTPS, and generate a new one if you suspect it leaked. Linking by hand always works: in the ticket, "Verknüpfung" ("link": hash, URL, file path or TKT number).

## Report and export

- `/bericht` A4 status report (ready to print, Ctrl+P), requires login. Key figures, what is stuck, due dates, per customer. Always shows the current state.
- Report link without login: Verwaltung → Einstellungen → "Berichtslink" (`/bericht/<token>`, read-only, confidential tickets only as a count).
- CSV of all tickets you can see: user menu → "Tickets als CSV" (`;`-separated, UTF-8 with BOM, for Excel).

## Deploy

Example with systemd and nginx (adjust paths and domain):

    install -d /opt/ticket/web /var/lib/ticket
    cp server.py /opt/ticket/ && cp web/index.html /opt/ticket/web/
    cp ticket.service /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now ticket
    # nginx-ticket.conf als Site einbinden, Domain und Zertifikatspfade anpassen

The app has its own accounts, so do not put Basic Auth in front of it: report and webhook links must be reachable without an nginx login. `nginx-ticket.conf` sets `X-Forwarded-Proto` (for the `Secure` cookie) and `client_max_body_size 1m` (push webhooks).

**Behind a proxy, set `TICKET_TRUST_PROXY=1`** (it is in `ticket.service`; on an existing installation, update the unit and run `systemctl daemon-reload`). Only then is `X-Real-IP` used as the client address (login throttle, setup, webhook, invites). Without the variable, the address of the connection counts, and a header set by the client cannot get around the throttle. Only set it if the proxy sets the header itself and does not just pass it through (nginx: `proxy_set_header X-Real-IP $remote_addr;`, as in the template). If it is missing behind a proxy, all people share one throttle bucket (8 failed attempts per short code, 40 in total within 10 minutes).


## Run and test locally

    TICKET_DB=/tmp/ticket.db python3 server.py     # http://127.0.0.1:8909
    python3 -m unittest discover -s tests          # ca. 1 Minute, kein Netz nötig

## License

MIT, see `LICENSE`.

**Deutsch:** ticket. ist ein Ticket-Board und Kanban-Board für Teams zum Selbsthosten, mit Projekten als Kanälen, Threads und eigenen Konten, nur Python und SQLite, deutsche Oberfläche.

---

**gedacht.gemacht.** Small, honest software. Self-hosted, your data stays on your own server. · [More projects](https://github.com/gedachtgemacht)

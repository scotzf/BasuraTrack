# Sukod

Waste measurement for Philippine campuses, LGUs and permitted establishments.

> Everyone reports what the bin is labelled. We report what's actually in it.

## Run it

```bash
.venv/Scripts/python.exe manage.py migrate
```

```bash
.venv/Scripts/python.exe manage.py seed_demo
```

```bash
.venv/Scripts/python.exe manage.py runserver
```

Sukod has **three separate interfaces**, one per role. They are separate on
purpose: what a janitor cannot see, a janitor cannot be measured by.

| Who | Where | What they do | How they get in |
|---|---|---|---|
| **Janitor** | `/` | Records sacks. Nothing else. | Types a pairing code once. No account, no login, ever. |
| **Supervisor** | `/monitor/` | Monitors: dashboard, reports, weekly purity checks. Cannot edit an entry. | Username and password |
| **Admin** | `/manage/` | Creates the other two. Calibration, devices, accounts. | Username and password |

The seeder sets up all three:

| Role | Open | Sign in with |
|---|---|---|
| Janitor | `/` | pairing code `SUKOD1` |
| Supervisor | `/monitor/` | `supervisor` / `sukod-super-2026` |
| Admin | `/manage/` | `admin` / `sukod-admin-2026` |

For a real site, create the first admin yourself and let them build the rest:

```bash
.venv/Scripts/python.exe manage.py createsuperuser
```

A Django superuser with no Staff row is treated as an admin. Without that rule
the first account would be locked out of the screen that creates accounts.

### Why the janitor has no navigation bar

Two rules in CLAUDE.md depend on it, and neither survives a shared menu:

- **The purity check is not the janitor's job.** A check done in a hurry by the
  person whose sacks are being judged is worthless. It is not on their screen,
  so they will not be asked to do it.
- **Logged volume must never be a performance measure for the janitor.** Their
  interface has no dashboard, no totals and no comparison between areas. They
  cannot see a number that could be held against them, because none is rendered.

Enforcement is server-side in `core/roles.py`, not just missing links.
`RoleSeparationTests` asserts both the permissions and the absence of the links.

### Where each screen lives

| Screen | Path | Role |
|---|---|---|
| Logger (the notebook) | `/` | janitor |
| Pair a device | `/pair/` | janitor |
| Dashboard | `/monitor/` | supervisor |
| Purity check | `/monitor/purity/` | supervisor |
| Purity over time | `/monitor/purity/history/` | supervisor |
| Report | `/monitor/report/` | supervisor |
| Overview | `/manage/` | admin |
| Accounts | `/manage/staff/` | admin |
| Devices and pairing codes | `/manage/devices/` | admin |
| Calibration | `/manage/calibration/` | admin |
| Django admin | `/django-admin/` | admin |

The old flat paths (`/dashboard/`, `/purity/`, `/calibration/`) redirect to
their new homes.

## Letting someone else test it

```bash
.\share.ps1
```

Starts the server and an ngrok tunnel, then prints a public HTTPS address and
the pairing code. HTTPS is what makes the progressive web app installable on a
phone; a service worker will not run over plain HTTP.

On ngrok's free plan the visitor must click **"Visit Site"** on the warning page
once. Until they do, ngrok intercepts everything including `sw.js` and
`app.js`, and the logger appears broken.

## Tests

```bash
.venv/Scripts/python.exe manage.py test core
```

63 tests. The ones that matter most:

- **Purity is never assumed to be 1.0.** `test_unchecked_returns_none_not_one`
  and `test_unmeasured_purity_contributes_nothing_to_true_recoverable`.
- **The fallback order** — this area's own check, then a declaration, then the
  site average, then nothing — has a test per step.
- **Declared purity** is marked distinctly and named in the limitations output.
- **A replayed offline queue never double-counts** (`OfflineSyncTests`).
- **The three interfaces are actually separate** (`RoleSeparationTests`): a
  supervisor is refused the back room, an anonymous visitor is bounced to the
  login screen, and the janitor's page contains no link to a dashboard.
- Every function in `core/calc.py` carries a worked example as a doctest:

```bash
.venv/Scripts/python.exe -m doctest core/calc.py
```

## Where things live

```
core/calc.py        The maths. Pure functions, no database, no Django.
core/roles.py       Who may see which of the three interfaces.
core/services.py    The only place that turns database rows into calc.py input.
core/views.py       Screens + the idempotent sync endpoint.
core/models.py      Schema.
static/app.js       The logger: IndexedDB queue, counters, silent sync.
templates/sw.js     Service worker. Caches the shell, never the API.
```

`calc.py` is deliberately Django-free so the numbers can be tested on their own.

## Database

SQLite by default so it runs anywhere. For MySQL:

```bash
SUKOD_DB=mysql SUKOD_DB_NAME=sukod SUKOD_DB_USER=root SUKOD_DB_PASSWORD=secret python manage.py migrate
```

## What the seed data deliberately gets wrong

The demo site leaves the **Library** with no purity check of its own, and gives
the staff-only **Kitchen** a declared purity. That is so the dashboard has
something to mark `SITE AVG` and `DECLARED`, and the report has something real
to put in its limitations section. A demo where everything is measured hides the
exact behaviour this system exists to make visible: what it does when it doesn't
know.

## Build status against the phased plan

- **Phase 1 — core loop:** done. Models, admin, pairing, logger, calibration,
  dashboard with mass and uncertainty.
- **Phase 2 — the differentiator:** done. Purity check flow, purity driving true
  recoverable and diversion, purity history, report.
- **Phase 3 — QR:** partial. `/a/<token>/` resolves a token to the logger with
  the area preselected, and every source area has a token. The printable sign
  PDF and the in-browser scanner are not built.
- **Phase 4 — administration:** partial. The three interfaces are separated by
  role, and the admin has purpose-built screens for accounts, devices and
  calibration. Django admin still covers entries, flags and site settings, and
  `AuditEntry` records undos and every account created. The purpose-built
  entries table with inline edit is not built.

Not built, by instruction: image classification, hardware integration, a
student-facing app, per-person accounts, a native app, multi-tenant billing,
notifications infrastructure.

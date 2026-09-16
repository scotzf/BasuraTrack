# CLAUDE.md — Sukod

Project configuration for Claude Code. Read this before writing any code.

---

## What we are building

**Sukod** — a waste measurement system for Philippine campuses, LGUs and permitted establishments.

It converts sack counts into kilograms using calibrated sack weights, measures how contaminated each segregated stream actually is, and reports a defensible diversion rate with a stated margin of error.

**The core claim, which every design decision must protect:**
> Everyone reports what the bin is labelled. We report what's actually in it.

**Non-negotiable:** no figure is ever displayed without its uncertainty and its method tag. If a screen shows a mass without an error band, that screen is wrong.

---

## Working with Denver

- Explain errors at a high level first, in plain language, before any error codes or stack traces.
- Write the code. Denver reviews it rather than writing it himself.
- **Use thorough inline comments** explaining what each block does and why — not just what the syntax is.
- For small changes, give a targeted diff: which file, what to change, what command to run. Never a full project re-download.
- Explain the underlying mechanism before the API surface. How it works first, then how to call it.
- Spell out every acronym and abbreviation on first use in a session, then use the short form.
- Keep explanations short and precise.

---

## Domain glossary

| Term | Meaning |
|---|---|
| **Source area** | A building or location whose waste is collected at one staging point (Canteen, Main Building, CS Building) |
| **Stream** | Biodegradable, Recyclable, Residual, Mixed |
| **Staging point** | Where sacks are gathered before the hauler takes them. Where logging happens. |
| **Calibration** | A weighing session establishing mean kg per full sack for a stream |
| **Fill level** | Full (1.00), two-thirds (0.67), half (0.50) |
| **Purity** | Share of items in a stream that actually belong there, from a spot-check |
| **Gross recoverable** | Mass in recoverable streams, by bin label |
| **True recoverable** | Gross x purity. What is actually clean enough to be recycled. |
| **Diversion rate** | True recoverable / total mass |
| **Method tag** | W weighed, C counted-and-calibrated, V volume-estimated, I imputed |

---

## The maths — implement exactly

```python
# Mass of one entry
kg = count * FILL_FACTOR[fill] * calibration.mean_kg

# Uncertainty of one counted entry (standard error of the calibrated mean)
sigma = count * FILL_FACTOR[fill] * (calibration.sd_kg / sqrt(calibration.n))

# Combining independent entries: add variances, not standard deviations
total_sigma = sqrt(sum(sigma_i ** 2 for each entry))

# Entries tagged W contribute mass but no estimation uncertainty.

# Purity from a spot-check
purity = 1 - (items_wrong / items_checked)

# True recoverable, per recoverable stream
true_recoverable_kg = stream_kg * purity

# Diversion
diversion_rate = sum(true_recoverable across recoverable streams) / total_kg
```

**Purity resolution order:** use the check for that exact source area and stream; if none, fall back to the site-wide average for that stream; if none exists, show "not yet checked", exclude it from true recoverable, and print the limitation line on any report that includes it.

**Never silently assume purity is 1.0.** Unmeasured is not the same as clean.

**Declared-purity streams:** a stream whose bin is filled only by trained staff (kitchen cardboard, office paper, bar bottles) may be marked `purity_declared` with a stated value and a reason. These render with a distinct marker and are named in the report's limitations section. Public-facing bins can never be declared — they must be measured.

---

## Stack

- **Backend:** Django + Django REST Framework, MySQL
- **Frontend:** Django templates + vanilla JavaScript. No React, no build step.
- **Offline:** service worker + IndexedDB
- **PDF:** WeasyPrint
- **Target:** Android Chrome, installable progressive web app

**Why no framework:** the logger must load fast on a cheap Android phone over weak signal. A build pipeline buys nothing here.

---

## Progressive web app requirements

Three things make it installable. Get them right from the start rather than retrofitting:

1. Served over HTTPS
2. `manifest.json` with name, icons (192 and 512 px), `display: "standalone"`, theme colour
3. A registered service worker with an offline fallback

**Offline is the assumption, not a feature.** Every entry is written to IndexedDB first and always. Sync is background and silent. Never show a spinner or a failure dialog on save.

**Duplicate protection:** every entry gets a client-generated UUID at creation. The server ignores an entry whose UUID it already holds. A re-sent queue must never double-count.

---

## Data model

```
Site(id, name, type, junkshop_rates_json)
SourceArea(id, site, name, denominator_type, denominator_value, qr_token,
           public_facing, archived)
Stream(id, name, recoverable, colour)
Calibration(id, site, stream, mean_kg, sd_kg, n, measured_on)
CalibrationSample(id, calibration, kg)      # individual weighings
SackLog(id_uuid, source_area, stream, count, fill, method, logged_at,
        device, lat, lng, location_verified, note)
PurityCheck(id, source_area, stream, items_checked, items_wrong,
            main_contaminant, checked_at, by_device)
DeclaredPurity(id, source_area, stream, value, reason, declared_by, declared_on)
Device(id, site, label, role, paired_at, last_seen)
Flag(id, sacklog, kind, resolved, resolved_by, note)
AuditEntry(id, actor, action, target, before_json, after_json, at)
```

**Roles:** logger, supervisor, admin. Loggers pair by code and never see a login screen.

---

## Build order

Stop at any phase and there is still something demonstrable.

**Phase 1 — core loop**
Models and admin, device pairing, Today screen, New entry screen, calibration entry, dashboard with mass and uncertainty

**Phase 2 — the differentiator (hackathon target)**
Purity check flow, purity affects true recoverable and diversion, purity history, PDF report

**Phase 3 — QR**
QR token per source area, printable sign PDF, scan screen, area picker fallback

**Phase 4 — administration**
Entries table with edit and audit trail, flags queue, devices, users, site settings

---

## UI rules

**Logger — it is a notebook, not an app.**
- All four streams on one screen with plus/minus counters. The user is standing in front of all the sacks at once.
- Opens on the last-used source area.
- Under ten seconds, start to finish. This is a hard requirement, not a goal.
- Large touch targets, high contrast, readable in direct sunlight, one-handed.
- Today's entries visible below, with undo on the most recent. No confirmation dialog.
- Sync status as a quiet line: "3 entries waiting". Never a modal.

**Location:** capture in the background, attach to the entry, **never block saving on it**. No fix means save anyway and tag the entry unverified. Mismatches surface on the dashboard for a supervisor to review — never to the logger in the moment.

**Dashboard**
- Time control across the top: Day, Week, Month, Year, Custom. Applies to everything below.
- Contaminated portion is drawn, not hidden. It is the argument.
- Per-source comparison is always normalized (per person per day, per room-night, per meal). Raw totals always rank the biggest area worst, which is not a finding.
- Compare against the same period last year, not last month. Waste is seasonal.
- Missing entries are flagged as gaps, never treated as zero.

---

## Who does the work — encoded assumptions

| Task | Who | Frequency | Time |
|---|---|---|---|
| Log sacks | Janitorial staff, or one person at a central staging point | Every pickup | 10 seconds |
| Purity check | Supervisor, student assistant, or PCO | Weekly, public-facing areas only | 15 minutes |
| Calibration | Admin | Quarterly | One session, 30+ sacks per stream |

The purity check is **not** the logger's job. Do not design flows that assume it is.

**Centralized logging is supported and preferred where it fits.** If a site funnels all waste to one staging point before collection, one person logs everything there, once a day. Source attribution then depends on sacks arriving identifiably by origin — the schema must allow an entry to record source area independently of which device logged it.

**Fallback modes**, in order, if daily logging does not take hold. Build mode 1 now, keep mode 2 cheap to add:
1. Log once at hauler pickup instead of per staging visit
2. Photograph the staging pile daily; a reviewer counts sacks from the photo and enters them
3. Hauler invoice weights only — no source attribution, no purity

---

## Do not build

- Image classification of waste. Field accuracy is unreliable and no public dataset of contaminated recyclables exists.
- Any hardware integration or sensor support.
- A student-facing scanning app. Voluntary logging biases the sample and cannot produce a total.
- Per-person accounts for janitorial staff. Device pairing only.
- Native Android app. Installable PWA only.
- Multi-tenant billing. Add sites manually until someone pays.
- Notifications infrastructure. A weekly email is enough.

---

## Design rule that outranks features

**Never use logged volume to evaluate the person logging it.** If the number becomes a performance measure for the janitor, they will report less, and the data dies. The system measures buildings and streams, never people. Keep this true in the schema, the dashboard, and anything exportable.

---

## Testing

- Unit tests on the maths: mass, uncertainty combination, purity fallback order, diversion.
- **Test the purity fallback explicitly.** Silently assuming 100% purity is the failure that would most damage credibility.
- Test that a declared-purity stream is marked distinctly and appears in the limitations output.
- Offline test: log entries with the network disabled, restore it, confirm no duplicates and no losses.
- Every calculation function gets a test with a worked example in the docstring.

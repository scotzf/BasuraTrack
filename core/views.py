"""
Sukod views.

Two families live here:

* HTML screens (logger, dashboard, purity, calibration) rendered from Django
  templates. No build step, no framework.
* A small JSON API the service worker posts to when a queued entry syncs.

The sync endpoint is the one that has to be bulletproof. It is idempotent:
posting the same client UUID twice is a no-op the second time.
"""
import json
import uuid
from datetime import datetime, timedelta

from django.db import transaction
from django.http import JsonResponse, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import calc, services
from .roles import admin_required, staff_for, supervisor_required
from .models import (
    AuditEntry, Calibration, CalibrationSample, DeclaredPurity, Device, Flag,
    PurityCheck, SackLog, Site, SourceArea, Staff, Stream, FILL_FACTOR,
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def current_site():
    """
    Single-site deployment for now. Multi-tenant billing is explicitly out of
    scope, so we take the first site rather than inventing a tenant resolver.
    """
    return Site.objects.first()


def device_from_request(request):
    """
    Identify the posting phone by its paired token.

    Loggers have no accounts. The token is stored in localStorage on the
    device at pairing time and sent with every entry.
    """
    token = (request.headers.get("X-Sukod-Device")
             or request.POST.get("device_token")
             or request.session.get("device_token"))
    if not token:
        return None
    return Device.objects.filter(token=token).first()


def parse_period(request):
    """
    Read the dashboard time control: ?period=day|week|month|year|custom.

    Returns (start, end, label, period_key). `end` is exclusive.
    Everything on the dashboard obeys this one window.
    """
    period = request.GET.get("period", "week")
    now = timezone.localtime()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    if period == "day":
        start, end, label = today, today + timedelta(days=1), "Today"
    elif period == "month":
        start, end, label = today - timedelta(days=30), today + timedelta(days=1), "Last 30 days"
    elif period == "year":
        start, end, label = today - timedelta(days=365), today + timedelta(days=1), "Last 12 months"
    elif period == "custom":
        # Bad or missing dates fall back to the week rather than erroring:
        # a dashboard that refuses to draw is worse than one showing a default.
        try:
            start = timezone.make_aware(
                datetime.strptime(request.GET["from"], "%Y-%m-%d"))
            end = timezone.make_aware(
                datetime.strptime(request.GET["to"], "%Y-%m-%d")) + timedelta(days=1)
            label = f"{start:%d %b %Y} to {(end - timedelta(days=1)):%d %b %Y}"
        except (KeyError, ValueError):
            start, end, label = today - timedelta(days=7), today + timedelta(days=1), "Last 7 days"
            period = "week"
    else:
        period = "week"
        start, end, label = today - timedelta(days=7), today + timedelta(days=1), "Last 7 days"

    return start, end, label, period


# --------------------------------------------------------------------------
# Pairing
# --------------------------------------------------------------------------

def pair(request):
    """
    The only screen a logger ever sees that is not the notebook. Type the code
    once; the device remembers the token from then on.
    """
    error = None
    if request.method == "POST":
        code = request.POST.get("code", "").strip().upper()
        device = Device.objects.filter(pair_code=code).first()
        if device:
            device.paired_at = device.paired_at or timezone.now()
            device.last_seen = timezone.now()
            device.save(update_fields=["paired_at", "last_seen"])
            request.session["device_token"] = device.token
            return render(request, "pair_done.html",
                          {"device": device, "token": device.token})
        error = "That code did not match any device. Check with your supervisor."
    return render(request, "pair.html", {"error": error})


# --------------------------------------------------------------------------
# Logger - the notebook
# --------------------------------------------------------------------------

def logger_screen(request):
    """
    All four streams on one screen with plus/minus counters, because the person
    is standing in front of all the sacks at once. Opens on the last-used
    source area (the phone remembers it; see app.js).

    Everything below is rendered once and driven client-side. There is no
    round trip between opening this screen and the entry being saved locally.
    """
    site = current_site()
    device = device_from_request(request)
    areas = SourceArea.objects.filter(site=site, archived=False)
    streams = Stream.objects.all()

    return render(request, "logger.html", {
        "site": site,
        "device": device,
        "areas": areas,
        "streams": streams,
        "fills": [("full", "Full"), ("two_thirds", "2/3"), ("half", "1/2")],
    })


@csrf_exempt
@require_POST
def sync(request):
    """
    Accept a batch of entries from the device queue.

    Idempotency: each entry carries a UUID the phone generated at creation.
    We use it as the primary key, so a re-sent queue collides with rows that
    already exist and we skip them. The response names which UUIDs are now
    held, which is how the phone knows what it may drop from its queue.
    """
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "bad json"}, status=400)

    device = device_from_request(request)
    accepted, duplicates, rejected = [], [], []

    for item in payload.get("entries", []):
        try:
            entry_id = uuid.UUID(item["id_uuid"])
        except (KeyError, ValueError, TypeError):
            rejected.append({"item": item, "why": "missing or malformed id_uuid"})
            continue

        # Already held -> acknowledge and move on. Never write twice.
        if SackLog.objects.filter(pk=entry_id).exists():
            duplicates.append(str(entry_id))
            continue

        try:
            area = SourceArea.objects.get(pk=item["source_area"])
            stream = Stream.objects.get(pk=item["stream"])
        except (KeyError, SourceArea.DoesNotExist, Stream.DoesNotExist):
            rejected.append({"id_uuid": str(entry_id), "why": "unknown area or stream"})
            continue

        logged_at = item.get("logged_at")
        logged_at = (timezone.datetime.fromisoformat(logged_at)
                     if logged_at else timezone.now())
        if timezone.is_naive(logged_at):
            logged_at = timezone.make_aware(logged_at)

        lat, lng = item.get("lat"), item.get("lng")
        verified = False
        if lat is not None and lng is not None and area.lat and area.lng:
            # Crude degree-box check, deliberately generous (~200 m). A GPS
            # fix under a concrete roof is not evidence of fraud.
            verified = abs(lat - area.lat) < 0.002 and abs(lng - area.lng) < 0.002

        with transaction.atomic():
            log = SackLog.objects.create(
                id_uuid=entry_id,
                source_area=area,
                stream=stream,
                count=int(item.get("count", 0)),
                fill=item.get("fill", "full"),
                method=item.get("method", "C"),
                weighed_kg=item.get("weighed_kg"),
                logged_at=logged_at,
                device=device,
                lat=lat, lng=lng,
                location_verified=verified,
                note=item.get("note", "")[:200],
            )
            # A location mismatch is a supervisor's problem, raised quietly on
            # the dashboard. The logger is never interrupted about it.
            if lat is not None and area.lat and not verified:
                Flag.objects.create(sacklog=log, kind="location_mismatch")

        accepted.append(str(entry_id))

    if device:
        device.last_seen = timezone.now()
        device.save(update_fields=["last_seen"])

    return JsonResponse({
        "accepted": accepted,
        "duplicates": duplicates,     # already held; safe for the phone to drop
        "rejected": rejected,
        "held": accepted + duplicates,
    })


def today_entries(request):
    """
    Today's entries for the logger's own reference, newest first, with the most
    recent one undoable. Read-only JSON; the screen renders it client-side.
    """
    site = current_site()
    start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    logs = (SackLog.objects
            .filter(source_area__site=site, voided=False, logged_at__gte=start)
            .select_related("stream", "source_area")[:50])
    return JsonResponse({"entries": [{
        "id_uuid": str(l.id_uuid),
        "area": l.source_area.name,
        "stream": l.stream.name,
        "count": l.count,
        "fill": l.get_fill_display(),
        "at": timezone.localtime(l.logged_at).strftime("%H:%M"),
    } for l in logs]})


@csrf_exempt
@require_POST
def undo(request, entry_id):
    """
    Undo, not delete. The row stays, marked voided, and the audit trail keeps
    it. No confirmation dialog - the logger has one thumb and ten seconds.
    """
    log = get_object_or_404(SackLog, pk=entry_id)
    log.voided = True
    log.save(update_fields=["voided"])
    AuditEntry.objects.create(
        actor="device", action="void", target=f"SackLog {entry_id}",
        before_json={"voided": False}, after_json={"voided": True},
    )
    return JsonResponse({"ok": True})


# --------------------------------------------------------------------------
# Purity - the differentiator
# --------------------------------------------------------------------------

@supervisor_required
def purity_check(request):
    """
    The spot-check flow. This is a supervisor's or student assistant's job,
    weekly, on public-facing areas. It is NOT part of the logger's ten seconds
    and is deliberately a separate screen reached from a separate menu.
    """
    site = current_site()
    if request.method == "POST":
        area = get_object_or_404(SourceArea, pk=request.POST["source_area"])
        stream = get_object_or_404(Stream, pk=request.POST["stream"])
        checked = int(request.POST["items_checked"])
        wrong = int(request.POST["items_wrong"])
        # Refuse the impossible rather than storing a purity below zero.
        if wrong > checked:
            return render(request, "purity.html", {
                "site": site,
                "areas": SourceArea.objects.filter(site=site, archived=False),
                "streams": Stream.objects.filter(recoverable=True),
                "error": "Items in the wrong place cannot exceed items checked.",
                "recent": PurityCheck.objects.filter(source_area__site=site)[:20],
            })
        PurityCheck.objects.create(
            source_area=area, stream=stream,
            items_checked=checked, items_wrong=wrong,
            main_contaminant=request.POST.get("main_contaminant", "")[:120],
            checked_at=timezone.now(),
            by_device=device_from_request(request),
        )
        return redirect("purity")

    return render(request, "purity.html", {
        "site": site,
        "areas": SourceArea.objects.filter(site=site, archived=False),
        "streams": Stream.objects.filter(recoverable=True),
        "recent": (PurityCheck.objects.filter(source_area__site=site)
                   .select_related("source_area", "stream")[:20]),
    })


@supervisor_required
def purity_history(request):
    """
    Purity over time per area and stream. Contamination trending up is the
    early warning that a bin has moved, a sign has fallen down, or a new
    tenant has arrived.
    """
    site = current_site()
    checks = (PurityCheck.objects.filter(source_area__site=site)
              .select_related("source_area", "stream")
              .order_by("source_area__name", "stream__name", "checked_at"))
    series = {}
    for c in checks:
        key = f"{c.source_area.name} - {c.stream.name}"
        series.setdefault(key, []).append({
            "at": timezone.localtime(c.checked_at).strftime("%Y-%m-%d"),
            "purity": round(c.purity * 100, 1),
            "n": c.items_checked,
            "contaminant": c.main_contaminant,
        })
    return render(request, "purity_history.html",
                  {"site": site, "series": series,
                   "series_json": json.dumps(series)})


# --------------------------------------------------------------------------
# Calibration
# --------------------------------------------------------------------------

@admin_required
def calibration(request):
    """
    Enter a weighing session: a stream, and the individual sack weights.

    We compute mean, sample standard deviation and n from the samples rather
    than asking anyone to type them, because the sd is the whole point and a
    typed one cannot be audited.
    """
    site = current_site()
    error = None

    if request.method == "POST":
        stream = get_object_or_404(Stream, pk=request.POST["stream"])
        raw = request.POST.get("weights", "")
        # Accept commas, spaces or newlines. People paste from a phone note.
        try:
            weights = [float(x) for x in raw.replace(",", " ").split() if x.strip()]
        except ValueError:
            weights = []

        if len(weights) < 2:
            error = "Enter at least two sack weights - a single weighing has no spread, so it cannot produce an error band."
        else:
            n = len(weights)
            mean = sum(weights) / n
            # Sample standard deviation (n-1 denominator): we are estimating
            # the spread of a population from a sample of it.
            var = sum((w - mean) ** 2 for w in weights) / (n - 1)
            sd = var ** 0.5
            cal = Calibration.objects.create(
                site=site, stream=stream, mean_kg=mean, sd_kg=sd, n=n,
                measured_on=timezone.localdate(),
            )
            CalibrationSample.objects.bulk_create(
                [CalibrationSample(calibration=cal, kg=w) for w in weights]
            )
            return redirect("calibration")

    return render(request, "calibration.html", {
        "site": site,
        "streams": Stream.objects.all(),
        "error": error,
        "current": services.current_calibrations(site).values(),
        "history": Calibration.objects.filter(site=site).select_related("stream")[:20],
    })


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------

@supervisor_required
def dashboard(request):
    """
    Everything obeys the time control at the top.

    The contaminated portion is drawn, not hidden - it is the argument. Areas
    are compared per person / per meal / per room-night, never on raw mass.
    Days with no entries are listed as gaps, never counted as zero.
    """
    site = current_site()
    if site is None:
        return render(request, "empty.html")

    start, end, label, period = parse_period(request)
    summary = services.period_summary(site, start, end)
    areas = services.per_area_breakdown(site, start, end)
    gaps = services.missing_entry_days(site, start, end)

    # Same period last year, not last month. Waste is seasonal: a semester
    # break against a midterm week is a comparison of calendars, not of effort.
    ly_start, ly_end = start - timedelta(days=365), end - timedelta(days=365)
    last_year = services.period_summary(site, ly_start, ly_end)

    # Bars for the stacked stream chart, each split into the clean portion and
    # the contaminated portion that the label claimed but the check refuted.
    bars = []
    for name, row in sorted(summary["streams"].items(),
                            key=lambda kv: -kv[1]["kg"]):
        bars.append({
            "name": name,
            "kg": row["kg"],
            "sigma": row["sigma_kg"],
            "clean": row["true_recoverable_kg"],
            "contaminated": row["kg"] - row["true_recoverable_kg"],
            "purity": row["purity"],
            # Empty string rather than None: non-recoverable streams have no
            # purity source at all, and the template tests these with `in`.
            "purity_source": row["purity_source"] or "",
            "recoverable": row["recoverable"],
            "methods": row["methods"],
        })
    # Bars are drawn to a common scale so streams can be compared by eye.
    max_kg = max([b["kg"] for b in bars], default=1) or 1
    for b in bars:
        # Four states, drawn differently, because collapsing them lies.
        #
        # "kg minus true recoverable" is contamination ONLY for a recoverable
        # stream that has actually been checked. For anything else that same
        # arithmetic gives the full mass, which would paint Residual as 100%
        # contaminated - and Residual is not contaminated, it is correctly
        # sorted waste that simply cannot be diverted. Overstating
        # contamination is exactly the error that gets a report picked apart.
        b["pct_clean"] = b["pct_dirty"] = 0.0
        b["pct_unknown"] = b["pct_nonrec"] = 0.0

        if not b["recoverable"]:
            # Residual, Mixed. Nothing to recover; nothing was mis-sorted.
            b["pct_nonrec"] = 100 * b["kg"] / max_kg
        elif b["purity"] is None:
            # Recoverable, but never checked. Unmeasured is not the same as
            # contaminated, and it is not the same as clean either.
            b["pct_unknown"] = 100 * b["kg"] / max_kg
        else:
            b["pct_clean"] = 100 * b["clean"] / max_kg
            b["pct_dirty"] = 100 * b["contaminated"] / max_kg

    return render(request, "dashboard.html", {
        "site": site,
        "summary": summary,
        "bars": bars,
        "areas": areas,
        "gaps": gaps,
        "label": label,
        "period": period,
        "last_year": last_year,
        "start": start,
        "end": end - timedelta(days=1),
        "diversion_pct": summary["diversion_rate"] * 100,
        "diversion_band": summary["diversion_sigma"] * 100,
        "ly_diversion_pct": last_year["diversion_rate"] * 100,
        "open_flags": Flag.objects.filter(resolved=False,
                                          sacklog__source_area__site=site).count(),
    })


@supervisor_required
def report_pdf(request):
    """
    The defensible document: totals with error bands, method tags, the purity
    that produced the diversion figure, and a limitations section that names
    every stream whose purity was assumed or unmeasured.

    WeasyPrint renders it when installed. When it is not, we return the same
    HTML with a print stylesheet, because a report you cannot produce on the
    demo laptop is not a feature.
    """
    site = current_site()
    start, end, label, period = parse_period(request)
    summary = services.period_summary(site, start, end)
    context = {
        "site": site, "summary": summary, "label": label,
        "start": start, "end": end - timedelta(days=1),
        "areas": services.per_area_breakdown(site, start, end),
        "gaps": services.missing_entry_days(site, start, end),
        "generated": timezone.localtime(),
        "diversion_pct": summary["diversion_rate"] * 100,
        "diversion_band": summary["diversion_sigma"] * 100,
    }
    html = render(request, "report.html", context).content.decode()

    try:
        from weasyprint import HTML
    except ImportError:
        # Graceful fallback, clearly labelled rather than silently different.
        return HttpResponse(html)

    pdf = HTML(string=html, base_url=request.build_absolute_uri()).write_pdf()
    resp = HttpResponse(pdf, content_type="application/pdf")
    resp["Content-Disposition"] = (
        f'inline; filename="sukod-{site.name}-{start:%Y%m%d}.pdf"')
    return resp


# --------------------------------------------------------------------------
# Progressive web app plumbing
# --------------------------------------------------------------------------

def service_worker(request):
    """
    Served from the site root, not /static/, because a service worker can only
    control pages at or below its own path. From /static/sw.js it could only
    control /static/, which is useless.
    """
    from django.template.loader import render_to_string
    return HttpResponse(render_to_string("sw.js"),
                        content_type="application/javascript")


def offline(request):
    """Fallback page the service worker serves when a navigation fails."""
    return render(request, "offline.html")


def scan(request, token):
    """
    QR landing: /a/<token> opens the notebook already set to that area.

    The sign on the wall by the sacks is the fastest possible area picker.
    """
    area = get_object_or_404(SourceArea, qr_token=token, archived=False)
    return redirect(f"/?area={area.id}")


# --------------------------------------------------------------------------
# The back room - admin only
#
# This is the "initial" screen Denver asked for: the place where the other two
# roles are brought into existence. A supervisor gets an account; a janitor
# gets a pairing code. Nobody self-registers.
# --------------------------------------------------------------------------

# Characters used in a pairing code. 0/O and 1/I/L are left out on purpose:
# the code gets read off a screen and typed into a phone by someone standing
# next to a bin, and "did you mean zero or oh" is a support call we can design
# away for free.
PAIR_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def new_pair_code(length=6):
    """A short, unambiguous, unused pairing code."""
    import secrets
    for _ in range(50):
        code = "".join(secrets.choice(PAIR_ALPHABET) for _ in range(length))
        if not Device.objects.filter(pair_code=code).exists():
            return code
    raise RuntimeError("could not find a free pairing code")


@admin_required
def manage_home(request):
    """
    Admin landing. Counts rather than charts: this screen exists to answer
    "is the system set up?", not "how are we doing?" - that is the monitor's
    question and it has its own interface.
    """
    site = current_site()
    return render(request, "manage/home.html", {
        "site": site,
        "staff_count": Staff.objects.filter(site=site).count(),
        "device_count": Device.objects.filter(site=site).count(),
        "unpaired": Device.objects.filter(site=site, paired_at__isnull=True).count(),
        "area_count": SourceArea.objects.filter(site=site, archived=False).count(),
        # A stream with no calibration silently contributes zero mass, so the
        # admin is told about it here rather than discovering it on a report.
        "uncalibrated": [
            s.name for s in Stream.objects.all()
            if not Calibration.objects.filter(site=site, stream=s).exists()
        ],
    })


@admin_required
def manage_staff(request):
    """
    Create and list supervisor accounts.

    Passwords are never stored or echoed by us - Django hashes them on
    set_password and there is no way to read one back. If a supervisor forgets
    theirs the admin sets a new one; that is the intended flow.
    """
    from django.contrib.auth.models import User

    site = current_site()
    error = None

    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        role = request.POST.get("role", "supervisor")
        full_name = request.POST.get("full_name", "").strip()

        if not username or not password:
            error = "A username and a password are both required."
        elif len(password) < 8:
            # Short enough to be memorable is short enough to be guessed.
            error = "Use at least 8 characters for the password."
        elif User.objects.filter(username__iexact=username).exists():
            error = f"There is already an account called '{username}'."
        elif role not in {"supervisor", "admin"}:
            error = "Unknown role."
        else:
            user = User.objects.create_user(
                username=username, password=password, first_name=full_name)
            Staff.objects.create(
                user=user, site=site, role=role,
                created_by=request.user.get_username())
            # Who granted access, and when. The audit trail is the point.
            AuditEntry.objects.create(
                actor=request.user.get_username(),
                action="create_staff",
                target=f"user:{username}",
                after_json={"role": role, "full_name": full_name})
            return redirect("manage_staff")

    return render(request, "manage/staff.html", {
        "site": site,
        "error": error,
        "staff": Staff.objects.filter(site=site).select_related("user"),
    })


@admin_required
def manage_devices(request):
    """
    Create a janitor's device and show its pairing code.

    A Device is a phone, not a person. It is labelled by where it lives
    ("Canteen staging point"), never by who carries it - see the note at the
    top of models.py for why that distinction is load-bearing.
    """
    site = current_site()
    error = None
    new_device = None

    if request.method == "POST":
        action = request.POST.get("action", "create")

        if action == "create":
            label = request.POST.get("label", "").strip()
            if not label:
                error = "Give the device a label naming where it is used."
            else:
                new_device = Device.objects.create(
                    site=site, label=label, role="logger",
                    pair_code=new_pair_code())
                AuditEntry.objects.create(
                    actor=request.user.get_username(), action="create_device",
                    target=f"device:{new_device.id}",
                    after_json={"label": label})

        elif action == "reset":
            # Unpair: issue a new code and forget the old token, so a lost or
            # replaced phone can no longer post entries.
            device = get_object_or_404(
                Device, pk=request.POST.get("device_id"), site=site)
            before = {"pair_code": device.pair_code}
            device.pair_code = new_pair_code()
            device.token = Device._meta.get_field("token").get_default()
            device.paired_at = None
            device.save(update_fields=["pair_code", "token", "paired_at"])
            AuditEntry.objects.create(
                actor=request.user.get_username(), action="reset_device",
                target=f"device:{device.id}", before_json=before,
                after_json={"pair_code": device.pair_code})
            new_device = device

    return render(request, "manage/devices.html", {
        "site": site,
        "error": error,
        "new_device": new_device,
        "devices": Device.objects.filter(site=site).order_by("label"),
    })


def after_login(request):
    """
    Where a signed-in person goes when they have not asked for anywhere
    specific. Not a screen - it renders nothing and always redirects.

    The two roles start in different places because they have different jobs:
    a supervisor opens Sukod to look at numbers, an admin opens it to set
    something up. Anyone with no Staff badge is not one of ours, so they go
    to the notebook like any other visitor.
    """
    staff = staff_for(request)
    if staff is None:
        return redirect("logger")
    return redirect("manage_home" if staff.is_admin else "dashboard")

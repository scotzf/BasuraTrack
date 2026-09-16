"""
The bridge between the database and core/calc.py.

calc.py knows nothing about Django on purpose. This module is the only place
that turns rows into the plain dicts calc.py eats, and turns PurityCheck rows
into the lookup closure it expects.
"""
from collections import defaultdict
from datetime import timedelta

from django.utils import timezone

from . import calc
from .models import (
    Calibration, DeclaredPurity, PurityCheck, SackLog, SourceArea, Stream,
)


def current_calibrations(site):
    """
    Latest calibration per stream for a site, as {stream_id: Calibration}.

    Calibration is ordered newest-first in Meta, so the first row we see for a
    stream is the one in force.
    """
    out = {}
    for cal in Calibration.objects.filter(site=site).select_related("stream"):
        out.setdefault(cal.stream_id, cal)
    return out


def entries_for(site, start, end, area=None):
    """
    Pull SackLogs in a window and flatten them into calc.py's input dicts.

    Voided entries (undone by the logger) are excluded. An entry whose stream
    has no calibration still appears, with n=0 - calc.entry_sigma_kg returns a
    zero sigma for it and `uncalibrated_streams` below names it, so the gap is
    reported rather than hidden.
    """
    cals = current_calibrations(site)

    qs = (SackLog.objects
          .filter(source_area__site=site, voided=False,
                  logged_at__gte=start, logged_at__lt=end)
          .select_related("stream", "source_area"))
    if area is not None:
        qs = qs.filter(source_area=area)

    entries = []
    uncalibrated = set()
    for log in qs:
        cal = cals.get(log.stream_id)
        if cal is None and log.method != "W":
            uncalibrated.add(log.stream.name)
        entries.append({
            "area_id": log.source_area_id,
            "area_name": log.source_area.name,
            "stream": log.stream.name,
            "count": log.count,
            "fill": log.fill,
            "method": log.method,
            "weighed_kg": log.weighed_kg,
            "mean_kg": cal.mean_kg if cal else 0.0,
            "sd_kg": cal.sd_kg if cal else 0.0,
            "n": cal.n if cal else 0,
        })
    return entries, sorted(uncalibrated)


def purity_lookup_for(site, as_of=None, window_days=90):
    """
    Build the (area_id, stream_name) -> (value, source) closure.

    Only checks from the last `window_days` count. A purity check from two
    years ago describes a bin that no longer exists, and pretending otherwise
    would be the same error as assuming 1.0, just slower.
    """
    as_of = as_of or timezone.now()
    cutoff = as_of - timedelta(days=window_days)

    checks = (PurityCheck.objects
              .filter(source_area__site=site, checked_at__gte=cutoff,
                      checked_at__lte=as_of, items_checked__gt=0)
              .select_related("stream", "source_area"))

    # Per (area, stream): pool the items across checks rather than averaging
    # the ratios, so a 200-item check outweighs a 10-item one.
    pooled = defaultdict(lambda: [0, 0])          # key -> [checked, wrong]
    site_pooled = defaultdict(lambda: [0, 0])     # stream -> [checked, wrong]
    for c in checks:
        key = (c.source_area_id, c.stream.name)
        pooled[key][0] += c.items_checked
        pooled[key][1] += c.items_wrong
        site_pooled[c.stream.name][0] += c.items_checked
        site_pooled[c.stream.name][1] += c.items_wrong

    area_checks = {
        k: calc.purity_from_check(v[0], v[1]) for k, v in pooled.items()
    }
    site_checks = {
        k: calc.purity_from_check(v[0], v[1]) for k, v in site_pooled.items()
    }

    declared = {
        (d.source_area_id, d.stream.name): d.value
        for d in DeclaredPurity.objects.filter(source_area__site=site)
                                       .select_related("stream", "source_area")
        # A public-facing bin can never carry a declared purity. Belt and
        # braces: the model refuses to save one, and we refuse to use one.
        if not d.source_area.public_facing
    }

    def lookup(area_id, stream_name):
        return calc.resolve_purity(
            area_id, stream_name, area_checks, site_checks, declared
        )

    return lookup


def recoverable_stream_names():
    """Streams flagged recoverable in the database, as a set of names."""
    return set(Stream.objects.filter(recoverable=True)
                             .values_list("name", flat=True))


def period_summary(site, start, end, area=None):
    """
    The one call a dashboard or report makes. Returns calc.summarise's dict
    plus the extra context screens need: the window, and any stream that had
    entries but no calibration behind them.
    """
    entries, uncalibrated = entries_for(site, start, end, area=area)
    lookup = purity_lookup_for(site, as_of=end)
    summary = calc.summarise(entries, lookup, recoverable_stream_names())

    if uncalibrated:
        summary["limitations"].append(
            "No calibration on file for: " + ", ".join(uncalibrated)
            + ". Their mass is shown as zero and excluded from all totals."
        )
    summary["start"] = start
    summary["end"] = end
    summary["entry_count"] = len(entries)
    summary["entries"] = entries
    return summary


def per_area_breakdown(site, start, end):
    """
    Normalised comparison across source areas.

    Always per-denominator, never raw, for the reason stated in calc.normalise.
    """
    rows = []
    lookup = purity_lookup_for(site, as_of=end)
    recoverable = recoverable_stream_names()

    for area in SourceArea.objects.filter(site=site, archived=False):
        entries, _ = entries_for(site, start, end, area=area)
        if not entries:
            # Missing entries are a GAP, not a zero. Say so.
            rows.append({"area": area, "gap": True})
            continue
        s = calc.summarise(entries, lookup, recoverable)
        days = max((end - start).days, 1)
        rows.append({
            "area": area,
            "gap": False,
            "summary": s,
            "per_unit": calc.normalise(
                s["total_kg"], area.denominator_value * days
            ),
            "unit": area.get_denominator_type_display(),
        })
    # Worst first, but only among areas that actually have data.
    rows.sort(key=lambda r: (r["gap"], -(r.get("per_unit") or 0)))
    return rows


def missing_entry_days(site, start, end):
    """
    Days in the window with no entries at all. Shown as gaps on the dashboard
    so nobody reads a quiet week as a clean week.
    """
    logged = set(
        SackLog.objects.filter(source_area__site=site, voided=False,
                               logged_at__gte=start, logged_at__lt=end)
        .values_list("logged_at__date", flat=True)
    )
    days = []
    d = start.date()
    while d < end.date():
        if d not in logged:
            days.append(d)
        d += timedelta(days=1)
    return days

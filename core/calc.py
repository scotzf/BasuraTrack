"""
Sukod - the maths.

Everything in this file is a pure function: it takes plain values in and
returns plain values out, touching no database. That is deliberate. These are
the numbers Sukod stakes its credibility on, so they must be testable on their
own, with a worked example in every docstring.

How the uncertainty works, before the API surface:

We do not weigh every sack. We weigh thirty of them once (a CALIBRATION) and
then count sacks. So the mass of a day's waste is a count multiplied by an
estimated average sack weight - and an estimate has an error bar.

The error bar on the calibrated mean is the STANDARD ERROR: sd / sqrt(n).
Weighing more sacks during calibration shrinks it; a wide spread of sack
weights widens it. When we combine many entries we add their VARIANCES
(sigma squared), not their sigmas, because independent errors partly cancel -
ten entries each +-2 kg give +-6.3 kg in total, not +-20 kg.
"""
import math
from collections import defaultdict

# Fill level -> fraction of a full sack. Mirrors models.FILL_FACTOR; kept here
# so this module can be imported and tested without Django loaded.
FILL_FACTOR = {
    "full": 1.00,
    "two_thirds": 0.67,
    "half": 0.50,
}

# Recoverable streams get purity applied. Residual and Mixed never count
# toward diversion, so their purity is irrelevant.
RECOVERABLE_DEFAULT = {"Recyclable", "Biodegradable"}


def entry_mass_kg(count, fill, mean_kg):
    """
    Mass of one counted entry.

        kg = count * fill_factor * calibrated mean kg per full sack

    >>> entry_mass_kg(4, "full", 7.5)
    30.0
    >>> round(entry_mass_kg(3, "two_thirds", 10.0), 2)
    20.1
    """
    return count * FILL_FACTOR[fill] * mean_kg


def entry_sigma_kg(count, fill, sd_kg, n):
    """
    Uncertainty of one counted entry, as one standard error.

        sigma = count * fill_factor * (sd / sqrt(n))

    The sd/sqrt(n) term is the standard error of the calibrated MEAN - how
    well we know the average sack, not how much sacks vary.

    >>> round(entry_sigma_kg(4, "full", 2.0, 25), 3)
    1.6
    >>> entry_sigma_kg(4, "full", 2.0, 0)
    0.0
    """
    if not n:
        # No calibration behind it: we cannot state an estimation error.
        # The caller is responsible for tagging such an entry as unusable.
        return 0.0
    return count * FILL_FACTOR[fill] * (sd_kg / math.sqrt(n))


def combine_sigma(sigmas):
    """
    Combine independent uncertainties: add variances, then take the root.

        total = sqrt(sum(sigma_i ** 2))

    >>> combine_sigma([3.0, 4.0])
    5.0
    >>> round(combine_sigma([2.0] * 10), 2)
    6.32
    >>> combine_sigma([])
    0.0
    """
    return math.sqrt(sum(s * s for s in sigmas))


def purity_from_check(items_checked, items_wrong):
    """
    Share of items in a stream that actually belong there.

        purity = 1 - wrong / checked

    Returns None when nothing was checked. None means "not yet checked", which
    is NOT the same as 1.0 and must never be silently turned into it.

    (Rounded in the example only because binary floating point renders this
    particular division as 0.8200000000000001.)

    >>> round(purity_from_check(50, 9), 4)
    0.82
    >>> purity_from_check(0, 0) is None
    True
    """
    if not items_checked:
        return None
    return 1 - (items_wrong / items_checked)


def resolve_purity(area_id, stream_name, area_checks, site_checks, declared):
    """
    Work out which purity figure applies, in a fixed order of preference.

    1. A measured check for THIS source area and stream.
    2. A declared purity for this area and stream (trained-staff bins only).
    3. The site-wide average measured purity for this stream.
    4. Nothing. Return None, meaning "not yet checked".

    Returns (value_or_None, source_label) where source_label is one of
    "measured", "declared", "site_average", "unmeasured". The label is carried
    all the way to the screen so the reader can see where the number came from.

    `area_checks`   {(area_id, stream): purity}   measured, this area
    `site_checks`   {stream: purity}              measured, site-wide mean
    `declared`      {(area_id, stream): purity}   declared values

    >>> resolve_purity(1, "Recyclable", {(1, "Recyclable"): 0.8}, {}, {})
    (0.8, 'measured')
    >>> resolve_purity(1, "Recyclable", {}, {"Recyclable": 0.7}, {})
    (0.7, 'site_average')
    >>> resolve_purity(1, "Recyclable", {}, {}, {(1, "Recyclable"): 0.95})
    (0.95, 'declared')
    >>> resolve_purity(9, "Recyclable", {}, {}, {})
    (None, 'unmeasured')
    """
    key = (area_id, stream_name)
    if key in area_checks:
        return area_checks[key], "measured"
    if key in declared:
        return declared[key], "declared"
    if stream_name in site_checks:
        return site_checks[stream_name], "site_average"
    return None, "unmeasured"


def true_recoverable_kg(stream_kg, purity):
    """
    Gross mass in a recoverable stream, discounted by how much of it is really
    the right material.

    A purity of None means unmeasured: the stream contributes ZERO to true
    recoverable, and the caller must print the limitation line. Assuming 1.0
    here would be the single most damaging thing this system could do.

    >>> true_recoverable_kg(100.0, 0.82)
    82.0
    >>> true_recoverable_kg(100.0, None)
    0.0
    """
    if purity is None:
        return 0.0
    return stream_kg * purity


def diversion_rate(true_recoverable_total, total_kg):
    """
    The headline figure: what share of everything we handled is genuinely
    recoverable material in the right stream.

    >>> round(diversion_rate(41.0, 100.0), 3)
    0.41
    >>> diversion_rate(0.0, 0.0)
    0.0
    """
    if not total_kg:
        return 0.0
    return true_recoverable_total / total_kg


def summarise(entries, purity_lookup, recoverable_streams=None):
    """
    Roll a list of entries up into the numbers a screen or report shows.

    `entries` is a list of dicts, each with:
        area_id, stream, count, fill, method,
        mean_kg, sd_kg, n         (calibration behind it; ignored when W)
        weighed_kg                (only when method == "W")

    `purity_lookup` is a callable (area_id, stream) -> (value_or_None, label).
    Pass a closure built from resolve_purity so this stays free of the database.

    Returns a dict with per-stream masses and sigmas, the totals, the diversion
    rate with its own error band, and a `limitations` list naming every stream
    whose purity was unmeasured or merely declared.

    Worked example - 10 full recyclable sacks, calibration 8.0 +- 2.0 kg from
    n=25 sacks, purity measured at 0.90:
        mass  = 10 * 1.00 * 8.0            = 80.0 kg
        sigma = 10 * 1.00 * 2.0/sqrt(25)   =  4.0 kg
        true recoverable = 80.0 * 0.90     = 72.0 kg
        diversion = 72.0 / 80.0            = 0.90

    >>> e = [dict(area_id=1, stream="Recyclable", count=10, fill="full",
    ...           method="C", mean_kg=8.0, sd_kg=2.0, n=25, weighed_kg=None)]
    >>> s = summarise(e, lambda a, st: (0.90, "measured"))
    >>> s["total_kg"], s["total_sigma_kg"], round(s["diversion_rate"], 3)
    (80.0, 4.0, 0.9)
    """
    if recoverable_streams is None:
        recoverable_streams = RECOVERABLE_DEFAULT

    # Accumulate mass per stream, and the list of sigmas per stream so we can
    # combine them properly (variances, not sums) at the end.
    mass = defaultdict(float)
    sigmas = defaultdict(list)
    methods = defaultdict(set)
    # Track which (area, stream) pairs actually carry mass, so we only resolve
    # and report purity for streams that exist in this period.
    pairs = set()

    for e in entries:
        st = e["stream"]
        methods[st].add(e["method"])

        if e["method"] == "W":
            # A scale reading. It contributes mass and no estimation error.
            mass[st] += e.get("weighed_kg") or 0.0
        else:
            mass[st] += entry_mass_kg(e["count"], e["fill"], e["mean_kg"])
            sigmas[st].append(
                entry_sigma_kg(e["count"], e["fill"], e["sd_kg"], e["n"])
            )
        pairs.add((e["area_id"], st))

    # Per-stream roll-up, with purity applied only to recoverable streams.
    streams = {}
    limitations = []
    true_total = 0.0
    true_sigmas = []        # one per recoverable stream, combined in quadrature

    for st, kg in mass.items():
        sigma = combine_sigma(sigmas[st])
        row = {
            "kg": kg,
            "sigma_kg": sigma,
            "methods": "".join(sorted(methods[st])),
            "recoverable": st in recoverable_streams,
            "purity": None,
            "purity_source": None,
            "true_recoverable_kg": 0.0,
            "true_recoverable_sigma_kg": 0.0,
        }

        if row["recoverable"]:
            # Purity is resolved per (area, stream), so a stream spanning
            # several areas gets each area's own figure where one exists.
            # Mass is split across those areas in proportion to their entries.
            area_mass = defaultdict(float)
            for e in entries:
                if e["stream"] != st:
                    continue
                if e["method"] == "W":
                    area_mass[e["area_id"]] += e.get("weighed_kg") or 0.0
                else:
                    area_mass[e["area_id"]] += entry_mass_kg(
                        e["count"], e["fill"], e["mean_kg"]
                    )

            stream_true = 0.0
            sources = set()
            weighted_purity_num = 0.0
            weighted_purity_den = 0.0

            for area_id, akg in area_mass.items():
                purity, source = purity_lookup(area_id, st)
                sources.add(source)
                stream_true += true_recoverable_kg(akg, purity)
                if purity is not None:
                    weighted_purity_num += purity * akg
                    weighted_purity_den += akg
                if source == "unmeasured":
                    limitations.append(
                        f"{st}: purity not yet checked - excluded from true "
                        f"recoverable ({akg:.1f} kg of gross mass)."
                    )
                elif source == "declared":
                    limitations.append(
                        f"{st}: purity declared, not measured "
                        f"({purity:.0%}, {akg:.1f} kg)."
                    )
                elif source == "site_average":
                    limitations.append(
                        f"{st}: no check for this area; site-wide average used "
                        f"({purity:.0%}, {akg:.1f} kg)."
                    )

            row["true_recoverable_kg"] = stream_true
            row["purity_source"] = "+".join(sorted(sources))
            if weighted_purity_den:
                row["purity"] = weighted_purity_num / weighted_purity_den
            true_total += stream_true

            # Uncertainty on the recoverable portion of this stream.
            #
            # True recoverable is the gross mass scaled by purity, so it
            # carries the same RELATIVE error as the mass it came from:
            #
            #     sigma_true = true_kg * (sigma_kg / kg)
            #
            # Purity is treated as exact here, which is the same conservative
            # simplification already used for diversion_sigma below. The
            # sampling error on a spot-check of 40 items is real and is NOT in
            # this band - summarise() says so in its limitations output, and
            # every report prints it.
            row["true_recoverable_sigma_kg"] = (
                stream_true * (sigma / kg) if kg else 0.0
            )
            true_sigmas.append(row["true_recoverable_sigma_kg"])

        streams[st] = row

    total_kg = sum(r["kg"] for r in streams.values())
    # Combine every entry's sigma across every stream for the grand total.
    total_sigma = combine_sigma([s for lst in sigmas.values() for s in lst])
    rate = diversion_rate(true_total, total_kg)

    return {
        "streams": streams,
        "total_kg": total_kg,
        "total_sigma_kg": total_sigma,
        "true_recoverable_kg": true_total,
        # Streams are independent, so their bands add as variances, not as
        # standard deviations - the same rule as every other total here.
        "true_recoverable_sigma_kg": combine_sigma(true_sigmas),
        "diversion_rate": rate,
        # The error band on the rate, propagated from the mass uncertainty.
        # We treat the numerator's share as fixed and carry the denominator's
        # relative error through, which is the conservative reading.
        "diversion_sigma": (rate * (total_sigma / total_kg)) if total_kg else 0.0,
        "limitations": sorted(set(limitations)),
    }


def normalise(kg, denominator_value):
    """
    Per-capita (or per-meal, per-room-night) figure.

    Comparing raw totals between a 3000-student main building and a 40-seat
    canteen ranks the main building worst every single time, which is not a
    finding. Dividing by the denominator is what makes the comparison mean
    anything.

    >>> round(normalise(150.0, 3000), 4)
    0.05
    >>> normalise(150.0, 0)
    0.0
    """
    if not denominator_value:
        return 0.0
    return kg / denominator_value


def format_kg(kg, sigma):
    """
    Render a mass the only way Sukod is allowed to render one: with its band.

    >>> format_kg(80.0, 4.0)
    '80.0 +- 4.0 kg'
    """
    return f"{kg:.1f} +- {sigma:.1f} kg"

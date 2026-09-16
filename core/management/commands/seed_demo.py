"""
Seed a realistic demo site: streams, areas, calibrations, three weeks of sack
logs, and purity checks on SOME areas but deliberately not all.

The gaps are the point. A demo where every stream is measured hides the exact
behaviour Sukod is built to make visible: what the system does when it does not
know. One area is left unchecked so the dashboard shows "NOT CHECKED" and the
report prints its limitation line, and one staff-only area carries a declared
purity so the "DECLARED" marker has something to mark.

    python manage.py seed_demo
"""
import random
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import (
    Calibration, CalibrationSample, DeclaredPurity, Device, PurityCheck,
    SackLog, Site, SourceArea, Stream,
)

STREAMS = [
    # name, recoverable, colour, sort order
    ("Biodegradable", True,  "#4d7c0f", 1),
    ("Recyclable",    True,  "#0369a1", 2),
    ("Residual",      False, "#57534e", 3),
    ("Mixed",         False, "#7c3aed", 4),
]

AREAS = [
    # name, denominator type, value, public facing
    ("Canteen",             "meals",   900,  True),
    ("Main Building",       "persons", 2400, True),
    ("CS Building",         "persons", 800,  True),
    ("Library",             "persons", 450,  True),
    ("Kitchen (staff only)", "meals",  900,  False),
]

# Mean and spread of a full sack, per stream, used to fabricate calibration
# samples. Roughly what a 60-litre sack of each actually weighs.
SACK_KG = {
    "Biodegradable": (11.5, 2.4),
    "Recyclable":    (4.2, 1.3),
    "Residual":      (7.0, 1.8),
    "Mixed":         (8.5, 2.6),
}


class Command(BaseCommand):
    help = "Create a demo site with calibrations, entries and purity checks."

    def handle(self, *args, **options):
        random.seed(7)   # reproducible demo; the same numbers every run

        site, _ = Site.objects.get_or_create(
            name="Bataan Peninsula State University - Main",
            defaults={"type": "campus",
                      # Junkshop buy-back rates, pesos per kg.
                      "junkshop_rates_json": {"Recyclable": 6.5,
                                              "Biodegradable": 0.0}},
        )

        streams = {}
        for name, recoverable, colour, order in STREAMS:
            s, _ = Stream.objects.get_or_create(
                name=name,
                defaults={"recoverable": recoverable, "colour": colour,
                          "sort_order": order},
            )
            streams[name] = s

        areas = {}
        for name, dtype, dval, public in AREAS:
            a, _ = SourceArea.objects.get_or_create(
                site=site, name=name,
                defaults={"denominator_type": dtype, "denominator_value": dval,
                          "public_facing": public,
                          "lat": 14.6760, "lng": 120.5400},
            )
            areas[name] = a

        # --- calibrations: 30 weighed sacks per stream ---------------------
        for name, (mean, sd) in SACK_KG.items():
            if Calibration.objects.filter(site=site, stream=streams[name]).exists():
                continue
            weights = [max(0.5, random.gauss(mean, sd)) for _ in range(30)]
            n = len(weights)
            m = sum(weights) / n
            var = sum((w - m) ** 2 for w in weights) / (n - 1)
            cal = Calibration.objects.create(
                site=site, stream=streams[name], mean_kg=m, sd_kg=var ** 0.5,
                n=n, measured_on=timezone.localdate() - timedelta(days=40),
            )
            CalibrationSample.objects.bulk_create(
                [CalibrationSample(calibration=cal, kg=w) for w in weights])

        # --- a paired logger device ---------------------------------------
        device, _ = Device.objects.get_or_create(
            site=site, label="Staging point phone",
            defaults={"role": "logger", "pair_code": "SUKOD1",
                      "paired_at": timezone.now()},
        )

        # --- three weeks of entries ---------------------------------------
        # Sunday is skipped, which leaves genuine gaps in the record. The
        # dashboard must show those as gaps rather than as zero-waste days.
        if not SackLog.objects.filter(source_area__site=site).exists():
            now = timezone.now()
            logs = []
            for day_offset in range(21, 0, -1):
                day = now - timedelta(days=day_offset)
                if day.weekday() == 6:
                    continue
                for area in areas.values():
                    for sname, stream in streams.items():
                        if sname == "Mixed" and random.random() > 0.25:
                            continue    # mixed sacks are the exception
                        count = random.randint(1, 6)
                        if area.name == "Main Building":
                            count += 3   # biggest building, biggest pile
                        logs.append(SackLog(
                            source_area=area, stream=stream, count=count,
                            fill=random.choice(["full", "full", "two_thirds", "half"]),
                            method="C",
                            logged_at=day.replace(hour=16, minute=random.randint(0, 50)),
                            device=device, location_verified=True,
                            lat=area.lat, lng=area.lng,
                        ))
            SackLog.objects.bulk_create(logs)

        # --- purity checks: deliberately incomplete -------------------------
        if not PurityCheck.objects.filter(source_area__site=site).exists():
            checks = [
                # area, stream, items checked, items wrong
                ("Canteen",       "Recyclable",    60, 19),
                ("Canteen",       "Biodegradable", 55, 8),
                ("Main Building", "Recyclable",    80, 34),
                ("Main Building", "Biodegradable", 70, 21),
                ("CS Building",   "Recyclable",    45, 11),
                # Library: never checked. This is on purpose. Its recyclables
                # contribute ZERO to true recoverable and the report says why.
            ]
            for weeks_ago in (3, 2, 1):
                for area_name, stream_name, checked, wrong in checks:
                    # Contamination drifts a little week to week.
                    w = max(0, min(checked, wrong + random.randint(-4, 4)))
                    PurityCheck.objects.create(
                        source_area=areas[area_name], stream=streams[stream_name],
                        items_checked=checked, items_wrong=w,
                        main_contaminant="food-soiled paper" if stream_name == "Recyclable" else "plastic film",
                        checked_at=timezone.now() - timedelta(weeks=weeks_ago),
                        by_device=device,
                    )

        # --- one declared purity, on a staff-only bin ----------------------
        # Trained kitchen staff are the only people who can fill this one, so a
        # declaration is defensible. It still renders with a DECLARED marker
        # and is still named in the limitations section.
        DeclaredPurity.objects.get_or_create(
            source_area=areas["Kitchen (staff only)"], stream=streams["Recyclable"],
            defaults={"value": 0.97,
                      "reason": "Cardboard bin behind the service line; filled only by trained kitchen staff.",
                      "declared_by": "Food Services Manager",
                      "declared_on": timezone.localdate()},
        )

        # --- the two accounts, so all three roles are testable at once ----
        # Demo passwords, printed below on purpose. This command exists to make
        # a machine demonstrable in one step; it is never run on a real site.
        from django.contrib.auth.models import User
        from core.models import Staff

        accounts = [
            ("admin", "sukod-admin-2026", "admin", "Demo Admin"),
            ("supervisor", "sukod-super-2026", "supervisor", "Demo Supervisor"),
        ]
        for username, password, role, full_name in accounts:
            user, made = User.objects.get_or_create(
                username=username, defaults={"first_name": full_name})
            if made:
                user.set_password(password)
                # The admin account also opens /django-admin/, which is still
                # the Phase 4 surface for entries, flags and site settings.
                if role == "admin":
                    user.is_staff = True
                    user.is_superuser = True
                user.save()
            Staff.objects.get_or_create(
                user=user, defaults={"site": site, "role": role,
                                     "created_by": "seed_demo"})

        self.stdout.write(self.style.SUCCESS(
            f"Seeded {site.name}: {SackLog.objects.count()} entries, "
            f"{PurityCheck.objects.count()} purity checks. "
            f"Pairing code for the logger device: {device.pair_code}"
        ))
        self.stdout.write(
            "Library has no purity check on purpose - the dashboard should "
            "show NOT CHECKED and exclude it from true recoverable."
        )
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Three ways in:"))
        self.stdout.write(
            "  janitor      /            pair with code " + device.pair_code)
        self.stdout.write(
            "  supervisor   /monitor/     supervisor / sukod-super-2026")
        self.stdout.write(
            "  admin        /manage/      admin / sukod-admin-2026")

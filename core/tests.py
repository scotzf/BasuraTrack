"""
Tests for the figures Sukod stakes its credibility on.

Priorities, in order:
  1. Purity is never silently assumed to be 1.0. This is the failure that
     would most damage credibility, so it is tested from several directions.
  2. Uncertainty combines by variance, not by sum.
  3. A declared purity is marked distinctly and appears in the limitations.
  4. Offline sync is idempotent - a replayed queue never double-counts.
"""
import json
import uuid
from datetime import timedelta

from django.test import TestCase, Client
from django.utils import timezone

from . import calc
from .models import (
    AuditEntry, Calibration, DeclaredPurity, Device, PurityCheck, SackLog,
    Site, SourceArea, Staff, Stream,
)
from . import services


# ==========================================================================
# Pure maths - no database
# ==========================================================================

class MassAndUncertaintyTests(TestCase):

    def test_mass_of_one_entry(self):
        # 4 full sacks at a calibrated 7.5 kg per sack.
        self.assertEqual(calc.entry_mass_kg(4, "full", 7.5), 30.0)

    def test_fill_level_scales_mass(self):
        # A two-thirds sack weighs two-thirds of a full one.
        self.assertAlmostEqual(calc.entry_mass_kg(3, "two_thirds", 10.0), 20.1)
        self.assertAlmostEqual(calc.entry_mass_kg(3, "half", 10.0), 15.0)

    def test_sigma_uses_standard_error_not_sd(self):
        # sd is 2.0 across n=25 sacks, so the error on the MEAN is 2/5 = 0.4.
        # Four sacks therefore carry 4 * 0.4 = 1.6 kg of uncertainty, not 8.
        self.assertAlmostEqual(calc.entry_sigma_kg(4, "full", 2.0, 25), 1.6)

    def test_more_calibration_sacks_narrows_the_band(self):
        loose = calc.entry_sigma_kg(10, "full", 2.0, 4)
        tight = calc.entry_sigma_kg(10, "full", 2.0, 100)
        self.assertLess(tight, loose)

    def test_sigma_is_zero_without_calibration(self):
        # No calibration means no estimation error can be stated. The caller
        # is responsible for reporting the gap; calc must not invent a band.
        self.assertEqual(calc.entry_sigma_kg(5, "full", 2.0, 0), 0.0)

    def test_uncertainties_combine_by_variance(self):
        # 3 and 4 combine to 5, not to 7. Independent errors partly cancel.
        self.assertAlmostEqual(calc.combine_sigma([3.0, 4.0]), 5.0)
        # Ten entries at 2.0 each give 6.32, not 20.
        self.assertAlmostEqual(calc.combine_sigma([2.0] * 10), 6.3245, places=3)

    def test_combining_nothing_is_zero(self):
        self.assertEqual(calc.combine_sigma([]), 0.0)


class PurityTests(TestCase):

    def test_purity_from_a_check(self):
        self.assertAlmostEqual(calc.purity_from_check(50, 9), 0.82)

    def test_zero_items_checked_is_not_a_measurement(self):
        # Must be None, never 1.0. Nothing was looked at.
        self.assertIsNone(calc.purity_from_check(0, 0))

    # --- the fallback order, tested explicitly -----------------------------

    def test_fallback_prefers_this_areas_own_measurement(self):
        v, src = calc.resolve_purity(
            1, "Recyclable",
            area_checks={(1, "Recyclable"): 0.80},
            site_checks={"Recyclable": 0.65},
            declared={(1, "Recyclable"): 0.99},
        )
        self.assertEqual((v, src), (0.80, "measured"))

    def test_fallback_uses_declared_before_site_average(self):
        # A declaration is about THIS bin; a site average is about other bins.
        v, src = calc.resolve_purity(
            1, "Recyclable", {}, {"Recyclable": 0.65}, {(1, "Recyclable"): 0.97})
        self.assertEqual((v, src), (0.97, "declared"))

    def test_fallback_uses_site_average_when_area_unchecked(self):
        v, src = calc.resolve_purity(1, "Recyclable", {}, {"Recyclable": 0.65}, {})
        self.assertEqual((v, src), (0.65, "site_average"))

    def test_unchecked_returns_none_not_one(self):
        # THE test. Unmeasured is not clean.
        v, src = calc.resolve_purity(1, "Recyclable", {}, {}, {})
        self.assertIsNone(v)
        self.assertNotEqual(v, 1.0)
        self.assertEqual(src, "unmeasured")

    def test_unmeasured_purity_contributes_nothing_to_true_recoverable(self):
        self.assertEqual(calc.true_recoverable_kg(100.0, None), 0.0)
        self.assertEqual(calc.true_recoverable_kg(100.0, 0.82), 82.0)

    def test_diversion_rate(self):
        self.assertAlmostEqual(calc.diversion_rate(41.0, 100.0), 0.41)
        # No mass handled is a zero rate, not a division error.
        self.assertEqual(calc.diversion_rate(0.0, 0.0), 0.0)


class SummariseTests(TestCase):
    """The worked example from the docstring, end to end."""

    def entry(self, **kw):
        base = dict(area_id=1, stream="Recyclable", count=10, fill="full",
                    method="C", mean_kg=8.0, sd_kg=2.0, n=25, weighed_kg=None)
        base.update(kw)
        return base

    def test_worked_example(self):
        s = calc.summarise([self.entry()], lambda a, st: (0.90, "measured"))
        self.assertAlmostEqual(s["total_kg"], 80.0)          # 10 * 1.00 * 8.0
        self.assertAlmostEqual(s["total_sigma_kg"], 4.0)     # 10 * 2.0/sqrt(25)
        self.assertAlmostEqual(s["true_recoverable_kg"], 72.0)  # 80 * 0.90
        self.assertAlmostEqual(s["diversion_rate"], 0.90)

    def test_unmeasured_stream_is_excluded_and_reported(self):
        s = calc.summarise([self.entry()], lambda a, st: (None, "unmeasured"))
        # Mass is still counted - the sacks exist.
        self.assertAlmostEqual(s["total_kg"], 80.0)
        # But none of it counts as diverted, and the reader is told why.
        self.assertEqual(s["true_recoverable_kg"], 0.0)
        self.assertEqual(s["diversion_rate"], 0.0)
        self.assertTrue(any("not yet checked" in l for l in s["limitations"]))

    def test_declared_purity_is_named_in_limitations(self):
        s = calc.summarise([self.entry()], lambda a, st: (0.97, "declared"))
        self.assertAlmostEqual(s["true_recoverable_kg"], 77.6)
        self.assertEqual(s["streams"]["Recyclable"]["purity_source"], "declared")
        self.assertTrue(any("declared, not measured" in l for l in s["limitations"]))

    def test_site_average_fallback_is_named_in_limitations(self):
        s = calc.summarise([self.entry()], lambda a, st: (0.65, "site_average"))
        self.assertTrue(any("site-wide average" in l for l in s["limitations"]))

    def test_weighed_entries_add_mass_but_no_uncertainty(self):
        entries = [
            self.entry(),                                    # C: 80 kg, +-4.0
            self.entry(method="W", weighed_kg=20.0, count=0),  # W: 20 kg, no band
        ]
        s = calc.summarise(entries, lambda a, st: (1.0, "measured"))
        self.assertAlmostEqual(s["total_kg"], 100.0)
        self.assertAlmostEqual(s["total_sigma_kg"], 4.0)  # unchanged by the W entry

    def test_non_recoverable_streams_never_count_toward_diversion(self):
        entries = [self.entry(), self.entry(stream="Residual")]
        s = calc.summarise(entries, lambda a, st: (1.0, "measured"))
        self.assertAlmostEqual(s["total_kg"], 160.0)
        # Only the recyclable half is recoverable, so the rate is 0.5.
        self.assertAlmostEqual(s["diversion_rate"], 0.5)

    def test_mixed_purity_across_areas(self):
        # Same stream, two areas: one checked at 0.5, one never checked.
        entries = [self.entry(area_id=1), self.entry(area_id=2)]
        lookup = lambda a, st: ((0.5, "measured") if a == 1 else (None, "unmeasured"))
        s = calc.summarise(entries, lookup)
        # 80 * 0.5 from area 1, zero from area 2.
        self.assertAlmostEqual(s["true_recoverable_kg"], 40.0)
        self.assertTrue(any("not yet checked" in l for l in s["limitations"]))


class NormalisationTests(TestCase):

    def test_normalising_changes_the_ranking(self):
        # The main building produces more in total but less per person. Raw
        # totals would rank it worst, which is not a finding.
        big = calc.normalise(300.0, 2400)
        small = calc.normalise(60.0, 200)
        self.assertGreater(300.0, 60.0)      # raw
        self.assertLess(big, small)          # normalised - the real story

    def test_zero_denominator_is_zero_not_an_error(self):
        self.assertEqual(calc.normalise(150.0, 0), 0.0)


class FormattingTests(TestCase):

    def test_a_mass_is_never_formatted_without_its_band(self):
        self.assertEqual(calc.format_kg(80.0, 4.0), "80.0 +- 4.0 kg")


# ==========================================================================
# Database-backed behaviour
# ==========================================================================

class ServiceLayerTests(TestCase):

    def setUp(self):
        self.site = Site.objects.create(name="Test Campus")
        self.rec = Stream.objects.create(name="Recyclable", recoverable=True)
        self.res = Stream.objects.create(name="Residual", recoverable=False)
        self.checked = SourceArea.objects.create(
            site=self.site, name="Canteen", denominator_value=100)
        self.unchecked = SourceArea.objects.create(
            site=self.site, name="Library", denominator_value=100)
        Calibration.objects.create(
            site=self.site, stream=self.rec, mean_kg=8.0, sd_kg=2.0, n=25,
            measured_on=timezone.localdate())
        self.now = timezone.now()

    def log(self, area, stream, count=10, fill="full"):
        return SackLog.objects.create(
            source_area=area, stream=stream, count=count, fill=fill,
            method="C", logged_at=self.now)

    def window(self):
        return self.now - timedelta(days=1), self.now + timedelta(days=1)

    def test_area_without_a_check_is_not_treated_as_clean(self):
        self.log(self.unchecked, self.rec)
        start, end = self.window()
        s = services.period_summary(self.site, start, end)
        self.assertAlmostEqual(s["total_kg"], 80.0)
        self.assertEqual(s["true_recoverable_kg"], 0.0)
        self.assertTrue(any("not yet checked" in l for l in s["limitations"]))

    def test_site_average_covers_an_area_with_no_check_of_its_own(self):
        # Canteen is checked at 0.75; the library is not checked at all and
        # therefore inherits the site-wide average rather than nothing.
        PurityCheck.objects.create(
            source_area=self.checked, stream=self.rec,
            items_checked=100, items_wrong=25, checked_at=self.now)
        self.log(self.unchecked, self.rec)
        start, end = self.window()
        s = services.period_summary(self.site, start, end)
        self.assertAlmostEqual(s["true_recoverable_kg"], 60.0)   # 80 * 0.75
        self.assertTrue(any("site-wide average" in l for l in s["limitations"]))

    def test_stale_purity_checks_are_ignored(self):
        # A check from two years ago describes a bin that no longer exists.
        PurityCheck.objects.create(
            source_area=self.checked, stream=self.rec,
            items_checked=100, items_wrong=0,
            checked_at=self.now - timedelta(days=800))
        self.log(self.checked, self.rec)
        start, end = self.window()
        s = services.period_summary(self.site, start, end)
        self.assertEqual(s["true_recoverable_kg"], 0.0)

    def test_checks_are_pooled_by_item_count_not_averaged_by_ratio(self):
        # A 10-item check at 0% and a 190-item check at 100% pool to 95%,
        # not to the 50% a naive mean of the two ratios would give.
        PurityCheck.objects.create(source_area=self.checked, stream=self.rec,
                                   items_checked=10, items_wrong=10,
                                   checked_at=self.now)
        PurityCheck.objects.create(source_area=self.checked, stream=self.rec,
                                   items_checked=190, items_wrong=0,
                                   checked_at=self.now)
        lookup = services.purity_lookup_for(self.site)
        value, source = lookup(self.checked.id, "Recyclable")
        self.assertAlmostEqual(value, 0.95)
        self.assertEqual(source, "measured")

    def test_uncalibrated_stream_is_reported_not_hidden(self):
        # Residual has no calibration in this test's setUp.
        self.log(self.checked, self.res)
        start, end = self.window()
        s = services.period_summary(self.site, start, end)
        self.assertTrue(any("No calibration on file" in l for l in s["limitations"]))

    def test_voided_entries_are_excluded(self):
        log = self.log(self.checked, self.rec)
        log.voided = True
        log.save()
        start, end = self.window()
        self.assertEqual(services.period_summary(self.site, start, end)["total_kg"], 0.0)

    def test_declared_purity_on_a_public_area_is_refused(self):
        from django.core.exceptions import ValidationError
        d = DeclaredPurity(source_area=self.checked, stream=self.rec,
                           value=0.99, reason="wishful thinking",
                           declared_by="someone",
                           declared_on=timezone.localdate())
        # self.checked is public_facing by default, so this must not stand.
        with self.assertRaises(ValidationError):
            d.clean()

    def test_declared_purity_is_ignored_for_public_areas_at_read_time(self):
        # Belt and braces: even if a row reached the table some other way
        # (a data import, a raw SQL insert), the lookup refuses to use it.
        DeclaredPurity.objects.create(
            source_area=self.checked, stream=self.rec, value=0.99,
            reason="snuck in", declared_by="import",
            declared_on=timezone.localdate())
        lookup = services.purity_lookup_for(self.site)
        value, source = lookup(self.checked.id, "Recyclable")
        self.assertIsNone(value)
        self.assertEqual(source, "unmeasured")

    def test_missing_days_are_gaps(self):
        start = self.now - timedelta(days=5)
        end = self.now + timedelta(days=1)
        self.log(self.checked, self.rec)
        gaps = services.missing_entry_days(self.site, start, end)
        # Five of the six days in the window have nothing logged.
        self.assertEqual(len(gaps), 5)

    def test_area_with_no_entries_is_a_gap_not_a_zero(self):
        self.log(self.checked, self.rec)
        start, end = self.window()
        rows = services.per_area_breakdown(self.site, start, end)
        library = [r for r in rows if r["area"] == self.unchecked][0]
        self.assertTrue(library["gap"])
        self.assertNotIn("summary", library)


class OfflineSyncTests(TestCase):
    """
    The offline contract: entries survive, and a replayed queue never
    double-counts. This is the test that stands in for pulling the network
    cable during a demo.
    """

    def setUp(self):
        self.client = Client()
        self.site = Site.objects.create(name="Test Campus")
        self.stream = Stream.objects.create(name="Recyclable", recoverable=True)
        self.area = SourceArea.objects.create(site=self.site, name="Canteen")
        self.device = Device.objects.create(
            site=self.site, label="phone", pair_code="TEST01")
        Calibration.objects.create(
            site=self.site, stream=self.stream, mean_kg=8.0, sd_kg=2.0, n=25,
            measured_on=timezone.localdate())

    def payload(self, entry_id, count=3):
        return {"entries": [{
            "id_uuid": str(entry_id),
            "source_area": self.area.id,
            "stream": self.stream.id,
            "count": count,
            "fill": "full",
            "method": "C",
            "logged_at": timezone.now().isoformat(),
        }]}

    def post(self, body):
        return self.client.post(
            "/api/sync/", data=json.dumps(body), content_type="application/json",
            HTTP_X_SUKOD_DEVICE=self.device.token)

    def test_an_entry_syncs(self):
        eid = uuid.uuid4()
        r = self.post(self.payload(eid))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["accepted"], [str(eid)])
        self.assertEqual(SackLog.objects.count(), 1)

    def test_replaying_the_queue_does_not_double_count(self):
        eid = uuid.uuid4()
        self.post(self.payload(eid))
        r = self.post(self.payload(eid))        # the phone retries
        self.assertEqual(SackLog.objects.count(), 1)
        self.assertEqual(r.json()["duplicates"], [str(eid)])
        # The server reports it HOLDS the id, which is the phone's signal that
        # the row may safely leave the queue.
        self.assertIn(str(eid), r.json()["held"])

    def test_a_replay_with_different_values_does_not_overwrite(self):
        # Same UUID, different count: the first write wins. An id is an
        # identity, not an update key.
        eid = uuid.uuid4()
        self.post(self.payload(eid, count=3))
        self.post(self.payload(eid, count=99))
        self.assertEqual(SackLog.objects.get(pk=eid).count, 3)

    def test_a_batch_of_many_entries_all_land_once(self):
        ids = [uuid.uuid4() for _ in range(10)]
        batch = {"entries": [self.payload(i)["entries"][0] for i in ids]}
        self.post(batch)
        self.post(batch)            # full replay of the whole queue
        self.assertEqual(SackLog.objects.count(), 10)

    def test_a_malformed_entry_is_rejected_without_losing_the_others(self):
        good = uuid.uuid4()
        batch = {"entries": [
            {"id_uuid": "not-a-uuid", "source_area": self.area.id,
             "stream": self.stream.id, "count": 1},
            self.payload(good)["entries"][0],
        ]}
        r = self.post(batch)
        self.assertEqual(SackLog.objects.count(), 1)
        self.assertEqual(len(r.json()["rejected"]), 1)
        self.assertEqual(r.json()["accepted"], [str(good)])

    def test_entry_saves_even_with_no_location(self):
        # No GPS fix must never stop an entry being recorded. It is simply
        # tagged unverified.
        eid = uuid.uuid4()
        self.post(self.payload(eid))
        self.assertFalse(SackLog.objects.get(pk=eid).location_verified)


class ScreenTests(TestCase):
    """The screens render with real data and do not crash on empty data."""

    def setUp(self):
        self.site = Site.objects.create(name="Test Campus")
        self.stream = Stream.objects.create(name="Recyclable", recoverable=True)
        self.area = SourceArea.objects.create(site=self.site, name="Canteen")

    def sign_in_as_supervisor(self):
        """The monitoring screens are behind a login now. Get past it."""
        from django.contrib.auth.models import User
        from core.models import Staff
        user = User.objects.create_user("sup", password="test-pass-1234")
        Staff.objects.create(user=user, site=self.site, role="supervisor")
        self.client.force_login(user)
        return user

    def test_logger_screen_renders(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Canteen")

    def test_dashboard_renders_with_no_entries(self):
        self.sign_in_as_supervisor()
        r = self.client.get("/monitor/")
        self.assertEqual(r.status_code, 200)

    def test_dashboard_shows_the_error_band(self):
        Calibration.objects.create(
            site=self.site, stream=self.stream, mean_kg=8.0, sd_kg=2.0, n=25,
            measured_on=timezone.localdate())
        SackLog.objects.create(source_area=self.area, stream=self.stream,
                               count=10, fill="full", method="C",
                               logged_at=timezone.now())
        self.sign_in_as_supervisor()
        r = self.client.get("/monitor/?period=week")
        # A mass without a band is a broken screen. Assert the band is there.
        self.assertContains(r, "&plusmn;")
        self.assertContains(r, "NOT CHECKED")   # purity was never measured

    def test_service_worker_is_served_from_the_root(self):
        # It can only control pages at or below its own path, so /sw.js it is.
        r = self.client.get("/sw.js")
        self.assertEqual(r.status_code, 200)
        self.assertIn("javascript", r["Content-Type"])

    def test_qr_scan_redirects_to_the_logger_with_the_area_set(self):
        r = self.client.get(f"/a/{self.area.qr_token}/")
        self.assertEqual(r.status_code, 302)
        self.assertIn(f"area={self.area.id}", r["Location"])


class RoleSeparationTests(TestCase):
    """
    The three interfaces are separate, and the separation is enforced on the
    server - not merely by leaving links out of a navigation bar.

    Two rules from CLAUDE.md are being defended here:

      * The purity check is not the janitor's job.
      * Logged volume must never become a performance measure for the janitor,
        so the janitor's interface offers no route to a total or a comparison.
    """

    def setUp(self):
        from django.contrib.auth.models import User
        from core.models import Staff

        self.site = Site.objects.create(name="Test Campus")
        self.stream = Stream.objects.create(name="Recyclable", recoverable=True)
        self.area = SourceArea.objects.create(site=self.site, name="Canteen")

        self.supervisor = User.objects.create_user("sup", password="pw-test-1234")
        Staff.objects.create(user=self.supervisor, site=self.site,
                             role="supervisor")

        self.admin = User.objects.create_user("adm", password="pw-test-1234")
        Staff.objects.create(user=self.admin, site=self.site, role="admin")

    # -- the janitor -------------------------------------------------------

    def test_janitor_needs_no_account_at_all(self):
        # No login, no session, no staff row. The notebook still opens.
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)

    def test_janitor_screen_offers_no_route_to_a_dashboard(self):
        """
        The rule that outranks features: the janitor must not be able to reach
        a screen that turns their logging into a score. Assert on the rendered
        HTML, because this is a property of what they can SEE, not only of
        what the server would allow.
        """
        body = self.client.get("/").content.decode()
        self.assertNotIn("/monitor/", body)
        self.assertNotIn("Dashboard", body)

    def test_janitor_screen_offers_no_route_to_the_purity_check(self):
        # Not their job. A purity check done in a hurry by the person whose
        # sacks are being judged is worthless.
        body = self.client.get("/").content.decode()
        self.assertNotIn("Purity", body)

    # -- the supervisor ----------------------------------------------------

    def test_monitor_screens_require_signing_in(self):
        for path in ["/monitor/", "/monitor/purity/", "/monitor/report/"]:
            r = self.client.get(path)
            self.assertEqual(r.status_code, 302, path)
            self.assertIn("/login/", r["Location"], path)

    def test_supervisor_reaches_every_monitoring_screen(self):
        self.client.force_login(self.supervisor)
        for path in ["/monitor/", "/monitor/purity/",
                     "/monitor/purity/history/", "/monitor/report/"]:
            self.assertEqual(self.client.get(path).status_code, 200, path)

    def test_supervisor_is_refused_the_back_room(self):
        # 403, deliberately not a redirect: bouncing them somewhere else looks
        # like a bug, while "you do not have access" is the truth.
        self.client.force_login(self.supervisor)
        for path in ["/manage/", "/manage/staff/", "/manage/devices/",
                     "/manage/calibration/"]:
            self.assertEqual(self.client.get(path).status_code, 403, path)

    # -- the admin ---------------------------------------------------------

    def test_admin_reaches_the_back_room_and_the_dashboard(self):
        self.client.force_login(self.admin)
        for path in ["/manage/", "/manage/staff/", "/manage/devices/",
                     "/manage/calibration/", "/monitor/"]:
            self.assertEqual(self.client.get(path).status_code, 200, path)

    def test_first_superuser_is_treated_as_admin_without_a_staff_row(self):
        """
        Bootstrap. Without this, createsuperuser would lock itself out: you
        would need a Staff row to reach the screen that creates Staff rows.
        """
        from django.contrib.auth.models import User
        from core.models import Staff

        root = User.objects.create_superuser("root", password="pw-test-1234")
        self.assertFalse(Staff.objects.filter(user=root).exists())
        self.client.force_login(root)
        self.assertEqual(self.client.get("/manage/").status_code, 200)
        # And reading the page did not quietly create a row as a side effect.
        self.assertFalse(Staff.objects.filter(user=root).exists())

    def test_admin_creates_a_supervisor_who_can_then_sign_in(self):
        from django.contrib.auth.models import User

        self.client.force_login(self.admin)
        r = self.client.post("/manage/staff/", {
            "username": "newsup", "password": "pw-created-1234",
            "role": "supervisor", "full_name": "New Supervisor"})
        self.assertEqual(r.status_code, 302)

        created = User.objects.get(username="newsup")
        self.assertEqual(created.staff.role, "supervisor")
        self.assertEqual(created.staff.created_by, "adm")

        # The new account works, and reaches the monitoring interface.
        self.client.logout()
        self.assertTrue(
            self.client.login(username="newsup", password="pw-created-1234"))
        self.assertEqual(self.client.get("/monitor/").status_code, 200)
        # ...but is still refused the back room.
        self.assertEqual(self.client.get("/manage/").status_code, 403)

    def test_creating_an_account_is_recorded_in_the_audit_trail(self):
        self.client.force_login(self.admin)
        self.client.post("/manage/staff/", {
            "username": "audited", "password": "pw-created-1234",
            "role": "supervisor"})
        entry = AuditEntry.objects.get(action="create_staff")
        self.assertEqual(entry.actor, "adm")
        self.assertIn("audited", entry.target)

    def test_a_weak_or_duplicate_account_is_refused(self):
        from django.contrib.auth.models import User

        self.client.force_login(self.admin)
        # Too short.
        self.client.post("/manage/staff/", {
            "username": "weak", "password": "short", "role": "supervisor"})
        self.assertFalse(User.objects.filter(username="weak").exists())
        # Already taken.
        r = self.client.post("/manage/staff/", {
            "username": "sup", "password": "pw-created-1234",
            "role": "supervisor"})
        self.assertContains(r, "already an account")

    def test_the_account_screen_cannot_create_a_janitor(self):
        """
        Per-person accounts for janitorial staff are on the Do Not Build list.
        The role selector must offer only the two signing-in roles.
        """
        self.client.force_login(self.admin)
        body = self.client.get("/manage/staff/").content.decode()
        self.assertIn('value="supervisor"', body)
        self.assertIn('value="admin"', body)
        self.assertNotIn('value="logger"', body)

    def test_a_forged_logger_role_is_rejected_not_silently_accepted(self):
        from django.contrib.auth.models import User

        self.client.force_login(self.admin)
        self.client.post("/manage/staff/", {
            "username": "sneaky", "password": "pw-created-1234",
            "role": "logger"})
        self.assertFalse(User.objects.filter(username="sneaky").exists())

    # -- devices -----------------------------------------------------------

    def test_admin_creates_a_device_with_an_unambiguous_code(self):
        self.client.force_login(self.admin)
        self.client.post("/manage/devices/",
                         {"action": "create", "label": "Canteen staging point"})
        device = Device.objects.get(label="Canteen staging point")
        self.assertEqual(device.role, "logger")
        self.assertIsNone(device.paired_at)
        # 0/O and 1/I/L are left out: the code is read off a screen and typed
        # into a phone by someone standing next to a bin.
        for ambiguous in "01OIL":
            self.assertNotIn(ambiguous, device.pair_code)

    def test_resetting_a_device_invalidates_the_old_phone_but_keeps_entries(self):
        device = Device.objects.create(site=self.site, label="Lost phone",
                                       pair_code="OLDCODE",
                                       token="old-token-value")
        SackLog.objects.create(source_area=self.area, stream=self.stream,
                               count=3, fill="full", method="C",
                               logged_at=timezone.now(), device=device)

        self.client.force_login(self.admin)
        self.client.post("/manage/devices/",
                         {"action": "reset", "device_id": device.id})

        device.refresh_from_db()
        self.assertNotEqual(device.token, "old-token-value")
        self.assertNotEqual(device.pair_code, "OLDCODE")
        # The entries it recorded are the site's record, not the device's.
        self.assertEqual(SackLog.objects.filter(device=device).count(), 1)

    # -- routing -----------------------------------------------------------

    def test_each_role_lands_on_its_own_screen_after_signing_in(self):
        self.client.force_login(self.supervisor)
        self.assertIn("/monitor/", self.client.get("/home/")["Location"])
        self.client.logout()

        self.client.force_login(self.admin)
        self.assertIn("/manage/", self.client.get("/home/")["Location"])

    def test_the_old_flat_urls_still_work(self):
        # They are in the README and in at least one person's history.
        self.client.force_login(self.admin)
        for old, new in [("/dashboard/", "/monitor/"),
                         ("/purity/", "/monitor/purity/"),
                         ("/calibration/", "/manage/calibration/")]:
            r = self.client.get(old)
            self.assertEqual(r.status_code, 302, old)
            self.assertEqual(r["Location"], new, old)


class TrueRecoverableBandTests(TestCase):
    """
    No figure is displayed without its uncertainty. True recoverable used to be
    the one headline mass with no band, which by CLAUDE.md's own rule made the
    screen wrong.
    """

    def test_true_recoverable_carries_the_relative_error_of_its_mass(self):
        """
        Worked example - 10 full recyclable sacks, calibration 8.0 +- 2.0 kg
        from n=25, purity measured at 0.90:

            mass         = 10 * 1.00 * 8.0          = 80.0 kg
            sigma        = 10 * 1.00 * 2.0/sqrt(25) =  4.0 kg
            true         = 80.0 * 0.90              = 72.0 kg
            true sigma   = 72.0 * (4.0 / 80.0)      =  3.6 kg
        """
        entries = [dict(area_id=1, stream="Recyclable", count=10, fill="full",
                        method="C", mean_kg=8.0, sd_kg=2.0, n=25,
                        weighed_kg=None)]
        s = calc.summarise(entries, lambda a, st: (0.90, "measured"))

        self.assertAlmostEqual(s["true_recoverable_kg"], 72.0)
        self.assertAlmostEqual(s["true_recoverable_sigma_kg"], 3.6)

    def test_streams_combine_as_variances_not_as_a_plain_sum(self):
        # Two independent streams, each contributing 3.0 and 4.0 of band:
        # sqrt(3^2 + 4^2) = 5.0, not 7.0.
        entries = [
            dict(area_id=1, stream="Recyclable", count=10, fill="full",
                 method="C", mean_kg=8.0, sd_kg=2.0, n=25, weighed_kg=None),
            dict(area_id=1, stream="Biodegradable", count=10, fill="full",
                 method="C", mean_kg=8.0, sd_kg=2.0, n=25, weighed_kg=None),
        ]
        s = calc.summarise(entries, lambda a, st: (1.0, "measured"),
                           {"Recyclable", "Biodegradable"})
        # Each stream: 80 kg, sigma 4.0, purity 1.0 -> true sigma 4.0 each.
        # Combined: sqrt(4^2 + 4^2) = 5.657, NOT 8.0.
        self.assertAlmostEqual(s["true_recoverable_sigma_kg"], 5.657, places=3)

    def test_an_unmeasured_stream_contributes_no_mass_and_no_band(self):
        # Unmeasured purity contributes zero to true recoverable, so it must
        # contribute zero uncertainty too - a band on a mass of nothing would
        # imply we know something about it.
        entries = [dict(area_id=1, stream="Recyclable", count=10, fill="full",
                        method="C", mean_kg=8.0, sd_kg=2.0, n=25,
                        weighed_kg=None)]
        s = calc.summarise(entries, lambda a, st: (None, "unmeasured"))
        self.assertEqual(s["true_recoverable_kg"], 0.0)
        self.assertEqual(s["true_recoverable_sigma_kg"], 0.0)

    def test_a_non_recoverable_stream_has_no_band(self):
        entries = [dict(area_id=1, stream="Residual", count=10, fill="full",
                        method="C", mean_kg=8.0, sd_kg=2.0, n=25,
                        weighed_kg=None)]
        s = calc.summarise(entries, lambda a, st: (1.0, "measured"))
        self.assertEqual(s["streams"]["Residual"]["true_recoverable_sigma_kg"],
                         0.0)

    def test_the_dashboard_renders_the_band(self):
        """The rule is about what is SHOWN, so assert on the page."""
        from django.contrib.auth.models import User

        site = Site.objects.create(name="Band Campus")
        stream = Stream.objects.create(name="Recyclable", recoverable=True)
        area = SourceArea.objects.create(site=site, name="Canteen")
        Calibration.objects.create(site=site, stream=stream, mean_kg=8.0,
                                   sd_kg=2.0, n=25,
                                   measured_on=timezone.localdate())
        SackLog.objects.create(source_area=area, stream=stream, count=10,
                               fill="full", method="C",
                               logged_at=timezone.now())
        PurityCheck.objects.create(source_area=area, stream=stream,
                                   items_checked=100, items_wrong=10,
                                   checked_at=timezone.now())

        user = User.objects.create_user("bandsup", password="pw-test-1234")
        Staff.objects.create(user=user, site=site, role="supervisor")
        self.client.force_login(user)

        html = self.client.get("/monitor/?period=week").content.decode()
        # The headline block must not contain a bare "kg" with no band before it.
        self.assertIn("True recoverable", html)
        head = html.split("True recoverable", 1)[1][:400]
        self.assertIn("&plusmn;", head)

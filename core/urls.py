"""
URL map, grouped by who the screen belongs to.

Three front doors, deliberately separate:

    /          the notebook   - janitor    - device-paired, no login, ever
    /monitor/  the dashboard  - supervisor - signs in
    /manage/   the back room  - admin      - signs in, creates the other two

Short paths, because some of these get typed on a phone.
"""
from django.urls import path
from django.views.generic import RedirectView
from django.contrib.auth import views as auth_views

from . import views

urlpatterns = [
    # ---------------------------------------------------------------- janitor
    # The notebook is at the root because it is the app. The person using it
    # never navigates anywhere: they open the icon and they are already there.
    path("", views.logger_screen, name="logger"),
    path("pair/", views.pair, name="pair"),

    # Sync and today's list, used by the service worker and app.js.
    # These belong to the notebook and are authenticated by device token.
    path("api/sync/", views.sync, name="sync"),
    path("api/today/", views.today_entries, name="today"),
    path("api/undo/<uuid:entry_id>/", views.undo, name="undo"),

    # QR sign target: opens the notebook with that area already selected.
    path("a/<str:token>/", views.scan, name="scan"),

    # ------------------------------------------------------------- supervisor
    # Monitoring only. Nothing under /monitor/ can create or edit a sack entry.
    path("monitor/", views.dashboard, name="dashboard"),
    path("monitor/purity/", views.purity_check, name="purity"),
    path("monitor/purity/history/", views.purity_history, name="purity_history"),
    path("monitor/report/", views.report_pdf, name="report"),

    # ------------------------------------------------------------------ admin
    # The back room. Everything that brings another role into existence.
    path("manage/", views.manage_home, name="manage_home"),
    path("manage/staff/", views.manage_staff, name="manage_staff"),
    path("manage/devices/", views.manage_devices, name="manage_devices"),
    path("manage/calibration/", views.calibration, name="calibration"),

    # ------------------------------------------------------------------- auth
    # Django's own login machinery, pointed at our template. Only supervisors
    # and admins ever reach it; the janitor's device pairs instead.
    path("login/", auth_views.LoginView.as_view(
        template_name="login.html", redirect_authenticated_user=True),
        name="login"),
    path("logout/", auth_views.LogoutView.as_view(next_page="/login/"),
         name="logout"),

    # Not a screen: reads the role and forwards to that role's landing page.
    path("home/", views.after_login, name="after_login"),

    # ---------------------------------------------------- moved, kept working
    # The old flat paths are in the README and in at least one person's browser
    # history. Redirect rather than 404; permanent=False so a bookmark that
    # gets re-pointed later is not cached forever.
    path("dashboard/", RedirectView.as_view(pattern_name="dashboard")),
    path("purity/", RedirectView.as_view(pattern_name="purity")),
    path("purity/history/", RedirectView.as_view(pattern_name="purity_history")),
    path("report/", RedirectView.as_view(pattern_name="report",
                                         query_string=True)),
    path("calibration/", RedirectView.as_view(pattern_name="calibration")),

    # ------------------------------------------------------------ PWA plumbing
    # sw.js must be at the root to control the whole origin.
    path("sw.js", views.service_worker, name="sw"),
    path("offline/", views.offline, name="offline"),
]
